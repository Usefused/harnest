"""Review-bound MCP provisioning shared by Studio controls and its model request boundary."""

from contextlib import contextmanager
from dataclasses import dataclass
import secrets
from threading import RLock
import time

from fastapi import HTTPException
from harnest.fused_connectors import FusedConnectorsService
from harnest.logging import get_logger

from .files import read
from .mcp_plans import configuration, endpoint, source_changes, validate_services

AUDIT = get_logger("studio.mcp.audit")


@contextmanager
def mutation(operation):
    """Report committed mutations and failures without source, endpoint, or credential payloads."""
    try:
        yield
    except Exception:
        AUDIT.warning("mcp.failed", operation=operation, trigger="user", outcome="failed")
        raise
    else:
        AUDIT.info("mcp.committed", operation=operation, trigger="user", outcome="committed")


@dataclass
class Review:
    """Retain immutable reviewed inputs and private connection results for safe local retries."""
    plan: object
    session: str
    files: list
    config: dict | None
    url_env: str
    token_env: str
    expires: float
    owner_revision: str
    url: str = ""
    credential: str = ""
    state: str = "pending"


class MCPService:
    """Own review lifetimes; model calls cannot execute externally mutating operations."""

    def __init__(self, workspace, jobs, credentials, connector=None):
        """Use one shared Fused backend with injected dependencies for transport tests."""
        self.workspace, self.jobs, self.credentials = workspace, jobs, credentials
        self.connector = connector or FusedConnectorsService()
        self.reviews = {}
        self.lock = RLock()

    def discover(self, session, query):
        """Expose a finite read-only discovery vocabulary, never a generic Admin proxy."""
        if not isinstance(query, dict):
            raise HTTPException(422, "Discovery expects an action object.")
        action = query.get("action")
        if action == "services":
            return self.connector.services(session)
        if action == "operations":
            return self.connector.operations(session, str(query.get("service_id", "")), str(query.get("version", "")))
        if action == "servers":
            offset = query.get("offset", 0)
            if not isinstance(offset, int) or not 0 <= offset <= 10000:
                raise HTTPException(422, "Invalid discovery offset.")
            return self.connector.servers(session, limit=50, offset=offset)
        raise HTTPException(422, "Choose services, operations, or servers for discovery.")

    def prepare(self, session, plan):
        """Validate discovery and source revisions without deploying or issuing tokens."""
        with self.lock, self.workspace.lock:
            self.reviews = {key: item for key, item in self.reviews.items() if item.expires > time.time()}
            if len(self.reviews) >= 32:
                raise HTTPException(429, "Too many pending MCP reviews; wait for old reviews to expire.")
            config, url = self._resolve_plan(session, plan)
            files, url_env, token_env = source_changes(self.workspace.project(plan.project), plan, plan.kind != "http" or bool(plan.bearer))
            review = Review(plan, session, files, config, url_env, token_env, time.time() + 1800, read(self.workspace.project(plan.project), plan.owner)["revision"], url=url, credential=plan.bearer)
            identity = secrets.token_urlsafe(24)
            self.reviews[identity] = review
            return {"mcp_review": identity, "summary": "Review MCP provisioning and project changes before applying.",
                    "files": files, "connection": {"kind": plan.kind, "name": plan.name or plan.resource, "url": url,
                    "owner": plan.owner, "owner_team": plan.owner_team, "configuration": config,
                    "credential_variables": [url_env, token_env] if plan.kind != "http" or plan.bearer else [url_env],
                    "token_lifetime_days": 7 if plan.kind != "http" else None}}

    def _resolve_plan(self, session, plan):
        """Resolve actual discovered endpoints and explicitly selected service scopes."""
        if plan.kind == "http":
            return None, endpoint(plan.url)
        self.connector._require_token(session)
        if plan.kind == "existing":
            return None, endpoint(self.connector.resolve(session, plan.name, plan.version, plan.server_offset))
        config = configuration(plan)
        validate_services(self.connector, session, plan)
        return config, ""

    def apply(self, session, identity):
        """Execute a reviewed plan once, with revision checks before any remote side effect."""
        with self.lock, self.workspace.lock, self.jobs.lock:
            review = self._review(session, identity)
            if review.state == "applied":
                return {"applied": True, "project": review.plan.project}
            self.jobs.ensure_idle()
            root = self.workspace.project(review.plan.project)
            if read(root, review.plan.owner)["revision"] != review.owner_revision:
                raise HTTPException(409, "The owning agent changed. Prepare a new MCP review.")
            self.workspace._preflight(root, review.files)
            if review.state == "pending":
                self._connect(review)
            bindings = {review.url_env: review.url}
            if review.credential:
                bindings[review.token_env] = review.credential
            # Keep the credential on the host even if a local source write fails;
            # retry uses this same connection and never repeats provisioning.
            with mutation("store_credentials"):
                self.credentials.save(root, bindings)
            with mutation("save_connection"):
                self.workspace.apply(review.plan.project, review.files)
            review.state = "applied"
            review.credential = ""
            review.plan.bearer = ""
            return {"applied": True, "project": review.plan.project, "restart_required": True}

    def _review(self, session, identity):
        """Reject cross-browser, expired, and uncertain mutation attempts instead of replaying."""
        review = self.reviews.get(identity)
        if review is None or review.session != session or review.expires <= time.time():
            raise HTTPException(409, "This MCP review expired. Prepare a new review.")
        if review.state == "failed":
            raise HTTPException(409, "The remote result is uncertain. Check Fused servers before preparing a new review; this action will not be replayed.")
        return review

    def _connect(self, review):
        """Retain successful provision/token results and never retry ambiguous external mutations."""
        if review.plan.kind == "http":
            review.state = "connected"
            return
        review.state = "failed"
        if review.config:
            with mutation("deploy_mcp"):
                result = self.connector.deploy(review.session, review.config, review.plan.owner_team)
                review.url = endpoint(result["url"])
        with mutation("issue_execution_token"):
            review.credential = self.connector.issue(review.session, review.plan.name, "studio-" + secrets.token_hex(6))
            if not review.credential:
                raise HTTPException(502, "Fused returned no execution credential. Check the server before reconnecting.")
        review.state = "connected"
