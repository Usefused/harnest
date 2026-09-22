---
name: harnest-authentication
description: Implement or review Harnest request authentication, AuthPrincipal propagation, browser-token pass-through or exchange, CredentialProvider policy, and trusted tool or library credential use. Use for auth extensions and downstream authorization flows; not for model-provider API keys configured directly on model connectors.
---

# Harnest authentication

Build a tested trust path from an incoming connection to a downstream service
without making credentials agent state.

Read [references/contract.md](references/contract.md) before changing an auth or
credential flow.

## Sensitive data handling

Use placeholders, environment-variable names, and synthetic credentials when
authoring code or testing; do not request or read real secret values. If supplied
in conversation, omit them from generated files, replies, and tool arguments.
Generated runtime code may validate incoming tokens or exchange them with the
configured identity provider, and send credentials only to the authorized
downstream audience. Keep them opaque until that trusted request boundary;
never persist or expose their raw values to the model.

## Required decisions

1. Treat `@lifecycle.authenticate` as the only raw connection boundary. Validate
   identity before returning `AuthPrincipal`.
2. Put selected non-secret authorization facts in `principal.claims`. Wrap every
   promoted token or signed header in `Credential` under
   `principal.credentials`. Never copy the complete request.
3. Implement downstream policy in one root `CredentialProvider`. Read identity,
   claims, and incoming credentials only through `request.principal`; there is
   no copied `CredentialRequest.user_id` field.
4. In trusted tools, nodes, or `lib/`, call
   `context.credentials.resolve(fixed_audience, scopes)` and reveal only while
   constructing the outbound request. Never make token, audience, or scopes
   model-generated arguments.
5. Prefer audience-specific token exchange. Permit pass-through only when the
   incoming credential is valid for that exact destination.
6. Keep credentials out of prompts, context resources, metadata, state,
   sessions, checkpoints, tasks, logs, traces, and errors. Queued work stores a
   grant reference and resolves later.

## Verification

Test invalid authentication, principal propagation, redacted representations,
provider policy, root/subagent inheritance, revocation, and managed/native
paths that changed. Finish with a local HTTP request using a synthetic token,
proving that an incoming header reaches the provider only as the explicitly
promoted opaque credential. Run the repository quality gate for Harnest source
changes.
