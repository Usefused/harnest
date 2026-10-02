"""Inert UI contribution contracts shared by bundled and company Studio Packs."""

from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .files import read, source_path

ID = r"^[a-z][a-z0-9-]{0,62}$"
SLOTS = Literal["shell", "service", "welcome", "workspace.tabs", "inspector.sections"]


class UIContract(BaseModel):
    """Keep unknown UI contract fields from silently becoming compatibility promises."""
    model_config = ConfigDict(extra="forbid", strict=True)


class Theme(UIContract):
    """Allow bounded CSS values, never stylesheet syntax or external resource loads."""
    id: str = Field(pattern=ID)
    title: str = Field(min_length=1, max_length=120)
    tokens: dict[str, str] = Field(default_factory=dict, max_length=256)

    @model_validator(mode="after")
    def valid_tokens(self):
        """Restrict tokens to literal colors, dimensions, and font stacks."""
        for name, value in self.tokens.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) or name.startswith("studio-"):
                raise ValueError("Invalid or reserved theme token: " + name)
            if not re.fullmatch(r"[a-zA-Z0-9#.,% '\"_-]{1,256}", value):
                raise ValueError("Theme tokens must be literal CSS values")
        return self


class Layout(UIContract):
    """Rearrange stable shell regions without assuming their internal HTML structure."""
    id: str = Field(pattern=ID)
    title: str = Field(min_length=1, max_length=120)
    order: list[Literal["sidebar", "workspace", "inspector"]] = Field(default_factory=lambda: ["sidebar", "workspace", "inspector"])
    hidden: list[Literal["sidebar", "inspector"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_regions(self):
        """Keep the main workspace present and each region's placement unambiguous."""
        if len(self.order) != 3 or set(self.order) != {"sidebar", "workspace", "inspector"}:
            raise ValueError("Layout order must contain each Studio region exactly once")
        if len(self.hidden) != len(set(self.hidden)):
            raise ValueError("Duplicate hidden layout region")
        return self


class Contribution(UIContract):
    """Declare a trusted module or an isolated HTML panel at a stable UI slot."""
    id: str = Field(pattern=ID)
    title: str = Field(min_length=1, max_length=120)
    slot: SLOTS
    entry: str = Field(min_length=1, max_length=256)
    mode: Literal["module", "frame"] = "frame"
    replaces: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]*/[a-z][a-z0-9-]*$")
    permissions: list[Literal["state.project", "state.connection"]] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def valid_entry(self):
        """Only trusted modules may own the shell or register nonvisual services."""
        suffix = ".js" if self.mode == "module" else ".html"
        if not self.entry.endswith(suffix):
            raise ValueError("UI entry must end in " + suffix)
        if self.slot in {"shell", "service"} and self.mode != "module":
            raise ValueError("Shell and service contributions require a trusted module")
        return self


class UI(UIContract):
    """Version one composition API, independent of project resource installation."""
    apiVersion: Literal["harnest.dev/studio-ui/v1"]
    assets: list[str] = Field(default_factory=list, max_length=128)
    styles: list[str] = Field(default_factory=list, max_length=8)
    themes: list[Theme] = Field(default_factory=list, max_length=16)
    layouts: list[Layout] = Field(default_factory=list, max_length=16)
    contributions: list[Contribution] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_references(self):
        """Require every executable or stylesheet entry to have an explicit snapshot."""
        entries = [item.entry for item in self.contributions] + self.styles
        if not set(entries).issubset(self.assets) or any(not item.endswith(".css") for item in self.styles):
            raise ValueError("UI entries and CSS styles must reference declared assets")
        groups = [self.assets, [item.id for item in self.themes], [item.id for item in self.layouts], [item.id for item in self.contributions]]
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("Duplicate UI asset or contribution identity")
        return self


def snapshot_ui(root: Path, ui: UI | None) -> dict[str, str]:
    """Freeze bounded, explicitly named text without executing any publisher code."""
    assets = {}
    for relative in ui.assets if ui else []:
        if not relative.startswith("assets/"):
            raise ValueError("UI assets must live under assets/ to avoid project resource collisions")
        path = source_path(root, relative)
        if path.suffix not in {".html", ".js", ".css", ".json", ".txt"} or not path.is_file():
            raise ValueError("UI assets must be existing HTML, JavaScript, CSS, JSON, or text files")
        assets[relative] = read(root, relative)["text"]
        if sum(len(text.encode()) for text in assets.values()) > 8 * 1024 * 1024:
            raise ValueError("UI assets exceed 8 MiB")
    return assets
