# Harnest channels through Fused

Status: proposed implementation plan; no channel APIs or commands below exist yet.
Reviewed against local Harnest and Fused source on 2026-09-16. Local source capabilities must be checked against the deployed Engine and generated SDK versions before rollout.

## Confirmed task decisions

- “Teams” means Microsoft Teams: its messaging app/bot integration for chats and channels, not Fused team management (confirmed by the user on 2026-09-16).

## Outcome and scope

The deliverable is a generic, provider-agnostic `harnest.channels` core: a transport-neutral contract (`ChannelEvent`, `ChannelAdapter`, `ChannelBinding`, `ChannelStore`) that any messaging platform can be wired into. Harnest itself does not ship tied to Slack, Teams, or Fused — those are extensions built on top of the core, not part of it. A team can implement their own adapter against an internal chat system without touching Harnest core, exactly like they would for a custom task or credential provider today.

Slack and Teams are the first two reference extensions, and Fused is the first reference *transport* for them (it already owns provider OAuth, webhook verification, and event delivery, so it is the fastest path to a working adapter). Neither is a hard dependency of the core: a direct-API adapter (Harnest talking to Slack's Events API or Teams' Bot Framework itself, with its own credential handling) must be implementable later using the exact same `ChannelAdapter` contract, with no core changes. Do not introduce a second provider credential store inside the core, and do not let the Fused-backed extension leak Fused-specific assumptions into `ChannelEvent`/`ChannelAdapter`/`ChannelBinding`/`ChannelStore`.

Ship the generic core and its conformance suite first; ship Slack-via-Fused as the first concrete extension proving the contract is sufficient, not as a prerequisite for the contract's shape. Start with direct messages and explicit mentions, text replies, allowlisted installations, and durable processing. Defer proactive notifications, attachments, message edits, shared multi-user sessions, and in-chat approval buttons until their authorization and delivery contracts are tested. Never auto-approve an agent action to make an unsupported channel flow succeed.

## What exists and what does not

| Surface | Verified local behavior | Consequence |
|---|---|---|
| Fused MCP event selection | Explicit webhook allowlists plus `webhook_attachment` are accepted; wildcard event selection is rejected | Existing MCP apps can expose selected event resources. Older skill text saying MCP cannot select webhooks is stale. |
| `fused-cli init --webhook` | Creates a distinct `kind: webhook` config (`apiVersion: fused/v1`, `name`, `services: {<service>: {}}`), applied separately from `kind: mcp` via `fused-cli webhook apply`. Confirmed `WebhookService` fields: `webhooks` (event list), `webhooks_select_all` (bool), `secret`/`secrets` (bucket secret references, never raw values), `event_extraction_path`, `incoming_webhook_config`. Applies over REST at `POST {engine}/webhook-config/apply`, not GraphQL. | `FusedAdminClient.apply_webhook_config` (added; `src/harnest/_fused_admin_client.py`) now issues this directly — no `fused-cli` process needed to register inbound delivery for a service already in the workspace. |
| `webhook_attachment` on `kind: mcp` / SDK apps | A named webhook registration attaches to an MCP server via a top-level `webhook_attachment` field in its config, making that server's resources also carry the registration's inbound events. | `FusedMCPClient.from_openapi(..., webhook_attachment=...)` (added; `packages/harnest-fused/src/harnest_fused/client.py`) threads this through `deploy_server`'s config and through `provision.setup()`. |
| Fused MCP subscriptions | `subscriptions/listen` announces resource changes; `resources/read` uses JetStream `GetLastMsg` | Latest-value recovery is not a complete message history or acknowledged delivery stream. Bursts and disconnects can omit intermediate messages. |
| Harnest MCP subscriptions | Lifecycle-owned listeners, reconnect reads, and in-memory resource digest deduplication (`src/harnest/mcp_subscriptions.py`, `MCPSubscription`/`SubscriptionLifecycle`) | Useful for resource updates; do not change their semantics or present them as a durable chat inbox. A webhook-attached MCP server's events surface through this same mechanism — a channel's live receive path should attach an `MCPSubscription` here, not invent a second listener. Not yet wired: `FusedChannelAdapter.normalize_event` today takes a raw payload directly (fixture-testable offline); connecting it to a live `MCPSubscription` handler is follow-up work. |
| Fused SDK webhooks | SDK-authenticated gRPC stream, stable receiver/version-scoped durable consumer, explicit ACK/NACK, bounded redelivery | Recommended production ingress; reuse this instead of inventing a second event broker. |
| Fused Python receiver | Handler receives payload and ACK/NACK callbacks; stream event ID and readiness are not exposed as useful public handler/lifecycle state | Add the narrow metadata/readiness/shutdown contract needed by the adapter. Validate the generated artifact, not just templates. |
| Harnest runtime | Sessions, task storage, governed in-process invocation, credential providers, extension lifecycle | Reuse these. Background channel ingress still needs an explicit runtime-owned identity and dispatch boundary. |
| Harnest channel support | Shipped: the provider-neutral `harnest.channels` contract (`ChannelEvent`, `ChannelAdapter`, `ChannelBinding`, `ChannelStore`, a memory store, and CLI scaffolding — `src/harnest/channels.py`, `channel_storage.py`, `channel_store_memory.py`) plus a generic Fused-backed adapter (`packages/harnest-fused/src/harnest_fused/channel.py`, registered under extension name `fused`) that connects to any Fused-connected service via a declarative field-path mapping, no per-platform Python class. `send_reply` calls one narrowly configured MCP tool directly through the raw session (`harnest.mcp_resources.resource_session`), deliberately bypassing model-selected tool-use per this doc's own delivery-boundary rule. | Remaining: wiring a binding's live inbound events (via an `MCPSubscription`) into `ChannelStore.admit_event` and agent dispatch — that is Phase 1/2 work below, not yet built. |

Fused accepts the platform webhook after synchronous broker persistence. Harnest's later delivery acknowledgment has a different meaning: Harnest has durably accepted responsibility for processing it.

## Architecture and ownership

```text
Slack / Teams / (any platform)
    │ provider-authenticated events
    ▼
[ pluggable transport: Fused webhook ingress, or a direct-API adapter ]
    │ authenticated event stream; explicit ACK
    ▼
harnest.channels core: ChannelAdapter → durable inbox (ChannelStore) → governed agent run
                                                   │
                                    existing tools authorized for that agent
                                                   │
                              durable reply outbox ← final answer
                                       │ deterministic send via the same transport
                                       ▼
                               original conversation/thread
```

The core (`ChannelEvent`/`ChannelAdapter`/`ChannelBinding`/`ChannelStore`, inbox/outbox, session mapping, dispatch) knows nothing about Slack, Teams, or Fused — it only knows about adapters implementing the contract. Everything platform-specific lives in an extension.

For the Slack/Teams reference extensions built on Fused: Fused owns provider credentials, installation authorization flows, webhook verification/challenges, selected service operations/events, and provider execution policies. A future direct-API extension would own those same responsibilities itself instead, behind the identical adapter interface. Harnest core owns agent routing, sender authorization, conversation/session mapping, work scheduling, agent execution, and reply intent tracking — regardless of which transport an adapter uses. The application owner decides which installations, people, conversations, and agent permissions are allowed.

Keep the channel receiver/sender's service authority separate from model tool authority. Sending an ordinary reply is deterministic runtime behavior, not a second model-selected tool call. Use a narrowly selected Fused SDK app for delivery; do not assume SDK tokens support MCP-style operation allowlist flags. Provider credentials remain in Fused. Harnest stores only execution-token secret references and approved connection identifiers.

Harnest initiates its connection to Fused. The provider-facing webhook URL belongs to Fused, so Harnest does not need public inbound HTTP merely to receive channel messages. A deployed Engine still needs appropriate public ingress and reachable, authenticated streaming transport.

## Proposed Harnest contracts

All names are provisional and belong under `harnest.channels`, not the top-level `harnest` namespace.

- `ChannelEvent`: transport delivery ID, stable provider event ID, event kind, installation scope, sender, conversation/thread, message ID, timestamp, bounded content, and immutable reply address.
- `ChannelAdapter`: lifecycle readiness/close, inbound normalization, supported capabilities, reply rendering, and sending through an authorized transport.
- `ChannelBinding`: adapter/connection references, target root agent, allowed installations/conversations, trigger policy, and identity/session policy.
- `ChannelStore`: async atomic event admission, processing claims/leases, session bindings, reply outbox, receipts, and failed/uncertain states. Supply PostgreSQL/Redis implementations and a custom-provider conformance suite. Memory-only development storage must explicitly warn that restart recovery is unavailable.
- Runtime-owned dispatch capability: admit verified external work and invoke the final Harnest pipeline with an application-resolved principal. Do not expose raw drivers or synthesize an HTTP request to bypass authentication.

Reuse existing task and session providers. Where task scheduling and channel storage cannot share a transaction, commit an inbox row plus dispatch intent atomically in the channel store, then reconcile into the task provider using a stable idempotency key. A crash between these steps must not strand acknowledged messages. Do not introduce an implicit PostgreSQL dependency.

Extensions receive only the scoped channel capability explicitly granted to them. Lifecycle startup must complete storage and runtime readiness before the receiver admits messages; shutdown stops intake first, then drains or releases work, then closes dependencies.

## Message processing contract

1. Fused validates the provider request, resolves the registration, persists the event, and acknowledges the provider. Verify each platform's challenge and authentication behavior during onboarding.
2. The adapter receives an authorized event stream and validates its expected app, attachment, event selection, installation, and payload schema. Unknown senders/installations fail closed; valid transport credentials alone do not authorize agent access.
3. Normalize and deduplicate using platform + installation + stable provider event ID. Fused currently creates a new ingress message UUID per HTTP delivery, so that ID alone cannot collapse provider retries. Require an adapter-defined stable identity when the provider lacks one; do not deduplicate solely by message text.
4. Commit inbox state and pending dispatch intent, then ACK Fused. Already-admitted duplicates may be ACKed without starting another run. Invalid or unauthorized events receive a durable rejection record and no invocation.
5. Resolve the external sender to a Harnest principal and explicitly authorized Fused connected-user/resource binding. Never accept credential selectors, session IDs, or permission grants from message text.
6. Serialize admitted turns per conversation/session, with independent sessions processed concurrently. Persist a stable invocation ID and reattach/reconcile after restart instead of blindly rerunning model tools.
7. Persist the final customer-facing answer and reply intent before sending. Render platform-safe text, bound message length, and prevent accidental mass mentions. Never publish model reasoning, secrets, or raw tool output by default.
8. Send through an exact, reviewed Fused operation and record its receipt. Reuse provider-supported idempotency when available. An ambiguous timeout becomes `delivery_unknown`: reconcile before replay. Do not promise exactly-once external sends where the platform cannot guarantee them.

No acknowledgment waits for the LLM. Fused transport retries, Harnest processing retries, and reply reconciliation are separate responsibilities with separate attempt limits.

## Identity, sessions, and approvals

Default session scope includes application, platform, installation/tenant, conversation, thread, and authorized actor. This avoids silently sharing one user's history or private memory with another participant. Shared-thread sessions require an explicit policy for participant access, memory visibility, and tool authority; private-user credentials must not become shared conversation authority.

Group replies are visible to the group even when a session is actor-scoped. Route only suitably scoped agents into group channels, and require explicit policy before exposing private results there. Ignore bot/self messages and unsupported edit/delete/status events to prevent loops and accidental invocation.

Initial release must fail safely at unsupported approval/client-tool continuations and provide a clear status. Full approval support is a separate acceptance gate: durable pending state, an authenticated approval surface or signed platform interaction, actor reauthorization, expiration, one-time consumption, and resume through existing Harnest continuation ownership. Free-form “yes” is not approval.

## Developer workflow

Proposed commands, run inside the agent folder:

```bash
harnest add channel slack --via fused
harnest channels inspect .
harnest channels test . --fixture ./fixtures/slack-message.json
harnest serve .
```

`--via fused` selects the Fused-backed Slack extension; `--via` is a generic extension-selection flag, not a Fused-specific one — a future direct-API Slack extension, or a Teams extension, or a team's internal chat-system extension, would register under the same `harnest add channel <platform> --via <extension>` shape. `add` scaffolds only local binding/policy files and optional extension dependencies. It does not create external apps, expand scopes, or write credentials. `inspect` verifies selected app versions, event/operation availability, receiver readiness, identity policy, and durable storage without invoking a model or sending messages, and is transport-agnostic. Fixture tests are offline; a separate explicit live option can post into a named disposable conversation.

Reuse Fused's existing configuration and connection workflow for the provider app, webhook attachment, exact event selections, and reply operation. Do not guess Registry slugs, operation IDs, or required scopes. Keep operational setup and public examples in Mintlify, with one Slack quickstart and a compact custom-adapter reference when implemented.

## Delivery phases and acceptance gates

### 1. Build the provider-neutral runtime

Add the channel contract (`ChannelEvent`/`ChannelAdapter`/`ChannelBinding`/`ChannelStore`), compiler/lifecycle integration, explicit dispatch authority, stores, inbox/outbox recovery, session binding, audit signals, and a conformance suite that any adapter (Fused-backed or otherwise) must pass. Test both ADK and LangGraph. Do not add background consumers to CLI inspection or compilation. Keep optional packages out of core installs that do not use channels. This phase must not import anything Fused-, Slack-, or Teams-specific.

### 2. Prove the Fused transport boundary with a Slack extension

Use a disposable Slack installation to build and validate the first concrete `ChannelAdapter` implementation. Inspect imported event schemas, signing/challenge policy, reply operation, and connection routing. Verify provider deadline compliance. Exercise a burst of messages, disconnection, reconnect, multiple receivers, token revocation, and app-version cutover.

Confirm broker retention, initial backlog behavior, retry exhaustion, and how failed deliveries can be recovered. Current receiver identity includes exact app version; upgrades need an explicit drain/cutover policy so a new consumer neither silently loses pending work nor replays old conversations unexpectedly.

Expose delivery identity, event metadata, readiness, terminal authorization failure, and awaited close in the generated Python receiver. Handler success must mean durable Harnest admission, not merely task creation in memory. Only declare production readiness after disconnect and crash tests pass. Treat any Fused-only assumption that leaks into the core contract during this phase as a bug in phase 1, not a reason to special-case Fused in the core.

### 3. Ship Slack through Fused

Support direct messages and allowlisted explicit mentions, stable thread replies, actor isolation, text formatting, deduplication, and loop prevention. Use the existing Fused MCP tools unchanged. Verify one real model turn, one authorized existing MCP action, and one reply in a disposable Slack conversation. This is the first extension shipped, not a prerequisite for the core contract.

### 4. Add durable interactions and Teams

Implement approval resume and cancellation before advertising those capabilities. Prove Teams inbound bot activity, token/audience verification, conversation references, app installation requirements, and outbound bot replies end to end through Fused. Do not assume a Graph API integration or outgoing Teams webhook is equivalent to a conversational bot. Any missing Fused provider contract is explicit work, not a Harnest bypass.

### 5. Evaluate WhatsApp separately

Validate the intended Business Platform use case, geographic eligibility, consent, template/window rules, verification challenges, and attachment handling before committing to support. Current terms distinguish primary AI offerings from ancillary AI and include geographic exceptions; do not advertise unrestricted general-purpose agents.

## Required tests and observability

- Duplicate provider webhook and duplicate broker delivery result in one admitted turn.
- Burst and offline-backlog tests recover every eligible message, not only the latest.
- Crashes before/after admission, ACK, scheduling, invocation completion, and reply delivery are recoverable or explicitly uncertain.
- Same-conversation concurrency and two replicas cannot independently own one turn.
- Installation/tenant/user boundaries, revoked connections, unknown users, and forged reply addresses fail closed.
- Token revocation stops intake and sending; cancellation and shutdown leave no orphan receiver tasks.
- Provider rate limits remain Engine-owned; Harnest does not layer blind provider retries on top.
- Metrics show inbox age, consumer health, retries, processing failures, pending/uncertain replies, and recovery outcomes. Audit committed admission, invocation, and sends without message text, tokens, or sensitive high-cardinality labels.
- Full Harnest quality gates, provider-store conformance, Fused generator/Engine tests, and a disposable live Slack test must pass before release.

## MCP-only delivery later

Do not block the first production channel on inventing another MCP method. If a single MCP connection becomes a product requirement, expose an explicitly negotiated durable event-consumption capability backed by the same Fused delivery machinery: stable consumer identity, ordered bounded history/cursors, acknowledgments, retention-gap errors, and replay rules. Keep standard latest-resource behavior intact and reject unsupported durable capability rather than silently falling back to latest-only delivery.

## Evidence

Local source inspected:

- Harnest: `src/harnest/mcp_subscriptions.py`, `mcp_subscription_http.py`, `mcp_resources.py`, `mcp.py`, `context_agent.py`, `http_routes.py`, `extension_runtime_context.py`, `credentials.py`, `task.py`, `task_storage.py`, `task_store_memory.py`, `testing_storage.py`, `_fused_admin_client.py`, `_fused_auth_client.py`, and `playground_connectors.py`.
- Harnest channels core (shipped this pass): `channels.py`, `channel_storage.py`, `channel_store_memory.py`, `testing_channels.py`, `channel_cli.py`, `cmd/harnest/channels.go`.
- Fused-backed channel extension (shipped this pass): `packages/harnest-fused/src/harnest_fused/channel.py`, `client.py`, `provision.py`.
- Fused CLI: `cli/internal/configfile/parser.go`, especially explicit MCP event selection validation; `fused-cli --help`, `fused-cli init --help`, and `fused-cli webhook --help` run locally (v0.33.1) confirmed the `kind: webhook` config shape, the `--webhook`/`--webhook-attachment`/`--events`/`--secret` flags, and that `init --webhook` writes `.fused/webhooks/<name>.yaml`. Struct field tags (`WebhookService.webhooks`, `webhooks_select_all`, `secret`, `secrets`, `event_extraction_path`, `incoming_webhook_config`) and the `/webhook-config/apply` REST path were confirmed by inspecting the installed `fused-cli` binary's string table; treat this as high-confidence but unverified against a live Engine until `apply_webhook_config` is exercised for real.
- Fused Engine: `engine-release/internal/engine/sandbox/mcp_modern_events.go`, `webhook.go`, and `internal/engine/api/webhook_grpc_handler.go`.
- Fused generator: `backend/generator/templates_python/webhooks/receiver.py`.

Platform references:

- [Slack Events API](https://docs.slack.dev/apis/events-api/): inbound events, identity fields, acknowledgment deadlines, retries, and outbound Web API replies.
- [Teams proactive messaging](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages): installation and conversation prerequisites.
- [WhatsApp Business Solution Terms](https://www.whatsapp.com/legal/business-solution-terms): AI use restrictions and exceptions; recheck before implementation.
