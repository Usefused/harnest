# MCP Agent

This Harnest agent connects to the configured authenticated Streamable HTTP MCP
server and makes its discovered tools available to an ADK agent using your
OpenAI-compatible model endpoint.

## Configure

From this agent folder, configure your model endpoint and MCP bearer token.
Keep credentials in the environment, not in this project:

```bash
export HARNEST_MCP_TOKEN='<token>'
export OPENAI_MODEL='<model-served-by-your-endpoint>'
export OPENAI_BASE_URL='https://your-model-server.example/v1'
# Only if your model endpoint requires authentication:
export OPENAI_API_KEY='<model-api-key>'
```

Set the non-secret MCP URL in `config.yaml`. Harnest does not select a default
model or provider endpoint.

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
