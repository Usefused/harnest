# Deferred sandbox-provider work

This engineering note preserves deliberately deferred work while Harnest
stabilizes its provider-neutral sandbox contract. It is not a public product
roadmap or a release commitment.

## Packaging boundary

- The `docker` Harnest Extension owns its framework-neutral provider factory,
  daemon SDK dependency, and `sandbox.provider` capability. Core Harnest exposes
  only the provider contract and policy types.
- Retain the extension's identity, budget, cleanup, and typed-result guarantees
  independently of the core release cadence.
- Build a separate Buildah Harnest Extension against the same contract after the
  Docker extraction proves provider packaging, installation, and conformance.
- Keep both implementations optional. Core Harnest owns policy types,
  capability negotiation, framework adapters, cancellation, and result
  contracts—not a container daemon or SDK.

## Provider conformance

Create a reusable provider suite covering:

- execution, invocation, and session identity isolation;
- deadline and cancellation behavior across admission, startup, execution, and
  cleanup;
- output, file, resource-budget, and teardown guarantees;
- network modes, exact host and port allowlists, repeated DNS resolution, and
  blocking of private, loopback, link-local, and metadata addresses;
- honest failure when a requested capability is unavailable.

Provider extensions may use stronger controls, including CIDRs, DNS pinning,
namespaces, firewall rules, or daemon-specific policy. They must map those
controls to the minimum Harnest guarantees without claiming application-level
request filtering as sandbox enforcement.

## Known Docker follow-ups

- Re-run the complete real-container matrix after extraction, including failure
  cleanup and retained-scope eviction.

## Explicitly out of scope

Browser automation products and their task/session APIs are not part of this
provider work. Harnest remains a general harness for browser and non-browser
sandboxes alike.
