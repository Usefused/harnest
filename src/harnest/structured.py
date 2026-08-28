"""Portable Pydantic contracts for model and tool output."""

from __future__ import annotations

import inspect
from functools import lru_cache
from typing import Annotated, Any, Callable, Generic, TypeAlias, TypeVar, get_type_hints

from pydantic import BaseModel, ValidationError, create_model


PydanticModel: TypeAlias = type[BaseModel]
MetadataValue = TypeVar("MetadataValue")


class _FrameworkMetadataMarker:
    """Identify one field that Harnest, rather than the model, must produce."""


_FRAMEWORK_METADATA = _FrameworkMetadataMarker()


class FrameworkMetadata(Generic[MetadataValue]):
    """Mark an explicit output-model field as runtime-owned metadata.

    ``FrameworkMetadata[Details]`` remains a normal Pydantic ``Details`` field in
    the application's public result. Harnest omits it from provider-facing
    structured output and supplies it after the framework turn completes.
    """

    def __class_getitem__(cls, item: Any) -> Any:
        return Annotated[item, _FRAMEWORK_METADATA]


class StructuredOutputError(ValueError):
    """A configured Pydantic output contract was not satisfied."""


def validate_output_schema(value: Any, *, field_name: str) -> PydanticModel | None:
    """Require a Pydantic model class at an authored schema boundary."""

    if value is None:
        return None
    if not inspect.isclass(value) or not issubclass(value, BaseModel):
        raise TypeError(f"{field_name} must be a Pydantic BaseModel class")
    return value


def callable_output_schema(function: Callable[..., Any]) -> PydanticModel | None:
    """Return a directly or postponed-annotated Pydantic result model."""

    try:
        annotation = get_type_hints(function).get(
            "return", inspect.signature(function).return_annotation
        )
    except (NameError, SyntaxError, TypeError):
        # Unrelated unresolved annotations must not prevent ordinary tool
        # discovery; an explicit output_schema remains available to the user.
        annotation = inspect.signature(function).return_annotation
    if annotation is inspect.Signature.empty:
        return None
    try:
        return validate_output_schema(annotation, field_name="tool return annotation")
    except TypeError:
        return None


def validate_output_value(
    schema: PydanticModel, value: Any, *, boundary: str
) -> BaseModel:
    """Validate Python or JSON text output and redact input values from failures."""

    try:
        if isinstance(value, (str, bytes, bytearray)):
            return schema.model_validate_json(value)
        return schema.model_validate(value)
    except (ValidationError, ValueError, TypeError) as exc:
        # Pydantic errors include rejected input values. Public model and tool
        # boundaries report only the declared contract to avoid data leakage.
        raise StructuredOutputError(
            f"{boundary} does not match {schema.__name__}"
        ) from exc


def framework_metadata_field(schema: PydanticModel) -> str | None:
    """Return the single explicit runtime-owned field on an output model."""

    fields = [
        name
        for name, model_field in schema.model_fields.items()
        if _FRAMEWORK_METADATA in model_field.metadata
    ]
    if len(fields) > 1:
        raise TypeError(
            f"{schema.__name__} must declare at most one FrameworkMetadata field"
        )
    return fields[0] if fields else None


@lru_cache(maxsize=None)
def provider_output_schema(schema: PydanticModel) -> PydanticModel:
    """Derive the output contract a provider may generate for Harnest."""

    metadata_field = framework_metadata_field(schema)
    if metadata_field is None:
        return schema
    fields = {
        name: (model_field.annotation, model_field)
        for name, model_field in schema.model_fields.items()
        if name != metadata_field
    }
    # The derived model is intentionally provider-only. Final validation uses
    # the authored class so its model validators still guard the public result.
    return create_model(
        f"{schema.__name__}ProviderOutput",
        __config__=schema.model_config,
        **fields,
    )


def validate_runtime_output(
    schema: PydanticModel,
    value: Any,
    *,
    metadata: Any,
    boundary: str,
) -> BaseModel:
    """Validate provider output, inject declared metadata, then validate fully."""

    metadata_field = framework_metadata_field(schema)
    if metadata_field is None:
        return validate_output_value(schema, value, boundary=boundary)
    provider_model = validate_output_value(
        provider_output_schema(schema), value, boundary=boundary
    )
    payload = provider_model.model_dump(mode="python", by_alias=True)
    authored_field = schema.model_fields[metadata_field]
    payload[authored_field.alias or metadata_field] = metadata
    return validate_output_value(schema, payload, boundary=boundary)


__all__ = [
    "PydanticModel",
    "FrameworkMetadata",
    "StructuredOutputError",
    "callable_output_schema",
    "provider_output_schema",
    "framework_metadata_field",
    "validate_output_schema",
    "validate_output_value",
    "validate_runtime_output",
]
