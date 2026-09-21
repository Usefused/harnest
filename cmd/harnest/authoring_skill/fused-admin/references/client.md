# Fused Admin client contract

This reference is grounded in Fused's built-in Admin Python SDK and Harnest's
vendored management client. Harnest's private `_fused_admin_client` module is
an internal integration detail, not an import to put in generated agent code.

## Obtain the SDK

The existing fused-cli materializes the built-in SDK locally, without an Engine
session or registry-package lookup:

```sh
fused-cli sdk admin-client --language python --out ./clients
```

This writes `clients/fused-admin`. Install that local project in the consuming
application's environment and declare the local dependency there. Python imports:

```python
from fused.fused_admin import (
    FusedAdminClient, FusedAdminConfig, AppTokenPayload, AppTokenBinding,
)
```

`--language ts` produces the local TypeScript client; use its shipped definitions
for TypeScript signatures. Do not assume these names identify packages available
from a public package registry. Studio's proposal agent cannot run this command;
describe it as a prerequisite if the client has not been materialized.

## Management workflow

Construct `FusedAdminClient(FusedAdminConfig(engine_url=engine_url,
access_token=oauth_access_token))` in trusted application code after Fused Auth
completes. This is a synchronous client; avoid blocking an async request loop.

| Method | Result and use |
|---|---|
| `list_services()` | Workspace services with `id`, `version_id`, `slug`, `name`, `description`, `version`. |
| `list_service_operations(service_id, version)` | Operations with `id`, `name`, `method`, `path`; use exact discovered identifiers. |
| `list_servers(limit=10, offset=0)` | `MCPServerList.items` and `total`; page results when resolving an existing server. |
| `deploy_server(config, owner_team=None)` | Creates/deploys a server from a complete Fused MCP configuration. No separate Admin `plan()` method is documented. |
| `generate_token(reference, AppTokenPayload(...))` | Issues a named execution token using a server name or family reference, resolved by the client. |

Service operation IDs, versions, bucket, and owner come from authorized discovery
or user selections. The following is a **shape**, not a deployable invented service:

```python
config = {
    "apiVersion": "fused/v1", "kind": "mcp",
    "name": chosen_name, "version": chosen_version,
    "description": capability_description, "bucket": chosen_bucket,
    "services": {
        discovered_service_slug: {
            "version": discovered_service_version,
            "operations": selected_operation_ids,
        },
    },
}
server = client.deploy_server(config, owner_team=chosen_owner)
```

Use `select_all: true` only when the user intends the entire service; do not combine
it with `operations`. Auth selections and provider connections must match verified
service configuration. This client does not expose every bucket/connection discovery
operation; do not invent methods when additional discovery is needed.

## Transport and execution credentials

`MCPServer` includes `id`, `name`, `version`, `active`, `stable`, and optional
`transport_urls`. Its `id` identifies an app version, not the app family.
`transport_urls.streamable_http` is the stable Streamable HTTP endpoint;
`versioned_streamable_http` deliberately pins a version. Confirm metadata exists
and select the requested active server/version. Do not construct a guessed URL.

`AppTokenPayload` accepts `name`, optional `allow` (operation IDs), `expires_in`
(seconds), `binding_mode`, and `bindings`. An `AppTokenBinding` contains
`service_slug`, `auth_name`, `end_user_ref`, and optional `resource_id`. Do not
invent bindings or broaden authorization to work around a rejection.

Token plaintext is returned once. The trusted host stores or delivers it outside
model context. Use bounded lifetime and the intended operation scope; token
issuance is a management mutation, not discovery. A naming conflict does not
recover a previously issued token. Never return whole Admin responses to the
model: they can contain `execution_token` or `token` fields.

In an authored Harnest project, connect through the public MCP API:

```python
from harnest.mcp import MCPClient


def client() -> MCPClient:
    """Connect to the Engine-hosted MCP with runtime-only credentials."""
    return MCPClient.streamable_http(
        "${FUSED_MCP_URL}",
        headers={"Authorization": "Bearer ${FUSED_MCP_TOKEN}"},
    )
```

The host supplies the returned URL and execution token through those environment
variables. Preserve existing variable names when modifying a project. Update
deployment bindings consistently. Do not send the Admin OAuth token to the MCP.

The OAuth grant must cover the requested management actions, and Engine also
checks workspace, owner, bucket, and service access. On denial, report the missing
access; do not grant permissions, change owners, or mint broader tokens as a retry.
