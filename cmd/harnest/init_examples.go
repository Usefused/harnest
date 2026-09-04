package main

// managedExampleScaffoldFiles adds inert samples only where the default profile
// has no code, preserving its root agent and required storage implementation.
func managedExampleScaffoldFiles(files map[string]string, agentName, framework string) map[string]string {
	samples := map[string]string{
		"tools/_example.py":                                       "\"\"\"Copy to echo.py to expose the echo tool.\"\"\"\n\n" + files["tools/echo.py"],
		"sandbox/_example.py":                                     files["sandbox/_example.py"],
		"skills/_example/SKILL.md":                                files["skills/getting-started/SKILL.md"],
		"plugins/_example_agent/plugin.json":                      files["plugins/starter/plugin.json"],
		"plugins/_example_agent/skills/starter-guidance/SKILL.md": files["plugins/starter/skills/starter-guidance/SKILL.md"],
		"evals/_example.evalset.json":                             files["evals/starter.evalset.json"],
		"tests/unit/_example.py":                                  "\"\"\"Copy to test_agent.py after activating tools/_example.py as echo.py.\"\"\"\n\n" + files["tests/unit/test_agent.py"],
		"tests/smoke/_example.py":                                 "\"\"\"Copy to test_health.py; run explicitly with harnest test --smoke.\"\"\"\n\n" + files["tests/smoke/test_health.py"],
	}
	// Reuse the default profile unchanged: resources with existing code need no
	// second example, and placeholders must not activate capabilities implicitly.
	files = minimalScaffoldFiles(files, agentName, framework, "managed")
	for path, source := range samples {
		files[path] = source
	}
	for path, source := range managedFolderCodeSamples() {
		files[path] = source
	}
	return files
}

// managedFolderCodeSamples supplies neutral, opt-in templates for the remaining
// folder contracts, keeping Harnest Extensions separate from Agent Plugins.
func managedFolderCodeSamples() map[string]string {
	return map[string]string{
		"lib/_example.py": `"""Copy to messages.py; import with from harnest.lib.messages import normalize."""


def normalize(message: str) -> str:
    """Collapse whitespace without retaining request data."""
    return " ".join(message.split())
`,
		"models/_example.py": `"""Copy to messages.py; import with from harnest.models.messages import Message."""

from pydantic import BaseModel, Field


class Message(BaseModel):
    """Validate a non-empty message at an authored boundary."""

    text: str = Field(min_length=1)
`,
		"tasks/_example.py": `"""Copy to prepare_report.py to register application-owned queue work.

Direct calls run inline. Await prepare_report.defer(subject="daily") only from
an active Harnest runtime with the required task-storage configuration.
Tasks are not automatically exposed as model tools.
"""

from harnest.task import task


@task(queue="reports", max_retries=3)
async def prepare_report(subject: str) -> dict[str, str]:
    """Prepare a deterministic result without external side effects."""
    return {"subject": subject, "status": "ready"}
`,
		"cron/_example.py": `"""Copy to daily_report.py after activating tasks/prepare_report.py.

Harnest owns scheduling; enabling this file requires the task runtime and store.
"""

from harnest.cron import Cron
from tasks.prepare_report import prepare_report


# Schedules use UTC, independent of the host machine's local timezone.
daily_report = Cron(
    "0 9 * * 1-5",
    task=prepare_report,
    arguments={"subject": "daily"},
)
`,
		"subagents/_example.py": `"""Copy to helper.py and reference helper explicitly in the root Graph.

Import with from subagents.helper import helper, then add it to Graph.nodes
and connect it with an Edge. A flat subagent has no private resource folders.
"""

import os

from harnest.agent import Agent
from harnest.model import LiteLLMModel


helper = Agent(
    name="helper",
    model=LiteLLMModel(
        model=os.getenv("LITELLM_MODEL", "ollama_chat/qwen3.5:cloud"),
        api_base=os.getenv("LITELLM_API_BASE", "http://127.0.0.1:11434"),
    ),
    instruction="Summarize the request clearly; acknowledge missing information.",
)
`,
		"mcp/_example.py": `"""Copy to knowledge.py and configure HARNEST_MCP_URL to enable this client.

Keep credentials in the runtime environment, never in this source file.
"""

import os

from harnest.mcp import MCPClient


def client():
    """Resolve the authored endpoint when Harnest discovers this client."""
    return MCPClient.streamable_http(os.environ["HARNEST_MCP_URL"], prefix="knowledge")
`,
		"extensions/_example/extension.yaml": `apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: starter_runtime
  version: 0.1.0
runtime:
  entrypoint: extension:extension
capabilities: []
`,
		"extensions/_example/extension.py": `"""Copy the containing folder to extensions/starter_runtime to enable this extension.

Declare any contributed lifecycle/context authority in extension.yaml before use.
"""

from harnest.extensions import Extension


class StarterExtension(Extension):
    """Own application-lifetime resources without importing a framework."""

    async def start(self, start_context):
        """Acquire extension-owned clients here, never during module import."""

    async def stop(self):
        """Close only resources this extension acquired during startup."""


extension = StarterExtension()
`,
		"plugins/_README.md": `# Plugin samples

Copy _example_agent/ to starter/ to enable this skills-only Agent Plugin.
Its plugin.json manifest follows Agent Plugins 1.0. Skills and MCP servers are
optional components: a plugin can provide either or both.

To add an MCP server, create starter/mcp.json after replacing the HTTPS
placeholder endpoint with your server's address:

    {
      "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
      "mcpServers": {
        "knowledge": {
          "type": "streamable-http",
          "url": "https://mcp.example.com/mcp"
        }
      }
    }

Use declarative mcp.json, not Python factories, inside Agent Plugins. Keep
credentials out of committed files. Application lifecycle hooks and resource
factories belong in lifecycle/ or Harnest Extensions, not Agent Plugins.

The underscore-prefixed folder is ignored until copied or renamed.
Harnest Extension samples live separately in extensions/_example/.
`,
		"skills/_README.md": `# Skill sample

Copy _example/ to getting-started/ to enable SKILL.md. The directory name must
match its frontmatter name. Put longer guidance in references/ and link it from
SKILL.md; keep the entrypoint at 400 words or fewer.
`,
		"evals/_README.md": `# Evaluation sample

Copy _example.evalset.json to starter.evalset.json to enable the shared EvalSet
for ADK or LangGraph; the filename must match eval_set_id. Run it explicitly
with harnest test --evals after configuring a model. Optional test_config.json
selects metrics. Underscore-prefixed examples do not run as eval suites.
`,
	}
}
