"""An example company CLI; v1/v2 switches demonstrate a real repository migration."""
from enum import Enum
import os
from pathlib import Path
import sys

from pydantic import BaseModel, Field
from harnest.authoring import ChangePlan, ProjectCLI, ProjectContext, ProjectPack, WritePolicy


class Environment(str, Enum):
    """Environments supported by the example company's deployment policy."""
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Options(BaseModel):
    """Typed values collected by the company CLI when creating an agent."""
    team: str = Field(min_length=1, description="Team responsible for this agent")
    environment: Environment = Environment.DEVELOPMENT


class SettingsV1(BaseModel):
    """The first company configuration contract."""
    owner: str = Field(min_length=1)
    environment: Environment


class Settings(BaseModel):
    """The current company configuration contract."""
    team: str = Field(min_length=1)
    environment: Environment


def create_pack(version: int = 2) -> ProjectPack:
    """Build either example release so migrations can be tried without publishing packages."""
    pack = ProjectPack('acme', schema_version=version, options=Options,
                       templates=Path(__file__).parent / 'templates', config_file='acme-agent.yaml',
                       config_model=SettingsV1 if version == 1 else Settings)

    @pack.initialize
    def initialize(context: ProjectContext) -> ChangePlan:
        """Add company configuration and a tracked workflow to the standard scaffold."""
        key = 'owner' if version == 1 else 'team'
        return ChangePlan(
            context.yaml.set('acme-agent.yaml', key=(key,), value=context.options.team),
            context.yaml.set('acme-agent.yaml', key=('environment',), value=context.options.environment.value),
            context.files.from_template('.github/workflows/agent.yml', template=f'ci-v{version}.yml'),
        )

    if version == 2:
        @pack.migration(from_version=1, to_version=2)
        def migrate_ownership(context: ProjectContext) -> ChangePlan:
            """Preserve the chosen team and upgrade only an unchanged generated workflow."""
            return ChangePlan(
                context.yaml.rename_key('acme-agent.yaml', source=('owner',), destination=('team',)),
                context.files.from_template('.github/workflows/agent.yml', template='ci-v2.yml', policy=WritePolicy.MANAGED),
            )
    return pack


def main() -> int:
    """Run the example CLI; its environment switches exist only for the versioned demo."""
    version = int(os.environ.get('ACME_DEMO_PACK_VERSION', '2'))
    cli = ProjectCLI('acme-agent', [create_pack(version)],
                     harnest_command=(os.environ.get('HARNEST_CLI', 'harnest'),))
    return cli.run()


if __name__ == '__main__':
    sys.exit(main())
