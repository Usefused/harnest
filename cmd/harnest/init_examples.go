// Example files `harnest init` writes into a managed agent's optional folders.

package main

import "harnest.dev/harnest/internal/pysource"

// managedExampleScaffoldFiles adds inert samples only where the default profile
// has no code, preserving its root agent and required storage implementation.
func managedExampleScaffoldFiles(files map[string]string, agentName string) map[string]string {
	samples := map[string]string{
		"tools/_example.py":                                       pysource.Render("init/examples/tools/_example.py.tmpl", map[string]any{"Source": files["tools/echo.py"]}),
		"sandbox/_example.py":                                     files["sandbox/_example.py"],
		"skills/_example/SKILL.md":                                      files["skills/getting-started/SKILL.md"],
		"agent-plugins/_example_agent/plugin.json":                      files["agent-plugins/starter/plugin.json"],
		"agent-plugins/_example_agent/skills/starter-guidance/SKILL.md": files["agent-plugins/starter/skills/starter-guidance/SKILL.md"],
		"evals/_example.evalset.json":                                   files["evals/starter.evalset.json"],
		"tests/unit/_example.py":                                  pysource.Render("init/examples/tests/unit/_example.py.tmpl", map[string]any{"Source": files["tests/unit/test_agent.py"]}),
		"tests/smoke/_example.py":                                 pysource.Render("init/examples/tests/smoke/_example.py.tmpl", map[string]any{"Source": files["tests/smoke/test_health.py"]}),
	}
	// Reuse the default profile unchanged: resources with existing code need no
	// second example, and placeholders must not activate capabilities implicitly.
	files = guidedScaffoldFiles(files, agentName, "managed")
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
		"lib/_example.py":       pysource.File("init/examples/lib/_example.py"),
		"models/_example.py":    pysource.File("init/examples/models/_example.py"),
		"tasks/_example.py":     pysource.File("init/examples/tasks/_example.py"),
		"cron/_example.py":      pysource.Render("init/examples/cron/_example.py.tmpl", nil),
		"subagents/_example.py": pysource.File("init/examples/subagents/_example.py"),
		"mcp/_example.py":       pysource.File("init/examples/mcp/_example.py"),
		"extensions/_example/extension.yaml": `apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: starter_runtime
  version: 0.1.0
runtime:
  entrypoint: extension:extension
capabilities: []
`,
		"extensions/_example/extension.py": pysource.File("init/examples/extensions/_example/extension.py"),
		"agent-plugins/_README.md": `# Agent Plugin samples

Optional: Yes. Delete this folder if the agent uses no portable plugins.

Copy _example_agent/ to starter/ to enable this skills-only Agent Plugin.
Its plugin.json manifest follows Agent Plugins 1.0. Skills and MCP servers are
optional components: a plugin can provide either or both.

Install a local portable package atomically by running:

    harnest agent-plugins install ./path/to/plugin --project ./path/to/agent

To add an MCP server, create starter/mcp.json after replacing the HTTPS
placeholder endpoint with your server's address:

` + "```json" + `
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "knowledge": {
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
` + "```" + `

Use declarative mcp.json, not Python factories, inside Agent Plugins. Keep
credentials out of committed files. Application lifecycle hooks and resource
factories belong in lifecycle/ or Harnest Extensions, not Agent Plugins.

The underscore-prefixed folder is ignored until copied or renamed.
Harnest Extension samples live separately in extensions/_example/.
`,
		"skills/_README.md": `# Skill sample

Optional: Yes. Delete this folder if the agent needs no progressively loaded guidance.

Copy _example/ to getting-started/ to enable SKILL.md. The directory name must
match its frontmatter name. Put longer guidance in references/ and link it from
SKILL.md; keep the entrypoint at 400 words or fewer.

` + "```markdown" + `
---
name: getting-started
description: Apply the agent's core instructions to general requests.
---
` + "```" + `
`,
		"evals/_README.md": `# Evaluation sample

Optional: Yes. Delete this folder if the agent has no shared evaluation suites.

Copy _example.evalset.json to starter.evalset.json to enable the shared EvalSet
for ADK or LangGraph; the filename must match eval_set_id. Run it explicitly
with harnest test --evals after configuring a model. Optional test_config.json
selects metrics. Underscore-prefixed examples do not run as eval suites.

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

Replace guide_agent with the name exported by agent.py when copying this
inline shape; the generated _example file already uses the scaffolded name.
`,
	}
}

// scaffoldCronExample keeps the folder guide and opt-in sample on the same
// single-function contract, including the queued task's explicit policy.
func scaffoldCronExample() string {
	return pysource.File("init/shared/cron_daily_report.py")
}
