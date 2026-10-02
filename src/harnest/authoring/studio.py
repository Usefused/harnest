"""Optional Studio integration without importing the web runtime for ordinary CLI use."""
from pathlib import Path
from typing import Sequence

from .contracts import ProjectError


class StudioLauncher:
    """Keep executable authority and bundled UI paths in trusted company startup code."""

    commands = ('add', 'compile', 'test', 'env', 'serve', 'run', 'extensions', 'provision')

    def __init__(self, command: Sequence[str], packs: Sequence[Path], trusted_ui: Sequence[str],
                 init_args: Sequence[str]) -> None:
        """Snapshot launch arguments; resolve assets relative to the installed company package."""
        if isinstance(command, str) or not command or any(not isinstance(arg, str) or not arg for arg in command):
            raise ProjectError('Studio command must be a non-empty executable argv sequence')
        self.command = tuple(command)
        self.packs = tuple(Path(path).resolve() for path in packs)
        self.trusted_ui = tuple(trusted_ui)
        self.init_args = tuple(init_args)
        self.forwarded: set[str] = set()

    def run(self, arguments: Sequence[str]) -> int:
        """Load the optional dependency only when the user launches Studio."""
        try:
            from harnest_builder.__main__ import main
        except ModuleNotFoundError as error:
            if error.name != 'harnest_builder':
                raise
            raise ProjectError('Studio is not installed. Install the company CLI Studio extra (harnest-agent-builder).') from error
        main(arguments=list(arguments), embedded_packs=self.packs, cli_command=self.command,
             trusted_ui=self.trusted_ui, init_args=self.init_args)
        return 0
