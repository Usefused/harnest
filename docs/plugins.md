# Plugins

`plugins/` contains reusable MCP capability bundles. A plugin keeps MCP client
connections together with the progressive skills that tell the host agent when
and how to use the tools exposed by those connections. A plugin never contains
or creates another agent, and it does not run lifecycle callbacks.

Each plugin is a directory with this shape:

```text
plugins/
└── warehouse/
    ├── mcp/
    │   └── bigquery.py
    └── skills/
        └── query-warehouse/
            ├── SKILL.md
            └── references/       # optional
```

Files under `mcp/` use the same contract as direct MCP resources. For example,
`plugins/warehouse/mcp/bigquery.py` exports an `MCPClient` or `None` named
`bigquery`:

```python
import os

from harnest.mcp import MCPClient


bigquery = (
    MCPClient.streamable_http(
        os.environ["BIGQUERY_MCP_URL"],
        prefix="bigquery",
    )
    if os.getenv("BIGQUERY_MCP_URL")
    else None
)
```

Exporting `None` disables an optional connection. Both current Streamable HTTP
and legacy SSE clients are supported. Credentials and provider dependencies
belong in deployment configuration and the agent's `requirements.txt`; they
are not embedded in the plugin.

Skill directories under `skills/` follow the Agent Skills layout. The
`SKILL.md` frontmatter `name` must match its directory. Its instructions should
teach the host agent how to select the plugin's MCP tools, provide their inputs,
interpret their results, and handle an unavailable optional connection. Skills
may include the usual `references/`, `assets/`, and `scripts/` resources.

Plugin MCP clients join direct clients from `mcp/`, and plugin skills join
direct skills from `skills/`. Discovery is deterministic by plugin name.
Identical MCP connection configurations and duplicate skill names fail
compilation instead of silently shadowing one another.

An entirely empty plugin directory is skipped. Once it contains a public
resource, it must contain at least one MCP client module and at least one skill;
an incomplete capability fails compilation instead of silently becoming a
different kind of resource. Plugin source is copied into the compiled artifact
and included in its digest.

Use [extensions](extensions.md) for lifecycle behavior such as persistence,
guardrails, auditing, or framework-native middleware. Use `subagents/` for
additional agent definitions; subagents are deliberately not plugin content.
