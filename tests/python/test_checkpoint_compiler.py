import tempfile
import unittest
from pathlib import Path

from harnest.bundle import BundleConventionError, compile_application


def _write_project(
    root: Path, *, mismatch: bool = False, native: bool = False
) -> None:
    (root / "lib").mkdir()
    (root / "extensions").mkdir()
    checkpoint_source = (
        "from harnest.checkpoint import LangGraphStore\n"
        "from langgraph.checkpoint.memory import InMemorySaver\n"
        "store = LangGraphStore(InMemorySaver())\n"
        if native
        else "from harnest.store import MemoryStore\n"
        "store = MemoryStore()\n"
    )
    (root / "lib" / "storage.py").write_text(
        checkpoint_source, encoding="utf-8"
    )
    (root / "extensions" / "storage.py").write_text(
        "from harnest.lifecycle import lifecycle\n"
        "from harnest.session import InMemorySessionStore\n"
        "from harnest.lib.storage import store\n"
        "@lifecycle.session_store\n"
        "def sessions(): return InMemorySessionStore()\n"
        "@lifecycle.checkpointer\n"
        "def checkpointer(): return store\n",
        encoding="utf-8",
    )
    checkpointer = (
        "store" if native else "store.as_langgraph_checkpointer()"
    )
    if mismatch:
        checkpointer = "MemoryStore().as_langgraph_checkpointer()"
    source = (
        "from harnest.agent import Agent\n"
        "from harnest.store import MemoryStore\n"
        "from harnest.lib.storage import store\n"
        "from langgraph.graph import StateGraph\n"
        "class State(dict): pass\n"
        "builder = StateGraph(dict)\n"
        "builder.add_node('work', lambda state: state)\n"
        "builder.set_entry_point('work')\n"
        "builder.set_finish_point('work')\n"
        f"native = builder.compile(checkpointer={checkpointer})\n"
        "root_agent = Agent.advanced(native, name='support')\n"
    )
    (root / "agent.py").write_text(source, encoding="utf-8")


class CheckpointCompilerTests(unittest.TestCase):
    def test_advanced_langgraph_uses_lifecycle_owned_harnest_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_project(root)

            application = compile_application(
                root,
                entrypoint="agent:root_agent",
                framework="langgraph",
                mode="advanced",
            )

        self.assertIs(application.target.checkpointer.store, application.checkpointer)
        self.assertEqual(application.checkpoint_metadata["owner"], "harnest")

    def test_advanced_langgraph_rejects_hidden_mismatched_checkpointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_project(root, mismatch=True)

            with self.assertRaisesRegex(
                BundleConventionError, "as_langgraph_checkpointer"
            ):
                compile_application(
                    root,
                    entrypoint="agent:root_agent",
                    framework="langgraph",
                    mode="advanced",
                )

    def test_advanced_langgraph_accepts_lifecycle_owned_native_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_project(root, native=True)

            application = compile_application(
                root,
                entrypoint="agent:root_agent",
                framework="langgraph",
                mode="advanced",
            )

        self.assertIs(application.target.checkpointer, application.checkpointer)
        self.assertEqual(application.checkpoint_metadata["owner"], "langgraph")


if __name__ == "__main__":
    unittest.main()
