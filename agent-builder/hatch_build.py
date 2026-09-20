"""Package Studio with a compiled Harnest agent and release-matched skill snapshots."""

from pathlib import Path
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Require successful native compilation before a production wheel can be produced."""

    def initialize(self, version, build_data):
        """Compile wheels; carry canonical grounding into source distributions for rebuilding."""
        sys.path.insert(0, str(Path(self.root) / "src"))
        from harnest_builder.assistant_build import SKILL_NAMES, compile_assistant, skill_sources

        include = build_data.setdefault("force_include", {})
        if self.target_name == "wheel":
            artifact = Path(self.root) / "build" / "assistant"
            compile_assistant(artifact)
            include[str(artifact)] = "harnest_builder/_assistant"
        else:
            for name in SKILL_NAMES:
                include[str(skill_sources() / name)] = f"src/harnest_builder/assistant_source/skills/{name}"
