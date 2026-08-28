import asyncio
import inspect
from types import SimpleNamespace
import unittest

from pydantic import BaseModel

from harnest.client_tool import client_tool_execution
from harnest.structured import (
    FrameworkMetadata,
    StructuredOutputError,
    provider_output_schema,
    validate_runtime_output,
)
from harnest.tool import client_tool, tool


class Result(BaseModel):
    count: int


class NativeMetadata(BaseModel):
    framework: str


class ResultWithMetadata(BaseModel):
    count: int
    metadata: FrameworkMetadata[NativeMetadata]


class StructuredOutputTests(unittest.TestCase):
    def test_tool_output_schema_validates_sync_and_async_results(self):
        @tool(output_schema=Result)
        def count_sync() -> dict[str, int]:
            """Count synchronously."""

            return {"count": 2}

        @tool
        async def count_async() -> Result:
            """Count asynchronously."""

            return {"count": 3}  # type: ignore[return-value]

        self.assertEqual(count_sync(), Result(count=2))
        self.assertEqual(asyncio.run(count_async()), Result(count=3))
        self.assertIs(inspect.signature(count_sync).return_annotation, Result)
        self.assertIs(inspect.signature(count_async).return_annotation, Result)

    def test_postponed_pydantic_return_annotation_is_resolved(self):
        def count() -> Result:
            """Count through a postponed annotation."""

            return {"count": 5}  # type: ignore[return-value]

        count.__annotations__["return"] = "Result"
        decorated = tool(count)

        self.assertEqual(decorated(), Result(count=5))

    def test_tool_output_schema_redacts_rejected_values(self):
        @tool(output_schema=Result)
        def invalid() -> dict[str, str]:
            """Return an invalid count."""

            return {"count": "private-value"}

        with self.assertRaisesRegex(
            StructuredOutputError, "tool 'invalid' output does not match Result"
        ) as raised:
            invalid()
        self.assertNotIn("private-value", str(raised.exception))

    def test_client_tool_validates_the_submitted_output(self):
        @client_tool(output_schema=Result)
        def remote_count() -> dict[str, int]:
            """Ask the client to count."""

            raise AssertionError("client tool declarations do not execute")

        class Execution:
            async def execute(self, **_kwargs):
                return {"count": 4}

        with client_tool_execution(SimpleNamespace(execute=Execution().execute)):
            result = asyncio.run(remote_count())

        self.assertEqual(result, Result(count=4))
        self.assertIs(inspect.signature(remote_count).return_annotation, Result)

    def test_schema_configuration_requires_a_pydantic_model_class(self):
        with self.assertRaisesRegex(TypeError, "Pydantic BaseModel class"):
            tool(output_schema=dict)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "Pydantic BaseModel class"):
            client_tool(output_schema=Result(count=1))  # type: ignore[arg-type]

    def test_runtime_metadata_is_explicit_but_not_provider_generated(self):
        provider_schema = provider_output_schema(ResultWithMetadata)

        self.assertNotIn("metadata", provider_schema.model_fields)
        self.assertIn("metadata", ResultWithMetadata.model_fields)
        self.assertEqual(
            validate_runtime_output(
                ResultWithMetadata,
                {"count": 2},
                metadata={"framework": "adk"},
                boundary="model output",
            ),
            ResultWithMetadata(
                count=2, metadata=NativeMetadata(framework="adk")
            ),
        )

    def test_runtime_metadata_is_not_injected_without_the_marker(self):
        self.assertIs(provider_output_schema(Result), Result)
        self.assertEqual(
            validate_runtime_output(
                Result,
                {"count": 2},
                metadata={"framework": "adk"},
                boundary="model output",
            ),
            Result(count=2),
        )

    def test_output_model_may_declare_only_one_framework_metadata_field(self):
        class InvalidResult(BaseModel):
            first: FrameworkMetadata[NativeMetadata]
            second: FrameworkMetadata[NativeMetadata]

        with self.assertRaisesRegex(TypeError, "at most one FrameworkMetadata"):
            provider_output_schema(InvalidResult)


if __name__ == "__main__":
    unittest.main()
