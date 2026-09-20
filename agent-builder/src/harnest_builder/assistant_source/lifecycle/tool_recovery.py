"""Let the builder correct hallucinated tool names without aborting its proposal."""

from google.adk.plugins.base_plugin import BasePlugin
from harnest import lifecycle


class SourceProtocolRecovery(BasePlugin):
    """Recover only missing-tool lookups, leaving real tool failures visible."""

    def __init__(self):
        """Give the private plugin a stable name without global mutable counters."""
        super().__init__(name="studio_source_protocol")

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        """Return the actual source-reading protocol for at most two invalid calls per session."""
        if not isinstance(error, ValueError) or tool.description != "Tool not found":
            return None
        key = "temp:studio_missing_tool_recoveries"
        attempts = tool_context.state.get(key, 0)
        if attempts >= 2:
            return None
        tool_context.state[key] = attempts + 1
        return {"error": "That tool does not exist. The available tools are list_skills, load_skill, and load_skill_resource.",
                "source_protocol": 'To request project source, finish this response with ONLY {"read_files": ["relative/path"]}. read_files is a JSON response field, NOT a callable tool. Studio will supply the authorized source in the next request. Do not invent file contents.'}


@lifecycle.adk_plugin
def source_protocol_recovery():
    """Register recovery through Harnest's supported native-plugin boundary."""
    return SourceProtocolRecovery()
