"""Invalid consumer calls that must fail static checking instead of becoming Any."""

from harnest import context


class CompanyService:
    """A company resource with no dynamically resolved members."""


async def invalid_tool() -> None:
    """Deliberately misuse each public chain to exercise the checker diagnostics."""
    await context.agent.create_session(state=123)
    await context.session.misspelled_method()
    context.resource("company", CompanyService).misspelled_method()
