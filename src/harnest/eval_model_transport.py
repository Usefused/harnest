"""Run-scoped ADK model adapters that borrow an agent's custom transport."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Iterator
import uuid

from .evaluation import EvaluationError
from .model_transport import model_transport_bindings


_ACTIVE_SCOPE: ContextVar[Any] = ContextVar("harnest_eval_model_transports", default=None)


def _model_owners(target: Any) -> Iterator[Any]:
    """Walk native ADK ownership and propagated managed graph metadata once."""

    pending = [target]
    visited: set[int] = set()
    while pending:
        value = pending.pop()
        if value is None or id(value) in visited:
            continue
        visited.add(id(value))
        yield value
        pending.extend(getattr(value, "sub_agents", ()) or ())
        pending.extend(
            getattr(value, name, None) for name in ("model", "root_agent")
        )


def _transport_bindings(target: Any) -> tuple[Any, ...]:
    """Deduplicate transports propagated through several enclosing targets."""

    found: dict[int, Any] = {}
    for owner in _model_owners(target):
        for binding in model_transport_bindings(owner):
            found[id(binding)] = binding
    return tuple(found.values())


def _select_binding(bindings: tuple[Any, ...], model: str) -> Any | None:
    """Prefer an exact model and otherwise require one compatible transport."""

    exact = [binding for binding in bindings if binding.model == model]
    provider, separator, _ = model.partition("/")
    compatible = [
        binding for binding in bindings
        if separator and binding.model.partition("/")[0] == provider
    ]
    candidates = exact or compatible
    if len(candidates) > 1:
        # Authenticated clients are authority boundaries. Picking a convenient
        # graph node could silently send evaluation data to the wrong gateway.
        raise EvaluationError(
            "ambiguous agent model transport for evaluation; select a model ID "
            "with one matching transport or use a single shared model transport"
        )
    return candidates[0] if candidates else None


class _EvalTransportScope:
    """Keep authenticated bindings out of ADK's process-wide model registry."""

    def __init__(self, bindings: tuple[Any, ...]) -> None:
        """Allocate a unique run namespace without retaining any global authority."""

        self.active = True
        self.bindings = bindings
        self.prefix = f"harnest_eval/{uuid.uuid4().hex}/"
        self.aliases: dict[str, tuple[str, Any]] = {}

    def alias(self, model: str) -> str:
        """Bind only models that can safely use an agent-owned transport."""

        binding = _select_binding(self.bindings, model)
        if binding is None:
            return model
        for alias, (original, candidate) in self.aliases.items():
            if original == model and candidate is binding:
                return alias
        alias = f"{self.prefix}{len(self.aliases)}"
        self.aliases[alias] = (model, binding)
        return alias

    def require(self, alias: str) -> tuple[str, Any]:
        """Reject escaped adapters and aliases belonging to another invocation."""

        if not self.active or _ACTIVE_SCOPE.get() is not self:
            raise EvaluationError("evaluation model transport scope is no longer active")
        if alias not in self.aliases:
            raise EvaluationError("evaluation model transport belongs to another run")
        return self.aliases[alias]

    def revoke(self) -> None:
        """Drop borrowed authority without closing the runtime-owned clients."""

        self.active = False
        self.bindings = ()
        self.aliases.clear()


@lru_cache(maxsize=1)
def _register_proxy_model() -> type[Any]:
    """Register one stateless Harnest namespace, never replace a provider mapping."""

    from google.adk.models import BaseLlm
    from google.adk.models.registry import LLMRegistry
    from pydantic import PrivateAttr

    class HarnestEvalTransportModel(BaseLlm):
        _scope: Any = PrivateAttr()
        _alias: str = PrivateAttr()
        _delegate: Any = PrivateAttr()

        def __init__(self, *, model: str, **kwargs: Any) -> None:
            """Construct a borrowing adapter only inside its authorized eval run."""

            scope = _ACTIVE_SCOPE.get()
            if scope is None:
                raise EvaluationError("evaluation model transport scope is unavailable")
            original, binding = scope.require(model)
            super().__init__(model=original, **kwargs)
            self._scope = scope
            self._alias = model
            self._delegate = binding.build_eval_model(original)

        @classmethod
        def supported_models(cls) -> list[str]:
            """Reserve only Harnest's opaque evaluation alias namespace."""

            return [r"^harnest_eval/[a-f0-9]{32}/[0-9]+$"]

        @property
        def capabilities(self) -> Any:
            """Preserve the original adapter's schema and tool capabilities."""

            return self._delegate.capabilities

        async def generate_content_async(self, llm_request: Any, stream: bool = False):
            """Forward copied requests through the borrowed client without aliases."""

            original, _ = self._scope.require(self._alias)
            request = llm_request.model_copy(update={"model": original})
            async for response in self._delegate.generate_content_async(request, stream):
                self._scope.require(self._alias)
                yield response

    LLMRegistry.register(HarnestEvalTransportModel)
    return HarnestEvalTransportModel


def _bind_criterion(criterion: Any, scope: _EvalTransportScope) -> Any:
    """Replace judge IDs in a copied criterion while preserving all other options."""

    if isinstance(criterion, (int, float)):
        return criterion
    payload = criterion.model_dump(by_alias=True)
    options = payload.get("judgeModelOptions", payload.get("judge_model_options"))
    if isinstance(options, dict):
        key = "judgeModel" if "judge_model" not in options else "judge_model"
        if isinstance(options.get(key), str):
            options[key] = scope.alias(options[key])
    return type(criterion).model_validate(payload)


@contextmanager
def eval_model_transports(target: Any, config: Any) -> Iterator[Any]:
    """Lend compatible agent transports to judge and simulator models for one run."""

    bindings = _transport_bindings(target)
    if not bindings:
        yield config
        return
    _register_proxy_model()
    scope = _EvalTransportScope(bindings)
    token = _ACTIVE_SCOPE.set(scope)
    try:
        prepared = config.model_copy(deep=True)
        prepared.criteria = {
            name: _bind_criterion(criterion, scope)
            for name, criterion in prepared.criteria.items()
        }
        simulator = prepared.user_simulator_config
        if simulator is not None and isinstance(getattr(simulator, "model", None), str):
            simulator.model = scope.alias(simulator.model)
        yield prepared
    finally:
        # Revocation also protects background tasks that inherited a copy of
        # this context; resetting the ContextVar alone would leave them usable.
        scope.revoke()
        _ACTIVE_SCOPE.reset(token)


def restore_eval_model_names(value: Any) -> Any:
    """Remove internal aliases from structured diagnostic results before persistence."""

    scope = _ACTIVE_SCOPE.get()
    if scope is None:
        return value
    if isinstance(value, str):
        original = scope.aliases.get(value)
        return original[0] if original else value
    if isinstance(value, dict):
        return {key: restore_eval_model_names(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_eval_model_names(item) for item in value]
    return value


async def close_owned_eval_model_transports(
    target: Any, *, primary_error: BaseException | None = None
) -> None:
    """Close CLI-owned model resources once after all borrowing eval calls finish."""

    from .model_lifecycle import _lifecycle_resources, _raise_cleanup_failures

    resources = {
        id(resource): resource
        for owner in _model_owners(target)
        for resource in _lifecycle_resources(owner)
    }
    failures = []
    for resource in reversed(tuple(resources.values())):
        try:
            await resource.aclose()
        except BaseException as error:
            failures.append(error)
    if failures and primary_error is not None:
        # Cleanup must not replace the scored failure or cancellation that
        # caused shutdown, and exception text may contain provider credentials.
        add_note = getattr(primary_error, "add_note", None)
        if callable(add_note):
            add_note("evaluation model transport cleanup also failed")
    elif failures:
        _raise_cleanup_failures(failures)
