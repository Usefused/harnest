---
name: fused-auth
description: Author Fused developer OAuth authentication using the built-in Fused Auth client, PKCE and callback state, token refresh/revocation, and delegated Admin access. Distinguish this from downstream provider OAuth and MCP execution tokens.
---

# Fused Auth

Produce a trusted application authentication flow that obtains the developer's
delegated Fused OAuth grant for management actions without exposing tokens to an
LLM or authored agent state.

Read [references/client.md](references/client.md) for the supported client and
OAuth flow. Load `fused-admin` when the authenticated application needs to
discover services, create MCP servers, or issue execution tokens. Load
`harnest-authentication` when implementing an authored agent's incoming identity
or credential-provider boundary.

## Required decisions

1. Identify the actual Fused issuer, registered client ID, exact callback URI,
   and intended scopes. Public clients use PKCE without a client secret;
   confidential clients keep their secret on the trusted server.
2. Keep state and the PKCE verifier bound to the initiating browser session.
   Validate and consume state once before exchanging the callback code.
3. Pass the resulting OAuth access token only to the trusted Fused Admin client.
   An MCP execution token and a downstream provider's OAuth connection are
   different credentials; do not substitute one for another.
4. Handle expiry, refresh, revocation, cancellation, and reconnect explicitly.
   Never log tokens, verifiers, authorization codes, or raw OAuth error bodies.
5. Expose only connected status and non-secret capability metadata to the model.
   Credentials stay out of prompts, source proposals, sessions, and checkpoints.

The SDK constructs the authorization URL; the host still owns the browser redirect,
callback validation, token storage, and refresh policy. Do not invent a completed
login, register an OAuth application implicitly, or ask for credentials in chat.
When the current host offers only code proposals, explain which host-side steps
remain instead of claiming authentication or external actions occurred.
