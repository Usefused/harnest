"""Authenticate the channel worker before accepting its isolated actor identity."""

import hmac
import os
import re

from harnest import lifecycle
from harnest.auth import AuthPrincipal, AuthenticationError


@lifecycle.authenticate
def authenticate(connection, principal):
    """Trust actor headers only from the independently authenticated channel worker."""
    secret = os.environ.get("CHANNEL_AGENT_TOKEN", "")
    supplied = connection.headers.get("authorization", "")
    actor = connection.headers.get("x-channel-actor", "")
    if not secret or not hmac.compare_digest(supplied, "Bearer " + secret):
        raise AuthenticationError()
    if re.fullmatch(r"[a-f0-9]{64}", actor) is None:
        raise AuthenticationError()
    return AuthPrincipal(actor)
