"""Acme's project hooks. pack.yaml holds the pack's name, version and command.

ACME_DEMO_PACK_VERSION=1 loads the first release of this pack so the 1 -> 2
migration can be tried locally; a real company publishes each release instead.
"""
from enum import Enum
import os

from pydantic import BaseModel, Field
from harnest.authoring import ChangePlan, ProjectContext, ProjectPack, WritePolicy

VERSION = int(os.environ.get("ACME_DEMO_PACK_VERSION", "2"))


class Environment(str, Enum):
    """Environments supported by Acme's deployment policy."""
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Options(BaseModel):
    """Typed values passed with --option when creating an agent."""
    team: str = Field(min_length=1, description="Team responsible for this agent")
    environment: Environment = Environment.DEVELOPMENT


class SettingsV1(BaseModel):
    """The first Acme configuration contract."""
    owner: str = Field(min_length=1)
    environment: Environment


class Settings(BaseModel):
    """The current Acme configuration contract."""
    team: str = Field(min_length=1)
    environment: Environment


pack = ProjectPack(VERSION, options=Options, templates="templates", config_file="acme-agent.yaml",
                   config_model=SettingsV1 if VERSION == 1 else Settings)


@pack.initialize
def initialize(context: ProjectContext) -> ChangePlan:
    """Add Acme configuration, compile selections and a tracked CI workflow."""
    key = "owner" if VERSION == 1 else "team"
    return ChangePlan(
        context.yaml.set("harnest-compile.yaml", key=("version",), value=1),
        context.yaml.set("acme-agent.yaml", key=(key,), value=context.options.team),
        context.yaml.set("acme-agent.yaml", key=("environment",), value=context.options.environment.value),
        context.files.from_template(".github/workflows/agent.yml", template=f"ci-v{VERSION}.yml"),
    )


if VERSION == 2:
    @pack.migration(from_version=1, to_version=2)
    def migrate_ownership(context: ProjectContext) -> ChangePlan:
        """Keep the chosen team and update the workflow only if nobody edited it."""
        return ChangePlan(
            context.yaml.rename_key("acme-agent.yaml", source=("owner",), destination=("team",)),
            context.files.from_template(".github/workflows/agent.yml", template="ci-v2.yml",
                                        policy=WritePolicy.MANAGED),
        )


@pack.command("whoami", help="Print the team that owns an agent")
def whoami(args: list[str]) -> int:
    """A company command: `acme whoami AGENT_DIR`."""
    import yaml
    from pathlib import Path

    if len(args) != 1:
        print("usage: acme whoami AGENT_DIR")
        return 2
    settings = yaml.safe_load((Path(args[0]) / "acme-agent.yaml").read_text()) or {}
    print(settings.get("team") or settings.get("owner") or "unknown")
    return 0
