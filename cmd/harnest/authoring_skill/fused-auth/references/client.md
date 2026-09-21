# Fused Auth client contract

Grounded in Fused's built-in Auth Python SDK and Harnest's vendored OAuth client.
Harnest's private `_fused_auth_client` is an internal host integration, not an
import for generated application source.

## Obtain the built-in SDK

```sh
fused-cli sdk auth-client --language python --out ./clients
```

This materializes `clients/fused-auth` locally without logging into an Engine.
Install that local project in the consuming application environment. Python uses:

```python
from fused.fused_auth import FusedAuthClient, FusedAuthConfig
```

The `ts` language option materializes the TypeScript SDK. Read its shipped
definitions instead of translating Python method names by guesswork.
Do not assume a public registry package exists. Materializing this SDK does not
register an OAuth client or authorize any Engine operation.

## Authorization-code flow with PKCE

```python
client = FusedAuthClient(FusedAuthConfig(
    issuer=issuer,
    client_id=registered_client_id,
    redirect_uri=registered_callback_uri,
    client_secret=server_side_client_secret,  # None for public clients
))
authorization = client.authorize_url(scopes=requested_scopes)
```

`AuthorizeRequest` contains `url`, `state`, and `code_verifier`. The client generates
fresh state and an S256 challenge. The host must store state/verifier server-side,
bind them to the initiating session and callback, expire them, and redirect the
browser to `authorization.url`. Do not put the verifier in browser-visible URLs.

On callback, handle a provider error without logging its raw payload. Reject
missing, mismatched, expired, or reused state. Consume the stored flow once before:

```python
tokens = client.exchange_code(code, stored_code_verifier)
```

The SDK does not validate callback state for the host. Keep callback endpoints
separate from model tools and do not accept a redirect URI chosen by the model.

`TokenResponse` fields are `access_token`, `token_type`, `expires_in` (seconds),
`scope`, and optional `refresh_token`. Compute expiry at receipt. In trusted
application code, provide `access_token` to `FusedAdminConfig`; send only connected
status and the capabilities the host chooses to expose to the model.

## Refresh, logout, and boundaries

- `metadata()` retrieves the issuer's OAuth authorization-server metadata.
- `refresh(refresh_token)` returns a new `TokenResponse`; store any rotated
  refresh token. Missing/invalid refresh credentials require reconnect.
- `revoke(token)` revokes the specified token. Logout also clears host-side session
  state; do not assume this method revokes every related token.
- `FusedAuthError` exposes `error`, `error_description`, and `status`; map these to
  safe host messages without leaking raw token-endpoint responses.

Request only scopes needed by the intended flow. The Harnest connector's existing
management flow uses `app.read`, `app.create`, `app.manage`, `app.tokens.manage`,
`service.read`, `service.consume`, `workspace.read`, `bucket.use`, and
`catalogue.read`; narrower flows should request a subset. OAuth scopes do not
replace Engine owner, workspace, bucket, or service permissions.

This authenticates a developer/application to Fused. Provider connections such as
Google consent are configured separately through Fused's connection management.
MCP execution tokens are separately issued through Fused Admin for runtime calls.
Never inject developer refresh tokens or Admin access tokens into the agent's MCP
environment. Use `harnest-authentication` for incoming agent principals and trusted
credential-provider resolution when the authored application needs user identity.
