"""Read evaluation runner declarations without importing user code."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self

from .server_config import _mapping, _read_yaml


class RunnerSpec(BaseModel):
    """Validated registration for one installed callable or the built-in evaluator."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    entrypoint: str = Field(default="", pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_registration(self) -> Self:
        """Reserve ADK's existing configuration surface and require custom callables."""
        if self.name == "adk":
            if self.model_fields_set != {"name"}:
                raise ValueError("the adk evaluation runner accepts only name")
        elif not self.entrypoint:
            raise ValueError("custom evaluation runner requires a module:function entrypoint")
        return self


class _EvaluationSpec(BaseModel):
    """Require an ordered, nonempty set of uniquely named runners."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    runners: list[RunnerSpec] = Field(min_length=1)


def read_runners(project: Path) -> list[RunnerSpec]:
    """Default legacy projects to ADK while rejecting malformed explicit selections."""
    config = project / "config.yaml"
    doc = _read_yaml(config, max_bytes=None) if config.exists() or config.is_symlink() else {}
    spec = _mapping(_mapping(doc, str(config)).get("spec", {}), "spec")
    if "evaluation" not in spec:
        return [RunnerSpec(name="adk")]
    runners = _EvaluationSpec.model_validate(spec["evaluation"]).runners
    if len({runner.name for runner in runners}) != len(runners):
        raise ValueError("spec.evaluation.runners requires unique names")
    return runners
