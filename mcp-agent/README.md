# MCP Agent

This Harnest agent connects to the configured authenticated Streamable HTTP MCP
server and makes its discovered tools available to an ADK agent running through
Ollama.

## Configure

Keep the bearer token in the environment. Do not add it to this project:

```bash
export HARNEST_MCP_TOKEN='<token>'
```

The non-secret MCP URL, Ollama endpoint, and model are configured in
`config.yaml`. Change `OLLAMA_MODEL` if you want to use another installed model.

## Verify

Export the token before every Harnest command that compiles or runs the agent.
The MCP service must also be running at the configured URL before live tool
discovery:

```bash
harnest test .
harnest run . "List the tools you can use and briefly explain them."
```

Harnest resolves `HARNEST_MCP_TOKEN` while materializing the native MCP toolset.
The value stays in the process environment and is not stored in this project.
