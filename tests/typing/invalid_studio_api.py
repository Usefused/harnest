"""Reject launch and command mistakes before a company CLI starts Studio."""

from pathlib import Path
from harnest import authoring
from harnest_builder.__main__ import main
from harnest_builder.app import create_app
from harnest_builder.packs import Packs

cli = authoring.ProjectCLI('acme', [])
cli.add_studio(command='acme')
cli.add_studio(command=(123,))
main(cli_command='acme')
main(embedded_packs=[123])
main(arguments=[123])
main(trusted_ui=[123])
main(init_args=[123])
create_app(Path('.'), ('acme',), safe_ui='yes')
create_app(Path('.'), ('acme',), packs=['acme'])
create_app(Path('.'), ('acme',), completion=lambda: 'text')
Packs([123])
