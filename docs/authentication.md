# Authentication and downstream credentials

Harnest keeps request authentication, agent identity, and downstream credentials
connected without making secrets part of agent state.

```text
HTTP/WebSocket connection
  -> @lifecycle.authenticate
  -> AuthPrincipal
  -> CredentialRequest.principal
  -> CredentialProvider
  -> context.credentials.resolve(...)
  -> trusted outbound client
```

## Authenticate a request

An authentication listener receives a read-only `ConnectionContext`. It can
inspect the transport, method, path, headers, cookies, and query parameters.
The request body is never provided. Return one validated `AuthPrincipal`:

```python
# lifecycle/authentication.py
from harnest import Credential, lifecycle
from harnest.auth import AuthPrincipal, AuthenticationError
from harnest.lib.identity import verify_browser_token


@lifecycle.authenticate
async def authenticate(connection, principal):
    if principal is not None:
        return None

    authorization = connection.headers.get("authorization")
    claims = await verify_browser_token(authorization)
    if claims is None:
        raise AuthenticationError()

    return AuthPrincipal(
        user_id=claims.subject,
        claims={"tenant_id": claims.tenant_id},
        credentials={"browser": Credential(authorization)},
    )
```

`claims` is for selected, non-secret authorization facts. Put tokens, signed
headers, or other secret-bearing values in the `credentials` mapping as opaque
`Credential` objects. Harnest does not copy the original connection into the
principal; the listener deliberately promotes only what its policy validates.

Authentication listeners form an ordered pipeline. A later listener receives
the current principal and may return a replacement with the same `user_id`.
Changing an established identity fails closed.

## Resolve a downstream credential

Declare one optional root provider:

```python
# lifecycle/credentials.py
from harnest import CredentialProvider, lifecycle
from harnest.lib.identity import exchange_for_engine


class ThreadifyCredentials(CredentialProvider):
    async def resolve(self, request):
        browser = request.principal.credentials["browser"]
        return await exchange_for_engine(
            browser,
            principal=request.principal,
            audience=request.audience,
            scopes=request.scopes,
        )


@lifecycle.credential_provider
def credential_provider():
    return ThreadifyCredentials()
```

`CredentialRequest` contains `principal`, `audience`, `scopes`, `framework`,
`agent_name`, `invocation_id`, and `session_id`. It has no copied `user_id`
field and never contains the original headers, cookies, query, body, prompt, or
request metadata. Carrying the complete principal means every intentionally
added principal claim or credential is available to the provider.

A provider may forward an incoming credential only when its audience policy
allows that, or exchange it for a narrower downstream credential. Harnest does
not make that security decision automatically.

## Use credentials from trusted code

Tools, graph nodes, and functions under `lib/` use the same non-enumerable
invocation capability:

```python
# lib/threadify_engine.py
from harnest import context


async def execute(payload):
    credential = await context.credentials.resolve(
        "threadify-engine", scopes=("engine:execute",)
    )
    return await engine.execute(
        payload,
        headers={"Authorization": credential.reveal()},
    )
```

The resolver works only during an active compiled invocation. Root agents and
subagents inherit the same binding; late child tasks fail after revocation.
Do not accept an audience or token as a model-generated tool argument.

## Secret and persistence boundary

Harnest does not copy principal claims, the principal credential map, or
resolved credential material into public context resources, prompts,
application state, session state, checkpoints, task payloads, traces, events,
or its own logs. A principal's stable `user_id` still scopes the invocation and
may appear as identity metadata. Secret-bearing representations are redacted,
provider failures are sanitized, and private bindings are revoked after each
request and invocation.

After trusted code calls `Credential.reveal()`, Harnest cannot prevent that
code, an HTTP client, or another library from logging the returned value. Reveal
only while constructing the outbound request and configure client logging to
redact authorization headers.

Queued work must store authorization intent or an application-owned grant
reference, not a resolved credential. Resolve a fresh credential when the task
executes.
