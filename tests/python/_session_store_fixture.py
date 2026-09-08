from pathlib import Path


def write_session_store(root: Path) -> None:
    extensions = root / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    (extensions / "sessions.py").write_text(
        "from harnest import lifecycle\n"
        "from harnest.session import InMemorySessionStore\n"
        "@lifecycle.storage.sessions\n"
        "def session_store(): return InMemorySessionStore()\n",
        encoding="utf-8",
    )
    (extensions / "checkpoints.py").write_text(
        "from harnest.checkpoint import MemoryStore\n"
        "from harnest import lifecycle\n"
        "@lifecycle.storage.checkpoints\n"
        "def checkpointer(): return MemoryStore()\n",
        encoding="utf-8",
    )
