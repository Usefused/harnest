"""Provider-backed, reviewable code proposals with no automatic tool execution."""

from __future__ import annotations

import asyncio
import json
import os
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .catalog import catalog
from .files import inventory, read, source_path, validate
from harnest.provisioner_config import Deployment

SYSTEM = "Propose Harnest source edits as JSON for explicit user review."


class Prompt(BaseModel):
    """Make source-sharing explicit and bound model context and requested output."""
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str
    prompt: str = Field(min_length=1, max_length=16000)
    model: str = Field(default="", max_length=200)
    paths: list[str] = Field(default_factory=list, max_length=24)
    allow_related_source: bool = False
    allow_fused_discovery: bool = False


def settings() -> dict:
    """Expose model identifiers and readiness without returning any credential values."""
    return {"model": os.getenv("HARNEST_BUILDER_MODEL", ""), "configured": bool(os.getenv("HARNEST_BUILDER_MODEL"))}


async def propose(workspace, body: Prompt, completion=None, *, mcp=None, session="") -> dict:
    """Ask the configured provider for source changes and validate them before review."""
    model = body.model.strip() or settings()["model"]
    if not model:
        raise HTTPException(422, "Set HARNEST_BUILDER_MODEL or enter a LiteLLM model identifier, such as openai/your-model.")
    root = workspace.project(body.project)
    with workspace.lock:
        documents = [read(root, p) for p in dict.fromkeys(body.paths)]
        known = inventory(root)
    fused = {"available": bool(mcp), "connected": bool(mcp and mcp.connector.connected(session)),
             "discovery_allowed": body.allow_fused_discovery, "results": []}
    source_rounds = 0
    for attempt in range(9):
        context = _context(documents, known, body.allow_related_source, fused)
        content = await _complete(model, body.prompt, context, completion)
        payload = _payload(content)
        handled = await _fused_reply(payload, mcp, session, body, fused)
        if handled is not None:
            if handled:
                return handled
            continue
        missing = _requested_source(content, documents, known)
        if not missing:
            result = _proposal(root, content, documents, known)
            return {**result, "context_paths": [doc["path"] for doc in documents]}
        _expand_source(workspace, root, documents, missing, body.allow_related_source, source_rounds)
        source_rounds += 1
    raise HTTPException(422, "The model needs too many source-reading rounds. Select the relevant files and retry.")


async def _fused_reply(payload, service, session, body, fused):
    """Fulfil read-only native tool requests or retain a proposal for explicit browser review."""
    from .mcp_plans import MCPPlan
    from pydantic import ValidationError

    if "fused_discover" in payload:
        if not service or not body.allow_fused_discovery:
            raise HTTPException(422, "Enable Fused discovery to share service and operation metadata with the model.")
        result = await asyncio.to_thread(service.discover, session, payload["fused_discover"])
        fused["results"].append({"query": payload["fused_discover"], "result": result})
        return {}
    if "mcp_plan" not in payload:
        return None
    if not service or not isinstance(payload["mcp_plan"], dict):
        raise HTTPException(422, "MCP planning is unavailable.")
    proposed = payload["mcp_plan"]
    if "bearer" in proposed or "project" in proposed:
        raise HTTPException(422, "Model MCP plans cannot include credentials or choose another project.")
    try:
        plan = MCPPlan.model_validate({**proposed, "project": body.project})
    except ValidationError:
        raise HTTPException(422, "The model returned an invalid MCP plan. Choose a resource, owner and explicit operation scope.") from None
    return await asyncio.to_thread(service.prepare, session, plan)


def _context(documents: list[dict], known: list[str], allow_related_source: bool, fused=None) -> str:
    """Apply the same total context limit before every provider call, including expanded reads."""
    context = json.dumps({"files": documents, "project_files": known, "capabilities": catalog(),
                          "allow_related_source": allow_related_source,
                          "deployment_schema": Deployment.model_json_schema(), "fused": fused})
    if len(context.encode()) > 160000:
        raise HTTPException(413, "Select fewer source files; prompt context is limited to 160 KiB.")
    return context


def _json_content(content: str) -> str:
    """Accept one clearly delimited JSON block after narration, rejecting ambiguous replies."""
    text = content.strip()
    if text.startswith("{"):
        return text
    blocks = re.findall(r"^```([^\n]*)\n(.*?)^```[ \t]*$", text, re.MULTILINE | re.DOTALL)
    if len(blocks) != 1 or blocks[0][0].strip().lower() not in {"", "json"}:
        raise ValueError("Expected one JSON proposal")
    return blocks[0][1].strip()


def _payload(content: str) -> dict:
    """Decode one model response without treating provider text as instructions or executable code."""
    try:
        value = json.loads(_json_content(content))
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value
    except (ValueError, TypeError, AttributeError) as error:
        raise HTTPException(422, "The model did not return a valid source proposal. Try a smaller, more specific request.") from error


def _requested_source(content: str, documents: list[dict], known: list[str]) -> list[str]:
    """Discard guessed edits to unseen files and ask again only after reading their real contents."""
    payload = _payload(content)
    requested = _read_requests(payload, known)
    candidates = payload.get("files", [])
    if not isinstance(candidates, list):
        raise HTTPException(422, "The model must return an array of file changes.")
    # Some providers skip the reading protocol and propose unseen files directly.
    guessed = [item.get("path") for item in candidates if isinstance(item, dict) and item.get("path") in known]
    selected = {doc["path"] for doc in documents}
    return list(dict.fromkeys(path for path in [*requested, *guessed] if path not in selected))


def _read_requests(payload: dict, known: list[str]) -> list[str]:
    """Restrict model-requested reads to the bounded, source-only inventory shared with it."""
    requested = payload.get("read_files", [])
    if not isinstance(requested, list) or len(requested) > 24:
        raise HTTPException(422, "The model must request at most 24 project source files.")
    for path in requested:
        if not isinstance(path, str) or path not in known:
            raise HTTPException(422, "The model requested a file outside the available project source.")
    return requested


def _expand_source(workspace, root, documents: list[dict], missing: list[str], allowed: bool, attempt: int) -> None:
    """Read only authorized project source, preserving captured revisions for conflict-checked review."""
    if not allowed:
        raise HTTPException(422, "The model needs " + ", ".join(missing) + "; select these files or enable related source access and retry.")
    if attempt == 2 or len(documents) + len(missing) > 24:
        raise HTTPException(422, "The model needs more source than this request allows. Select the relevant files and retry.")
    with workspace.lock:
        documents.extend(read(root, path) for path in missing)


async def _complete(model: str, prompt: str, context: str, completion) -> str:
    """Call the compiled agent boundary and keep diagnostics from exposing credentials."""
    if completion is None:
        raise HTTPException(503, "The Harnest builder agent is not configured.")
    options = {"model": model, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": context}, {"role": "user", "content": prompt}], "timeout": 120, "max_tokens": 12000}
    if os.getenv("HARNEST_BUILDER_API_BASE"):
        options["api_base"] = os.environ["HARNEST_BUILDER_API_BASE"]
    if os.getenv("HARNEST_BUILDER_API_KEY"):
        options["api_key"] = os.environ["HARNEST_BUILDER_API_KEY"]
    try:
        response = await completion(**options)
        return response.choices[0].message.content or ""
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, _provider_failure(error)) from error


def _provider_failure(error: Exception) -> str:
    """Expose actionable categories without echoing provider payloads or credential-bearing URLs."""
    code = getattr(error, "status_code", None)
    messages = {
        401: "The model provider rejected the API key (HTTP 401). Configure a valid server-side provider key.",
        403: "The model provider denied access (HTTP 403). Check model permissions and provider account access.",
        404: "The provider could not find this model or endpoint (HTTP 404). Check the model identifier and API base URL.",
        429: "The model provider is rate-limiting this account or has no available quota (HTTP 429). Check your provider quota and retry.",
    }
    return messages.get(code, "The model request failed. Check the model identifier, provider API key, endpoint, and provider availability.")


def _proposal(root, content: str, documents: list[dict], known: list[str]) -> dict:
    """Bind proposed edits to the revisions actually shared with the model."""
    try:
        payload = _payload(content)
        candidates = payload["files"]
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 24:
            raise ValueError("Expected 1–24 files")
        selected = {doc["path"]: doc for doc in documents}
        changes = [_change(root, item, selected, known) for item in candidates]
        if len({c["path"] for c in changes}) != len(changes):
            raise ValueError("Duplicate file paths")
        return {"summary": str(payload.get("summary", "Review the proposed changes."))[:8000], "files": changes}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise HTTPException(422, "The model did not return a valid source proposal. Try a smaller, more specific request.") from error


def _change(root, item: dict, selected: dict, known: list[str]) -> dict:
    """Never let model output overwrite unseen source or bypass path and syntax checks."""
    path, text = item["path"], item["text"]
    if not isinstance(path, str) or not isinstance(text, str):
        raise ValueError("Source must be text")
    if path in known and path not in selected:
        raise HTTPException(422, f"The model wants to change {path}; add it to context and try again.")
    before = selected.get(path, {"text": "", "revision": ""})
    validate(source_path(root, path), text)
    return {"path": path, "text": text, "revision": before["revision"], "before": before["text"]}
