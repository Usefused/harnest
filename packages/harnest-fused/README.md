# harnest-fused

Built and maintained by [Fused](https://usefused.com).

Compose multiple OpenAPI specifications into one Fused MCP server and connect
through Harnest's standard MCP runtime. Operation selection is optional for each
specification: omitting it selects all operations.

Install this source package with `pip install ./packages/harnest-fused` from the
Harnest checkout. Provisioning requires a separately installed and configured
`fused-cli`; importing and running an already provisioned client does not.

See the [Fused MCP guide](https://docs.usefused.com/harnest/build/fused-mcp)
for authoring, explicit provisioning, and credential setup.
