"""Copy to messages.py; import with from harnest.lib.messages import normalize."""


def normalize(message: str) -> str:
    """Collapse whitespace without retaining request data."""
    return " ".join(message.split())
