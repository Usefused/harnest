"""Consumer-side embedding and pack authoring must remain IDE discoverable."""

from pathlib import Path
from typing import assert_type

from fastapi import FastAPI
from harnest import authoring
from harnest.authoring.distribution import build_pack
from harnest_builder.__main__ import main
from harnest_builder.app import create_app
from harnest_builder.mcp_credentials import CredentialStore
from harnest_builder.packs import Packs
from harnest_builder.types import Completion
from harnest_builder.ui_types import package_types


def embed(completion: Completion) -> None:
    """Check actual supported imports without launching a process or writing a wheel."""
    pack = authoring.ProjectPack(1, templates='templates')
    pack.command('check', help='Check an agent')(lambda args: 0)
    assert_type(pack.commands, tuple[authoring.PackCommand, ...])
    assert_type(authoring.load_pack(Path('acme')), authoring.Pack)
    main(arguments=['--port', '1940', '--pack', 'acme', '--option', 'team=support'])
    credentials = CredentialStore(Path('/tmp/studio-credentials'))
    packs = Packs([Path('acme')])
    assert_type(create_app(Path('.'), ('harnest',), packs=packs, completion=completion,
                           credentials=credentials, init_args=('--pack', 'acme')), FastAPI)
    assert_type(create_app(Path('.'), 'harnest'), FastAPI)
    assert_type(packs.expand(['acme/deploy']), list[str])
    assert_type(credentials.read(Path('.')), dict[str, str])
    assert_type(build_pack(Path('acme'), Path('dist')), Path)
    assert_type(package_types(Path('dist')), Path)
