import asyncio
import inspect
import unittest

from harnest.client_tool import ClientToolError
from harnest.tool import client_tool


class ClientToolTests(unittest.TestCase):
    def test_decorator_preserves_typed_signature_and_never_runs_stub(self):
        called = False

        @client_tool
        def browser_open(url: str, *, new_tab: bool = False) -> dict[str, str]:
            """Open a URL in the connected browser."""

            nonlocal called
            called = True
            return {"title": "wrong"}

        self.assertEqual(
            str(inspect.signature(browser_open)),
            "(url: str, *, new_tab: bool = False) -> dict[str, str]",
        )
        with self.assertRaisesRegex(ClientToolError, "managed Harnest runtime"):
            asyncio.run(browser_open("https://example.test"))
        self.assertFalse(called)

    def test_description_and_timeout_are_validated(self):
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            client_tool(description="Open.", timeout_seconds=0)

        with self.assertRaisesRegex(ValueError, "docstring"):

            @client_tool
            def missing_docs(value: str) -> str:
                return value


if __name__ == "__main__":
    unittest.main()
