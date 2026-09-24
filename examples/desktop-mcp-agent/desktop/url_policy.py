"""Validate desktop navigation URLs with an optional authored host list."""

from urllib.parse import urlsplit


def allowed_url(url: str, allowed_hosts: frozenset[str]) -> bool:
    """Accept HTTP(S) URLs, restricting hosts only when configured to do so."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and bool(host)
            and parsed.username is None
            and parsed.password is None
            and (not allowed_hosts or host in allowed_hosts)
        )
    except ValueError:
        return False
