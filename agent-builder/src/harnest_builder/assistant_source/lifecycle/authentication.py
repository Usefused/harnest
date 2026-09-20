"""Keep the private compiled service accessible only to its supervising Studio."""

import hmac
import os

from harnest import lifecycle
from harnest.auth import AuthPrincipal, AuthenticationError


@lifecycle.authenticate
def authenticate(connection, principal):
    """Require a launch-scoped token without promoting credentials into model context."""
    token = os.getenv("HARNEST_BUILDER_SERVICE_TOKEN", "")
    authorization = connection.headers.get("authorization", "")
    if not token or not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
        raise AuthenticationError()
    return AuthPrincipal(user_id="studio")
