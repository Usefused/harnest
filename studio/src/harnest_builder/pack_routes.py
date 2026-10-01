"""Authenticated pack discovery and capability-bound review/apply endpoints."""

from contextlib import contextmanager

from fastapi import HTTPException
from pydantic import Field

from .folders import directory
from .files import name
from .pack_manifest import Contract


class Selection(Contract):
    """Select catalog identities without accepting executable or arbitrary filesystem content."""
    resources: list[str] = Field(min_length=1, max_length=32)
    project: str | None = None
    directory: str | None = None
    name: str | None = None


class Apply(Contract):
    """Apply the exact server-owned proposal previously presented for review."""
    review: str


@contextmanager
def translated():
    """Present catalog errors without leaking tracebacks through the local HTTP API."""
    try:
        yield
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, str(error)) from error


def install_routes(app, workspace, jobs, packs):
    """Reuse the loopback authorization and job serialization boundaries."""
    @app.get("/api/packs")
    def catalog():
        """Expose immutable company resources and manifests without source contents."""
        return {"packs": [pack["manifest"] for pack in packs.packs.values()], "resources": packs.catalog()}

    @app.post("/api/packs/preview")
    def preview(body: Selection):
        """Bind a reviewed diff to either an existing project or an explicit new directory."""
        with workspace.lock, translated():
            if body.project is not None:
                if body.directory or body.name:
                    raise ValueError("Choose an existing project or a new template destination")
                proposal = packs.preview(workspace.project(body.project), body.resources)
                return packs.remember(proposal, project=body.project)
            if not body.directory or not body.name:
                raise ValueError("Templates require a parent directory and agent name")
            destination = directory(body.directory) / name(body.name)
            if destination.exists():
                raise HTTPException(409, "The agent folder already exists")
            selected = packs.expand(body.resources)
            if not any(packs.resources[key]["kind"] == "template" for key in selected):
                raise ValueError("New projects require an agent template")
            proposal = packs.preview(destination, body.resources)
            return packs.remember(proposal, destination=destination)

    @app.post("/api/packs/apply")
    def apply(body: Apply):
        """Commit a reviewed install only while no CLI process owns project mutation."""
        with workspace.lock, jobs.lock, translated():
            jobs.ensure_idle()
            return packs.apply(workspace, body.review)
