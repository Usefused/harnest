package main

// scaffoldLibraryGuide explains the optional compiler-owned helper namespace.
func scaffoldLibraryGuide() string {
	return `# Reusable Python helpers

Optional: Yes. Delete this folder if the agent has no shared Python helpers.

Add ordinary Python modules here when multiple agent resources need the same
implementation. Harnest bundles them without discovering them as capabilities.
For example, ` + "`lib/audit.py`" + ` can contain:

` + "```python" + `
def record_change(action: str) -> dict[str, str]:
    """Return a small structured audit value."""
    return {"action": action}
` + "```" + `

Import it through the compiler-owned namespace:

` + "```python" + `
from harnest.lib.audit import record_change
` + "```" + `

Nested helper modules follow the same import path. Keep tools, tasks, hooks,
and other discovered declarations in their owning folders.
`
}

// scaffoldModelsGuide explains the optional compiler-owned model namespace.
func scaffoldModelsGuide() string {
	return `# Pydantic contracts

Optional: Yes. Delete this folder if the agent has no shared Pydantic models.

Store request, response, tool, WebSocket, and streaming models here. For
example, ` + "`models/support.py`" + ` can contain:

` + "```python" + `
from pydantic import BaseModel, Field


class SupportRequest(BaseModel):
    """Validate one support request at the authored boundary."""

    message: str = Field(min_length=1)


class SupportResponse(BaseModel):
    """Return the normalized support response."""

    answer: str
` + "```" + `

Import these contracts through the compiler-owned namespace:

` + "```python" + `
from harnest.models.support import SupportRequest, SupportResponse
` + "```" + `

Nested modules use the same ` + "`harnest.models.*`" + ` path. Models are bundled but
never discovered as agent capabilities.
`
}

// optionalFolderGuide documents one generated resource folder and its removal policy.
func optionalFolderGuide(directory, mode string) string {
	guide := scaffoldFolderGuides()[directory]
	if mode == "advanced" && containsDirectory(advancedExplicitResourceDirectories, directory) {
		guide += `

> Advanced mode owns framework wiring in agent.py; Harnest does not discover
> this folder. Delete this folder or treat the example above only as the
> managed-mode contract when switching modes.
`
	}
	return guide
}

// containsDirectory keeps advanced-mode ownership notes aligned with mode migration.
func containsDirectory(directories []string, target string) bool {
	for _, directory := range directories {
		if directory == target {
			return true
		}
	}
	return false
}

// scaffoldTestGuide documents one optional authored-test lane.
func scaffoldTestGuide(lane string) string {
	if lane == "smoke" {
		return `# Smoke tests

Optional: Yes. Delete this folder if the agent has no live integration tests.

Add opt-in test_*.py files for live models, MCP, and HTTP behavior. For example,
` + "`tests/smoke/test_health.py`" + ` can contain:

` + "```python" + `
def test_health(client):
    response = client.get("/healthz")
    assert response.status_code == 200
` + "```" + `

Run this lane explicitly with ` + "`harnest test . --smoke`" + ` from the agent folder. Keep network
and credential-dependent assertions out of unit tests.
`
	}
	return `# Unit tests

Optional: Yes. Delete this folder if the agent has no authored unit tests.

Add offline test_*.py files for agent definitions and local tools. Harnest
injects the compiled agent and discovered tools as fixtures. For example,
` + "`tests/unit/test_tools.py`" + ` can contain:

` + "```python" + `
def test_agent_is_compiled(agent):
    assert agent.name
` + "```" + `

Run this lane with ` + "`harnest test .`" + ` from the agent folder.
`
}

// scaffoldFolderGuides returns the canonical guidance for discovered folders.
func scaffoldFolderGuides() map[string]string {
	return map[string]string{
		"tools": `# Model tools

Optional: Yes. Delete this folder if the model does not need authored tools.

Create a starter with ` + "`harnest add tool lookup`" + `.
Add one @tool callable per public Python file. For example,
` + "`tools/lookup.py`" + ` can contain:

` + "```python" + `
from harnest.agent import tool


@tool
def lookup(topic: str) -> str:
    """Return locally available information for a topic."""
    return f"No indexed result for {topic}."
` + "```" + `

The filename is the tool module; Harnest discovers the decorated callable.
`,
		"tasks": `# Durable tasks

Optional: Yes. Delete this folder if the application has no durable background work.

Create a starter with ` + "`harnest add task prepare-report`" + `,
or add one durable @task callable per public Python file.
Harnest discovers tasks in both authoring modes. For example,
` + "`tasks/prepare_report.py`" + ` can contain:

` + "```python" + `
from harnest.task import task


@task(queue="reports", max_retries=3)
async def prepare_report(subject: str) -> dict[str, str]:
    """Prepare a durable report result."""
    return {"subject": subject, "status": "ready"}
` + "```" + `

Tasks are application work, not model tools. Invoke directly or defer them from
an active runtime with the required task storage configured.
`,
		"cron": `# Scheduled tasks

Optional: Yes. Delete this folder if the application has no schedules.

Add one UTC Cron declaration per public Python file.
Harnest owns scheduling in both authoring modes. After adding the task above,
` + "`cron/daily_report.py`" + ` can contain:

` + "```python" + `
from harnest.cron import Cron
from tasks.prepare_report import prepare_report


daily_report = Cron(
    "0 9 * * 1-5",
    task=prepare_report,
    arguments={"subject": "daily"},
)
` + "```" + `

Schedules use UTC and must target a root ` + "`tasks/`" + ` export.
`,
		"subagents": `# Subagents

Optional: Yes. Delete this folder if the root agent does not delegate work.

For a managed ADK Agent, run
` + "`harnest add subagent helper`" + `; Harnest discovers
and attaches the new definition automatically. Managed graphs instead reference
subagent definitions explicitly as graph nodes. For example,
` + "`subagents/helper.py`" + ` can contain:

` + "```python" + `
from harnest.agent import Agent
from harnest.model import LiteLLMModel


helper = Agent(
    name="helper",
    model=LiteLLMModel.from_openai_environment(),
    instruction="Summarize the request clearly.",
)
` + "```" + `

Only graph roots import ` + "`helper`" + ` in ` + "`agent.py`" + `, add it to
` + "`Graph.nodes`" + `, and connect it with an edge. Use a subfolder when a
subagent owns private resources.
`,
		"mcp": `# MCP clients

Optional: Yes. Delete this folder if the agent has no direct MCP connections.

Add one public Python file per connection. Its zero-argument ` + "`client()`" + ` factory
must return MCPClient. For example, ` + "`mcp/knowledge.py`" + ` can contain:

` + "```python" + `
import os

from harnest.mcp import MCPClient


def client():
    """Connect to the configured knowledge server."""
    return MCPClient.streamable_http(
        os.environ["KNOWLEDGE_MCP_URL"],
        prefix="knowledge",
    )
` + "```" + `

The filename is the client identity. Keep credentials in the runtime
environment, never in source.
`,
		"extensions": `# Harnest Extensions

Optional: Yes. Delete this folder if the application needs no reusable extensions.

Add packages at ` + "`extensions/<name>/`" + ` with extension.yaml and extension.py.
A minimal ` + "`extensions/audit/extension.yaml`" + ` is:

` + "```yaml" + `
apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: audit
  version: 0.1.0
runtime:
  entrypoint: extension:extension
capabilities: []
` + "```" + `

Its ` + "`extension.py`" + ` can contain:

` + "```python" + `
from harnest.extensions import Extension


class AuditExtension(Extension):
    """Own audit resources for the application lifetime."""


extension = AuditExtension()
` + "```" + `

Declare extension hooks and resource factories in its internal ` + "`lifecycle/`" + `
package. Agent Plugins belong in ` + "`plugins/`" + ` instead.
`,
		"lifecycle": `# Application lifecycle

Optional: Conditional. The generated lifecycle/storage.py supplies session and
checkpoint state for the default agent's ` + "`history=\"session\"`" + `. Delete this folder
only after removing that requirement or moving equivalent storage ownership.
All other lifecycle modules are optional.

Run ` + "`harnest add lifecycle audit`" + ` for a hook or
` + "`harnest add context request-cache`" + ` for a
provider. You can also add @lifecycle-decorated hooks and resource factories in
public Python files here. For example, ` + "`lifecycle/audit.py`" + ` can contain:

` + "```python" + `
from harnest import lifecycle


@lifecycle.agent.after
def observe_result(_context, result):
    """Observe a completed invocation without replacing its result."""
    return result
` + "```" + `

Use grouped paths such as ` + "`lifecycle.storage.sessions`" + ` and
` + "`lifecycle.model.before`" + `. Reusable packages belong in ` + "`extensions/`" + `.
`,
		"plugins": `# Agent Plugins

Optional: Yes. Delete this folder if the agent uses no portable plugins.

Agent Plugin folders contain an Agent Plugins 1.0 plugin.json manifest and may
include ` + "`skills/`" + `, mcp.json, or both. For example,
` + "`plugins/research/plugin.json`" + ` can contain:

` + "```json" + `
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "research"
}
` + "```" + `

Put reusable application code and lifecycle ownership in ` + "`extensions/`" + `,
not Agent Plugins.
`,
		"sandbox": `# Sandboxes

Optional: Yes. Delete this folder if no agent executes isolated code.

Install a sandbox provider extension, then add one Python file per named
sandbox. For example, ` + "`sandbox/calculations.py`" + ` can contain:

` + "```python" + `
from harnest.extensions.docker import docker
from harnest.sandbox import SandboxNetworkPolicy


calculations = docker.sandbox(
    image="python:3.12-slim",
    network_policy=SandboxNetworkPolicy.none(),
)
` + "```" + `

Add ` + "`sandboxes=[" + `"calculations"` + "]`" + ` to each allowed Agent. Authored tools
can then call ` + "`context.sandboxes[" + `"calculations"` + "].execute(code)`" + `.
`,
		"skills": `# Agent Skills

Optional: Yes. Delete this folder if the agent needs no progressively loaded guidance.

Add one directory per instruction pack. The directory name must match the
SKILL.md frontmatter name. For example, ` + "`skills/refunds/SKILL.md`" + ` can contain:

` + "```markdown" + `
---
name: refunds
description: Apply the refund policy when a user asks to reverse a charge.
---

# Refunds

1. Verify the order before proposing a refund.
2. Request approval before committing a reversal.
` + "```" + `

Keep SKILL.md at 400 words or fewer and move detailed references into a linked
` + "`references/`" + ` folder.
`,
		"evals": `# Evaluations

Optional: Yes. Delete this folder if the agent has no shared evaluation suites.

Add ` + "`*.evalset.json`" + ` files and optional test_config.json metrics. For example,
` + "`evals/starter.evalset.json`" + ` begins with:

` + "```json" + `
{
  "eval_set_id": "starter",
  "name": "Starter evaluation",
  "eval_cases": [
    {
      "evalId": "answers_greeting",
      "conversation": [
        {
          "userContent": {"role": "user", "parts": [{"text": "Say hello."}]},
          "finalResponse": {"role": "model", "parts": [{"text": "Hello!"}]}
        }
      ],
      "sessionInput": {
        "appName": "guide_agent",
        "userId": "eval-user",
        "state": {}
      }
    }
  ]
}
` + "```" + `

Run suites explicitly with ` + "`harnest test . --evals`" + ` from the agent folder. The filename must
match ` + "`eval_set_id`" + `; replace ` + "`guide_agent`" + ` with the name exported by agent.py.
`,
	}
}
