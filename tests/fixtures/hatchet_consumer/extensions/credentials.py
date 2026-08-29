"""Secret-safe Hatchet credentials owned by the consuming application."""

from __future__ import annotations

import os

from harnest.credentials import Credential, CredentialProvider, CredentialRequest
from harnest.lifecycle import lifecycle


class HatchetEnvironmentCredentials(CredentialProvider):
    """Resolve the local Hatchet token only at its outbound SDK boundary."""

    async def resolve(self, request: CredentialRequest) -> Credential | None:
        """Return only Hatchet credentials and keep missing tokens unavailable."""

        if request.audience != "hatchet":
            return None
        token = os.environ.get("HATCHET_CLIENT_TOKEN")
        return None if not token else Credential(token)


@lifecycle.credential_provider
def credential_provider() -> HatchetEnvironmentCredentials:
    """Give Harnest lifecycle ownership of the environment-backed resolver."""

    return HatchetEnvironmentCredentials()
