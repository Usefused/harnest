"""Reject pack and launch mistakes before a company starts Studio or loads a pack."""

from pathlib import Path
from harnest import authoring
from harnest_builder.__main__ import main
from harnest_builder.app import create_app
from harnest_builder.packs import Packs

pack = authoring.ProjectPack(1)
authoring.ProjectPack('1')
pack.command(123)
pack.command('check', help=1)
main(arguments=[123])
authoring.load_pack(123)
authoring.load_pack(Path('.'), import_project='yes')
pack.bind(1, Path('.'))
create_app(Path('.'), ('acme',), init_args=[1])
create_app(Path('.'), ('acme',), packs=['acme'])
create_app(Path('.'), ('acme',), completion=lambda: 'text')
Packs([123])
