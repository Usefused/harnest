"""Decorators must keep invalid authored arguments visible to the IDE."""

from public_api import queued_lookup, sync_lookup, company_provider, before_model, after_model, plugin_factory, middleware_factory


async def invalid_calls() -> None:
    await queued_lookup(123)
    sync_lookup(123)
    company_provider(123)
    before_model(123)
    after_model("invalid")
    plugin_factory(123)
    middleware_factory("invalid")
