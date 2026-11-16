from harnest.logging import get_logger
from harnest.agent import tool
from harnest.tracing import span


logger = get_logger("tools.echo")


@tool
def echo(message: str) -> str:
    """Return a message unchanged."""

    with span("tool.echo", message_length=len(message)):
        logger.info("tool.echo.completed", message_length=len(message))
        return message
