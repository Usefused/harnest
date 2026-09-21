---
name: fused-admin
description: Author Fused Admin integrations that discover workspace services and operations, create Engine-hosted MCP servers, select returned transport URLs, and issue scoped execution tokens. Use when building an MCP with Fused, not merely attaching an existing MCP URL.
---

# Fused Admin

Produce a real Fused management integration and an MCP connection grounded in
discovered services, operations, and returned server metadata.

Read [references/client.md](references/client.md) for the client distribution,
method contracts, MCP configuration, and Harnest connection example. Load
`fused-auth` for the developer OAuth grant needed by the Admin client.

## Make the right distinction

- Creating a Fused MCP means deploying a `kind: mcp` configuration to a Fused
  Engine. Connecting to an existing MCP means consuming its returned HTTP URL.
  Do not replace creation with a request for a URL the user expects you to create.
- Fused Admin and Fused Auth are built-in client SDKs materialized by fused-cli.
  An Engine-hosted MCP is not an npm subprocess. Do not invent packages such as
  `@fused/fused-mcp`, undocumented constructors, or service/operation IDs.
- Use workspace discovery to choose service versions and the operations needed
  for the user's goal. Preserve the intended owner and bucket. Missing discovery
  results are missing inputs, not permission to guess or select every operation.
- Deploy the complete configuration, then use the returned transport metadata.
  Keep the management OAuth grant separate from the MCP execution token.
- Return source/configuration with environment or credential-provider references;
  never put credential values into model-visible output or generated source.

## Respect available capabilities

Skills provide guidance, not executable tools. Call only management tools actually
advertised by the host. If the host supports source proposals only, author the
integration and explain the missing authentication, discovery, or execution step.
Never claim deployment or token issuance succeeded without an actual result.
Preserve review requirements for external mutations and stop on denied access.
