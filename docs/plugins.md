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
`plugins/warehouse/mcp/bigquery.py` exports a literally zero-parameter
`client()` factory; optional parameters, `*args`, and `**kwargs` are rejected.
The filename supplies the local identity `bigquery`:

```python
import os

from harnest.mcp import MCPClient


def client():
    return MCPClient.streamable_http(
        os.environ["BIGQUERY_MCP_URL"],
        prefix="bigquery",
    )
```

Both current Streamable HTTP and legacy SSE clients are supported. The factory
is ordinary Python and may use `os.environ`, `os.getenv`, a credential provider,
or third-party code. Credentials and provider dependencies belong in deployment
configuration and `requirements.txt`; they are not embedded in the plugin.
Factory diagnostics redact exception messages because those values may contain
credentials. A selective `@require_human_approval(tools=[...])` policy names
the server's original tools before any prefix; ADK and LangGraph validate every
name after discovery and fail closed on a typo.

Skill directories under `skills/` follow the Agent Skills layout. The
`SKILL.md` frontmatter `name` must match its directory. Its instructions should
teach the host agent how to select the plugin's MCP tools, provide their inputs,
interpret their results, and handle a connection failure. Skills
may include the usual `references/`, `assets/`, and `scripts/` resources.

Plugin MCP clients join direct clients from `mcp/`, and plugin skills join
direct skills from `skills/`. Discovery is deterministic by plugin name.
Identical MCP connection configurations and duplicate skill names fail
compilation instead of silently shadowing one another. Connection equality
deliberately ignores compiler identity and approval metadata, so declaring one
configured server twice still fails. Harnest separately assigns a deterministic
path-scoped capability identity, allowing same-named direct and plugin clients
to remain distinct when their configurations differ.

An entirely empty plugin directory is skipped. Once it contains a public
resource, it must contain at least one MCP client module and at least one skill;
an incomplete capability fails compilation instead of silently becoming a
different kind of resource. Plugin source is copied into the compiled artifact
and included in its digest.

Use [extensions](extensions.md) for lifecycle behavior such as persistence,
guardrails, auditing, or framework-native middleware. Use `subagents/` for
additional agent definitions; subagents are deliberately not plugin content.
