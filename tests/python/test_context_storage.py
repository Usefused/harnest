from __future__ import annotations

from dataclasses import dataclass
import unittest

from harnest.context import activate_context, context, create_agent_context, revoke_context
from harnest.context_storage import StorageContext


@dataclass
class UsersRepository:
    name: str


def _active(*, stores: dict[str, object]):
    return create_agent_context(
        framework="langgraph",
        agent_name="support",
        invocation_id="invoke-1",
        user_id="user-1",
        session_id="session-1",
        metadata={},
        resources={},
        custom_stores=stores,
    )


class ContextStorageTests(unittest.TestCase):
    def test_context_storage_resolves_named_custom_resource(self) -> None:
        repository = UsersRepository("users")
        active = _active(stores={"state-db": repository})

        with activate_context(active):
            self.assertIsInstance(context.storage, StorageContext)
            self.assertIs(context.storage("state-db"), repository)
            self.assertIs(
                context.storage.resource("state-db", UsersRepository), repository
            )

        revoke_context(active)

    def test_context_storage_never_exposes_private_framework_authorities(self) -> None:
        active = _active(stores={})

        with activate_context(active):
            with self.assertRaisesRegex(LookupError, "not available"):
                context.storage("sessions")
            with self.assertRaisesRegex(LookupError, "not available"):
                context.storage("checkpoints")

        revoke_context(active)

    def test_context_storage_fails_after_invocation_revocation(self) -> None:
        active = _active(stores={"users": UsersRepository("users")})

        with activate_context(active):
            facade = context.storage
            revoke_context(active)
            with self.assertRaisesRegex(RuntimeError, "managed invocation"):
                facade("users")


if __name__ == "__main__":
    unittest.main()
