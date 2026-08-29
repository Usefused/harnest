"""Shared invocation coordination for every framework-neutral transport."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
import uuid

from pydantic import ValidationError
from starlette.exceptions import HTTPException

from .approval import InMemoryApprovalStore
from .assets import AssetScope, AssetStore
from .client_tool import InMemoryClientToolStore
from .content_validation import ContentValidationError, resolve_model_content
from .runtime_continuation import (
    completed_payload,
    json_client_requires_action,
    json_requires_action,
    next_run_boundary,
    start_approval_run,
)
from .runtime_contract import (
    InvocationRequest,
    InvocationResult,
    NoCustomerFacingOutputError,
    RuntimeDriver,
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
        if kind == "error":
            raise value
        if kind != "result":
            raise RuntimeError("unexpected approval run notification")
        return None

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
