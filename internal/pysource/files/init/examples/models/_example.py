"""Copy to messages.py; import with from harnest.models.messages import Message."""

from pydantic import BaseModel, Field


class Message(BaseModel):
    """Validate a non-empty message at an authored boundary."""

    text: str = Field(min_length=1)
