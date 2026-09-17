"""Vendored Fused Auth OAuth 2.0 client (standard library only).

Copied from fused-cli's built-in ``fused-auth`` Python client so the Studio can
authenticate a developer into Fused without a fused-cli install or session.
It talks to the Engine's public OAuth endpoints over HTTPS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional


class FusedAuthError(Exception):
    """Raised when Fused returns an OAuth 2.0 error envelope."""

    def __init__(self, error: str, description: Optional[str], status: int) -> None:
        super().__init__(description or error)
        self.error = error
        self.error_description = description
        self.status = status


@dataclass
class FusedAuthConfig:
    """Connection settings for one registered Fused OAuth client."""

    issuer: str
    client_id: str
    redirect_uri: str
    # Confidential client secret (fos_...). Omit for public (PKCE) clients.
    client_secret: Optional[str] = None


@dataclass
class AuthorizeRequest:
    """The artifacts an application needs to start a browser authorization."""

    url: str
    state: str
    code_verifier: str


@dataclass
class TokenResponse:
    """A successful token-endpoint response."""

    access_token: str
    token_type: str
    expires_in: int
    scope: str
    refresh_token: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "TokenResponse":
        return cls(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_in=int(data.get("expires_in", 0)),
            scope=data.get("scope", ""),
            refresh_token=data.get("refresh_token"),
        )


def _random_string(length: int) -> str:
    """Generates a URL-safe random string suitable for state and PKCE values."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _pkce_challenge(verifier: str) -> str:
    """Derives the S256 code challenge from a verifier via SHA-256."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class FusedAuthClient:
    """Direct OAuth 2.0 client for Fused's authorization server."""

    def __init__(self, config: FusedAuthConfig) -> None:
        # An absolute issuer keeps endpoint joins unambiguous.
        if not config.issuer.startswith(("http://", "https://")):
            raise ValueError("FusedAuthClient issuer must be an absolute URL")
        self.issuer = config.issuer.rstrip("/")
        self.client_id = config.client_id
        self.client_secret = config.client_secret
        self.redirect_uri = config.redirect_uri

    def metadata(self) -> dict:
        """Returns the RFC 8414 discovery document for the configured issuer."""
        return self._get_json("/.well-known/oauth-authorization-server")

    def authorize_url(
        self, scopes: Optional[List[str]] = None, state: Optional[str] = None
    ) -> AuthorizeRequest:
        """Builds the authorize URL plus a fresh PKCE pair.

        The caller navigates the user's browser to `url`; Fused renders the
        consent screen and redirects back to the registered redirect URI with
        `code` and `state`.
        """
        state = state or _random_string(32)
        code_verifier = _random_string(64)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes or []),
            "state": state,
            "code_challenge": _pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        url = f"{self.issuer}/oauth/authorize?{urllib.parse.urlencode(params)}"
        return AuthorizeRequest(url=url, state=state, code_verifier=code_verifier)

    def exchange_code(self, code: str, code_verifier: str) -> TokenResponse:
        """Exchanges an authorization code (and its PKCE verifier) for tokens."""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
        }
        return self._token_request(form)

    def refresh(self, refresh_token: str) -> TokenResponse:
        """Refreshes an access token with a previously issued refresh token."""
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def revoke(self, token: str) -> None:
        """Revokes an access or refresh token (RFC 7009)."""
        self._post("/oauth/revoke", {"token": token}, expect_json=False)

    def _token_request(self, form: dict) -> TokenResponse:
        data = self._post("/oauth/token", form, expect_json=True)
        return TokenResponse.from_dict(data)

    def _auth_headers(self, form: dict) -> dict:
        """Returns client-authentication headers for a token/revoke request.

        Confidential clients use HTTP Basic (client_secret_basic); public
        clients carry only the client id in the form body.
        """
        if self.client_secret:
            raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
            encoded = base64.b64encode(raw).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        form.setdefault("client_id", self.client_id)
        return {}

    def _post(self, path: str, form: dict, expect_json: bool):
        """Performs one form-urlencoded POST and decodes the response."""
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        headers.update(self._auth_headers(form))
        body = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            f"{self.issuer}{path}", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request) as response:
                if not expect_json:
                    return None
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Best-effort decode of the OAuth error envelope without masking
            # transport failures as authorization errors.
            try:
                payload = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, ValueError):
                payload = {}
            raise FusedAuthError(
                payload.get("error", "server_error"),
                payload.get("error_description"),
                error.code,
            ) from error

    def _get_json(self, path: str):
        """Performs one unauthenticated GET and decodes its JSON body."""
        with urllib.request.urlopen(f"{self.issuer}{path}") as response:
            return json.loads(response.read().decode("utf-8"))
