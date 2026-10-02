"""Static consumer examples: public domains must retain concrete IDE-visible types."""

from typing import Any, Mapping, assert_type

import harnest
from harnest import context, memory
from harnest.context import AgentResponse, AgentSession, MemoryContext, ScopedAssets, SessionContext, StorageContext
from harnest.credentials import CredentialContext
from harnest.decision_runtime import DecisionContext
from harnest.extensions import ExtensionContext
from harnest.mcp import MCPContext
from harnest.skills import SkillAccess
from harnest.context_sandboxes import ScopedSandboxes


class CompanyService:
    """Example application-owned resource exposed by a context provider."""

    def lookup(self) -> str:
        """Return an application-specific value."""
        return "result"


class CompanyContext(ExtensionContext):
    """An organization's typed extension context."""


async def authored_tool() -> None:
    """Check author-facing chains without invoking runtime-bound context in tests."""
    assert_type(harnest.context.current(), context.AgentContext)
    assert_type(context.agent_name, str)
    assert_type(context.framework, str)
    assert_type(context.invocation_id, str)
    assert_type(context.session_id, str)
    assert_type(context.user_id, str)
    assert_type(context.metadata, Mapping[str, Any])
    assert_type(context.depth, int)
    assert_type(context.is_root, bool)
    assert_type(context.parent_agent_name, str | None)
    assert_type(context.assets, ScopedAssets)
    assert_type(context.credentials, CredentialContext)
    assert_type(context.storage, StorageContext)
    assert_type(context.memory, MemoryContext)
    assert_type(context.decisions, DecisionContext)
    assert_type(context.mcp, MCPContext)
    assert_type(context.skills, SkillAccess)
    assert_type(context.sandboxes, ScopedSandboxes)
    assert_type(context.session.namespace("company"), SessionContext)
    assert_type(await context.session.delete("value"), bool)
    session = await context.agent.create_session()
    assert_type(session, AgentSession)
    assert_type(context.resource("company", CompanyService), CompanyService)
    assert_type(context.current().resource("company", CompanyService), CompanyService)
    assert_type(context.storage("company", CompanyService), CompanyService)
    assert_type(context.storage.resource("company", CompanyService), CompanyService)
    assert_type(context.extensions("company", CompanyContext), CompanyContext)
    assert_type(memory.InMemoryStore(), memory.InMemoryStore)
    assert_type(harnest.memory.InMemoryStore(), memory.InMemoryStore)


def response_text(response: AgentResponse) -> str:
    """Resolve public lazy contract imports to their actual attributes."""
    return response.output_text


from harnest import lifecycle
from harnest.task import task, TaskHandle
from harnest.skills import SkillContext


@task
async def queued_lookup(key: str, *, limit: int = 10) -> str:
    return key


@task(queue="company")
def sync_lookup(key: str) -> int:
    return len(key)


@context.provider("company")
def company_provider() -> CompanyService:
    return CompanyService()


@lifecycle.model.before
def before_model(value: str) -> str:
    return value


@lifecycle.model.after(order=2)
def after_model(value: int) -> int:
    return value


@lifecycle.adk_plugin
def plugin_factory(value: str) -> CompanyService:
    return CompanyService()


@lifecycle.langgraph_middleware(order=1)
def middleware_factory(value: int) -> CompanyService:
    return CompanyService()


async def authored_decorators(skill: SkillContext) -> None:
    assert_type(await queued_lookup("key", limit=3), str)
    assert_type(sync_lookup("key"), int)
    assert_type(await queued_lookup.defer("key"), TaskHandle)
    assert_type(company_provider(), CompanyService)
    assert_type(before_model("request"), str)
    assert_type(after_model(1), int)
    assert_type(plugin_factory("plugin"), CompanyService)
    assert_type(middleware_factory(1), CompanyService)
    assert_type(skill.credentials, CredentialContext)
    assert_type(skill.storage, StorageContext)
    assert_type(skill.resource("company", CompanyService), CompanyService)
