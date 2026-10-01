"""Let the builder correct hallucinated tool names without aborting its proposal."""

from google.adk.plugins.base_plugin import BasePlugin
from harnest import lifecycle
from harnest.skills import SkillNotFoundError


def recovery_feedback(tool, error):
    """Correct missing names without masking policy, validation, or real execution failures."""
    if isinstance(error, SkillNotFoundError) and tool.name in {"load_skill", "load_skill_resource"}:
        return {"error": "The requested bundled skill or resource does not exist.",
                "recovery": "Use list_skills to find the exact skill name, then load_skill to read its instructions. Use only exact relative resource paths linked in those instructions or a successfully loaded reference. Do not guess documentation filenames or use public URLs as resource paths."}
    if isinstance(error, ValueError) and tool.description == "Tool not found":
        return {"error": "That tool does not exist. The available tools are list_skills, load_skill, load_skill_resource, read_files, fused_discover, and plan_fused_mcp.",
                "source_protocol": 'Call read_files with {"paths": ["relative/path"]} to request project source. Studio will supply authorized files in the next turn. Shell, browser, network, and write tools are unavailable; diagnose from supplied source and user-provided evidence. Do not invent file contents.'}
    return None


class SourceProtocolRecovery(BasePlugin):
    """Bound recoverable tool and skill lookups, leaving other failures visible."""

    def __init__(self):
        """Give the private plugin a stable name without global mutable counters."""
        super().__init__(name="studio_source_protocol")

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        """Allow at most two corrective lookup responses per session across all lookup kinds."""
        feedback = recovery_feedback(tool, error)
        if feedback is None:
            return None
        key = "temp:studio_lookup_recoveries"
        attempts = tool_context.state.get(key, 0)
        if attempts >= 2:
            return None
        tool_context.state[key] = attempts + 1
        return feedback


@lifecycle.adk_plugin
def source_protocol_recovery():
    """Register recovery through Harnest's supported native-plugin boundary."""
    return SourceProtocolRecovery()
