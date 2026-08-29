"""Shared invocation coordination for every framework-neutral transport."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from pydantic import ValidationError
from starlette.exceptions import HTTPException

from .approval import InMemoryApprovalStore
from .assets import AssetScope, AssetStore
from .client_tool import InMemoryClientToolStore
from .checkpoint import RunRecord
from .content_validation import ContentValidationError, resolve_model_content
from .runtime_continuation import (
    completed_payload,
    external_in_progress_payload,
    json_client_requires_action,
    json_requires_action,
    next_run_boundary,
    start_approval_run,
)
from .external_continuation import (
    ExternalContinuationFailed,
    PendingExternalContinuation,
)
from .runtime_contract import (
    InvocationRequest,
    InvocationResult,
    NoCustomerFacingOutputError,
    RuntimeDriver,
    SessionMessage,
    require_customer_facing_output,
)
from .server_config import format_byte_size


async def resolved_transport_input(
    value: Any,
    input_schema: Any,
    store: AssetStore,
    scope: AssetScope,
    stores: Mapping[str, AssetStore] | None = None,
) -> Any:
    """Run structural Pydantic validation before trusted metadata resolution."""

    if input_schema is None:
        return value
    try:
        model = (
            value
            if isinstance(value, input_schema)
            else input_schema.model_validate(value)
        )
        resolved = await resolve_model_content(
            model, store=store, stores=stores, scope=scope
        )
        return resolved.model_dump(mode="json", by_alias=True)
    except (ValidationError, ContentValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid content input") from exc


@dataclass(slots=True)
class InvocationCoordinator:
    """Own common session, input, limit, and JSON invocation policy."""

    driver: RuntimeDriver
    approvals: InMemoryApprovalStore
    client_tools: InMemoryClientToolStore
    assets: AssetStore
    asset_stores: Mapping[str, AssetStore]
    semaphore: asyncio.Semaphore
    request_timeout: float
    max_request_bytes: int
    external_continuations: Any | None = None

    async def resolve_session(self, *, user_id: str, session_id: str | None) -> Any:
        """Create an implicit session or authorize one explicit session."""

        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise HTTPException(status_code=400, detail="sessionId must be non-empty")
        if session_id is None:
            return await self.driver.create_session(
                session_id=uuid.uuid4().hex,
                user_id=user_id,
                state={},
            )
        session = await self.driver.get_session(
            session_id=session_id,
            user_id=user_id,
        )
        if session is None:
            # Ownership failures intentionally share the not-found response so
            # callers cannot probe another principal's session identifiers.
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    async def resolve_input(
        self,
        value: Any,
        *,
        user_id: str,
        session_id: str,
        require_non_empty_text: bool,
    ) -> Any:
        """Enforce transport limits and resolve content within session scope."""

        self.validate_input(
            value,
            require_non_empty_text=require_non_empty_text,
        )
        return await self._resolve_content(
            value,
            user_id=user_id,
            session_id=session_id,
        )

    async def _resolve_content(
        self,
        value: Any,
        *,
        user_id: str,
        session_id: str,
    ) -> Any:
        """Resolve trusted content metadata only after session authorization."""

        return await resolved_transport_input(
            value,
            self.driver.info.input_schema,
            self.assets,
            AssetScope(user_id=user_id, session_id=session_id),
            stores=self.asset_stores,
        )

    def validate_input(self, value: Any, *, require_non_empty_text: bool) -> None:
        """Reject invalid plain input and oversized text before session mutation."""

        if self.driver.info.input_schema is None and require_non_empty_text and (
            not isinstance(value, str) or not value.strip()
        ):
            raise HTTPException(status_code=400, detail="Input must be non-empty")
        if isinstance(value, str) and len(value.encode("utf-8")) > self.max_request_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Input exceeds {format_byte_size(self.max_request_bytes)}",
            )

    def create_request(
        self,
        value: Any,
        *,
        user_id: str,
        session_id: str,
        metadata: Mapping[str, Any],
        transport: str,
        invocation_id: str | None = None,
    ) -> InvocationRequest:
        """Build one portable request after transport-owned authorization."""

        return InvocationRequest(
            input=value,
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id or f"resp_{uuid.uuid4().hex}",
            metadata=dict(metadata),
            state_delta={},
            transport=transport,
        )

    async def prepare_request(
        self,
        value: Any,
        *,
        user_id: str,
        session_id: str | None,
        metadata: Mapping[str, Any],
        transport: str,
        require_non_empty_text: bool = True,
        validate_before_session: bool = True,
    ) -> InvocationRequest:
        """Resolve a caller-owned session and normalized input into one request."""

        if validate_before_session:
            # `/responses` historically rejects its envelope and text limit
            # before implicit session creation. Custom routes were introduced
            # with the inverse order, so their observable behavior stays intact.
            self.validate_input(
                value,
                require_non_empty_text=require_non_empty_text,
            )
        session = await self.resolve_session(user_id=user_id, session_id=session_id)
        if validate_before_session:
            resolved = await self._resolve_content(
                value,
                user_id=user_id,
                session_id=session.id,
            )
        else:
            resolved = await self.resolve_input(
                value,
                user_id=user_id,
                session_id=session.id,
                require_non_empty_text=require_non_empty_text,
            )
        return self.create_request(
            resolved,
            user_id=user_id,
            session_id=session.id,
            metadata=metadata,
            transport=transport,
        )

    async def invoke_json(self, request: InvocationRequest) -> dict[str, Any]:
        """Run one JSON invocation through shared continuation state."""

        run = start_approval_run(
            self.approvals,
            self.client_tools,
            self.driver,
            request,
            stream=False,
            external_continuations=self.external_continuations,
        )
        try:
            async with self.semaphore:
                # Activation happens inside the concurrency boundary so queued
                # work cannot consume driver or downstream capacity early.
                run.activation.set()
                kind, value = await next_run_boundary(
                    run,
                    timeout=self.request_timeout,
                )
                response = self._boundary_response(kind, value, request=request)
                if response is not None:
                    return response
                result = value
                require_customer_facing_output(result.text, result.result)
        except asyncio.TimeoutError as exc:
            self.approvals.cancel_run(run)
            raise HTTPException(status_code=504, detail="Response timed out") from exc
        except NoCustomerFacingOutputError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return self._completed_json(result, request=request)

    async def poll_json(
        self, *, response_id: str, user_id: str, session_id: str
    ) -> dict[str, Any]:
        """Read one principal/session-scoped external continuation response."""

        if self.external_continuations is None:
            raise HTTPException(status_code=404, detail="Response not found")
        try:
            kind, value = await self.external_continuations.response_boundary(
                response_id=response_id,
                user_id=user_id,
                session_id=session_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Response not found") from exc
        if kind == "final":
            return value
        if kind == "durable_terminal":
            return await self._durable_terminal_json(
                value,
                response_id=response_id,
                user_id=user_id,
                session_id=session_id,
            )
        if kind == "external_continuation":
            return self._external_in_progress(
                value, response_id=response_id, session_id=session_id
            )
        if kind == "error":
            payload = self._failed_json(value, response_id, session_id)
            self._retain_final(payload, response_id, user_id, session_id)
            return payload
        boundary = self._boundary_response(
            kind,
            value,
            request=InvocationRequest(
                input="",
                user_id=user_id,
                session_id=session_id,
                invocation_id=response_id,
                metadata={},
                state_delta={},
            ),
        )
        if boundary is not None:
            return boundary
        require_customer_facing_output(value.text, value.result)
        payload = self._completed_json(
            value,
            request=InvocationRequest(
                input="",
                user_id=user_id,
                session_id=session_id,
                invocation_id=response_id,
                metadata={},
                state_delta={},
            ),
        )
        self._retain_final(payload, response_id, user_id, session_id)
        return payload

    async def _durable_terminal_json(
        self,
        run: RunRecord,
        *,
        response_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Rebuild a terminal response when another replica owned execution."""

        result = (
            await self._reconstruct_result(run, user_id=user_id)
            if run.status == "completed"
            else None
        )
        if result is None:
            payload = self._failed_json(
                RuntimeError("durable response unavailable"),
                response_id,
                session_id,
            )
        else:
            try:
                require_customer_facing_output(result.text, result.result)
            except NoCustomerFacingOutputError as error:
                payload = self._failed_json(error, response_id, session_id)
            else:
                payload = self._completed_json(
                    result,
                    request=self.create_request(
                        "",
                        user_id=user_id,
                        session_id=session_id,
                        metadata={},
                        transport="continuation_poll",
                        invocation_id=response_id,
                    ),
                )
        self._retain_final_if_present(payload, response_id, user_id, session_id)
        return payload

    async def _reconstruct_result(
        self, run: RunRecord, *, user_id: str
    ) -> InvocationResult | None:
        """Read one stable final assistant message from the committed session."""

        messages = await self.driver.get_session_messages(
            session_id=run.session_id,
            user_id=user_id,
        )
        session = await self.driver.get_session(
            session_id=run.session_id,
            user_id=user_id,
        )
        if messages is None or session is None:
            return None
        # A later turn makes "last assistant" ambiguous. Failing closed avoids
        # returning another invocation's output under this response identifier.
        if _timestamp_after(session.updated_at, run.updated_at):
            return None
        return _result_from_final_message(messages, run.session_id)

    @staticmethod
    def _boundary_response(
        kind: str,
        value: Any,
        *,
        request: InvocationRequest,
    ) -> dict[str, Any] | None:
        """Return a suspension payload or propagate a terminal run boundary."""

        if kind == "approval":
            return json_requires_action(
                value,
                response_id=request.invocation_id,
                session_id=request.session_id,
            )
        if kind == "client_tool":
            return json_client_requires_action(
                value,
                response_id=request.invocation_id,
                session_id=request.session_id,
            )
        if kind == "external_continuation":
            return InvocationCoordinator._external_in_progress(
                value,
                response_id=request.invocation_id,
                session_id=request.session_id,
            )
        if kind == "error":
            raise value
        if kind != "result":
            raise RuntimeError("unexpected approval run notification")
        return None

    @staticmethod
    def _external_in_progress(
        pending: PendingExternalContinuation,
        *,
        response_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Render an opaque wait without exposing provider or external job ids."""

        payload = external_in_progress_payload(
            pending,
            response_id=response_id,
            session_id=session_id,
            sequence=0,
        )
        payload.pop("type")
        payload.pop("sequence")
        payload["id"] = payload.pop("responseId")
        return payload

    @staticmethod
    def _failed_json(
        error: BaseException, response_id: str, session_id: str
    ) -> dict[str, Any]:
        """Detach provider and framework error details from status polling."""

        code = (
            "external_continuation_failed"
            if isinstance(error, ExternalContinuationFailed)
            else "invocation_failed"
        )
        return {
            "id": response_id,
            "sessionId": session_id,
            "status": "failed",
            "error": {"code": code},
            "outputText": "",
            "output": [],
            "metadata": {},
        }

    def _retain_final(
        self,
        payload: dict[str, Any],
        response_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        """Delegate bounded response ownership to the continuation runtime."""

        self.external_continuations.retain_final(
            response_id=response_id,
            user_id=user_id,
            session_id=session_id,
            payload=payload,
        )

    def _retain_final_if_present(
        self,
        payload: dict[str, Any],
        response_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        """Cache when this replica owns local state; durable reads need none."""

        try:
            self._retain_final(payload, response_id, user_id, session_id)
        except KeyError:
            # Callback-only replicas reconstruct from tenant-scoped durable
            # state and intentionally have no process-local response tombstone.
            return

    @staticmethod
    def _completed_json(
        result: InvocationResult,
        *,
        request: InvocationRequest,
    ) -> dict[str, Any]:
        """Adapt one successful result to the established JSON response shape."""

        completed = completed_payload(
            response_id=request.invocation_id,
            session_id=result.session_id,
            sequence=0,
            events=result.events,
            text=result.text,
            metadata=result.metadata,
            result=result.result,
        )
        completed.pop("type")
        completed.pop("sequence")
        completed["id"] = completed.pop("responseId")
        return completed


def _timestamp_after(left: str | None, right: str | None) -> bool:
    """Treat missing or malformed ordering evidence as unsafe reconstruction."""

    if left is None or right is None:
        return True
    try:
        return datetime.fromisoformat(left) > datetime.fromisoformat(right)
    except (TypeError, ValueError):
        return True


def _result_from_final_message(
    messages: Sequence[SessionMessage], session_id: str
) -> InvocationResult | None:
    """Project only the final assistant item so an older turn cannot leak."""

    if not messages or not isinstance(messages[-1], SessionMessage):
        return None
    message = messages[-1]
    if message.role != "assistant":
        return None
    text, result = _portable_message_output(message.content)
    if not text and result is None:
        return None
    events = (
        ({"type": "message", "role": "assistant", "text": text},)
        if text
        else ({"type": "graph_output", "output": result},)
    )
    return InvocationResult(
        text=text,
        events=events,
        result=result,
        session_id=session_id,
        metadata={},
    )


def _portable_message_output(content: Any) -> tuple[str, Any]:
    """Preserve structured portable content while extracting visible text."""

    if isinstance(content, str):
        return content, None
    if isinstance(content, list):
        text = "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, Mapping)
            and block.get("type") in {"text", "output_text"}
        )
        structured = any(
            not isinstance(block, Mapping)
            or block.get("type") not in {"text", "output_text"}
            for block in content
        )
        return text, content if structured else None
    return "", content
