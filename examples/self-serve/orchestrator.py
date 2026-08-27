"""Python deployment declaration for all self-serve agents in this project."""

orchestrator = define_orchestrator(
    agents=[AgentSource.directory("agents", exclude=("_*",))],
    parallelism=4,
    fail_fast=False,
    labels={"team": "customer-experience", "environment": "development"},
)
