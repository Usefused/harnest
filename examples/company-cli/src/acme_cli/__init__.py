"""One company CLI for agent initialization, maintenance and its bundled Studio."""
import os
from pathlib import Path

from harnest.authoring import ChangePlan, ProjectCLI, ProjectPack

pack = ProjectPack('acme', schema_version=1)


@pack.initialize
def initialize(context):
    """Include the company setup guide in every agent created from CLI or Studio."""
    return ChangePlan(context.files.write_text('docs/acme.md', 'Created with Acme. Use acme upgrade to review team updates.\n'))


def main() -> int:
    """Resolve bundled UI assets independently of the team's current working directory."""
    cli = ProjectCLI('acme', [pack], harnest_command=(os.getenv('HARNEST_CLI', 'harnest'),))
    cli.add_studio(command=('acme',), packs=[Path(__file__).parent / 'studio'])
    return cli.run()
