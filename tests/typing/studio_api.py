"""Consumer-side embedding and pack authoring must remain IDE discoverable."""

from pathlib import Path
from typing import assert_type

from fastapi import FastAPI
from harnest import authoring
from harnest_builder.__main__ import main
from harnest_builder.app import create_app
from harnest_builder.mcp_credentials import CredentialStore
from harnest_builder.pack_distribution import package_packs
from harnest_builder.packs import Packs
from harnest_builder.types import Completion
from harnest_builder.ui_types import package_types


def embed(completion: Completion) -> None:
    """Check actual supported imports without launching a process or writing a wheel."""
    cli = authoring.ProjectCLI('acme', [])
    cli.add_studio(command=['acme'], packs=[Path('studio')])
    cli.add_studio(command=('acme',), trusted_ui=('acme',))
    main(arguments=['--port', '1940'], embedded_packs=(Path('studio'),),
         cli_command=('acme',), trusted_ui=['acme'], init_args=['--profile', 'company'])
    credentials = CredentialStore(Path('/tmp/studio-credentials'))
    packs = Packs([Path('studio')])
    assert_type(create_app(Path('.'), ('acme',), packs=packs, completion=completion,
                           credentials=credentials, safe_ui=False), FastAPI)
    assert_type(create_app(Path('.'), 'harnest'), FastAPI)
    assert_type(packs.expand(['acme/deploy']), list[str])
    assert_type(credentials.read(Path('.')), dict[str, str])
    assert_type(package_packs([Path('studio')], Path('dist'), 'acme', '1.0.0'), Path)
    assert_type(package_types(Path('dist')), Path)
