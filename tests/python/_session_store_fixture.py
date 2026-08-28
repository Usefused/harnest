from pathlib import Path


def write_session_store(root: Path) -> None:
    extensions = root / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    (extensions / "sessions.py").write_text(
        "from harnest.lifecycle import lifecycle\n"
        "from harnest.session import InMemorySessionStore\n"
        "@lifecycle.session_store\n"
        "def session_store(): return InMemorySessionStore()\n",
        encoding="utf-8",
    )
