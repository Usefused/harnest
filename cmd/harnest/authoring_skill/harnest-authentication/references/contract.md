# Authentication contract

## Source locations

- `lifecycle/authentication.py`: ordered `@lifecycle.authenticate` listeners.
- `lifecycle/credentials.py`: the optional root provider factory.
- `lib/`: verification, exchange, and outbound client implementation.
- `tools/`: fixed-purpose model capabilities that call trusted library code.
- `docs/authentication.md`: canonical public behavior and examples.

## Authentication

`ConnectionContext` exposes read-only transport, method, path, headers, cookies,
and query values, never the body or FastAPI request. Return `AuthPrincipal`:

```python
return AuthPrincipal(
    user_id=claims.subject,
    claims={"tenant_id": claims.tenant_id},
    credentials={"browser": Credential(authorization)},
)
```

Claims must be selected non-secret facts. Credentials are named opaque values;
their mapping and material are redacted. Later authentication listeners may
replace the principal only without changing `user_id`.

## Resolution

`CredentialRequest` exposes:

- `principal`: the complete authenticated `AuthPrincipal`;
- `audience` and normalized `scopes`;
- `framework`, `agent_name`, `invocation_id`, and `session_id`.

It does not expose duplicate identity fields or the original connection, body,
prompt, metadata, cookies, or unselected headers.

```python
class EngineCredentials(CredentialProvider):
    async def resolve(self, request):
        source = request.principal.credentials["browser"]
        return await exchange(
            source,
            subject=request.principal.user_id,
            tenant=request.principal.claims["tenant_id"],
            audience=request.audience,
            scopes=request.scopes,
        )
```

`context.credentials` is a non-enumerable resolver capability. It is not an
AgentContext resource.

## Tests

Use fake verification and exchange clients for unit tests. An integration test
must send a real HTTP header through authentication and assert the provider sees
the same `Credential` only through `request.principal.credentials`. Do not echo
the value in the response. For a live local test, bind loopback, use a synthetic
token, inspect only a boolean or digest inside the trusted fixture, and shut the
server down.
