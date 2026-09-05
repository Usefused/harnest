# Harnest Hatchet Extension

The official Hatchet durable-workflow integration for
[Harnest](https://docs.usefused.com/harnest), built and maintained by Fused.
It lets an agent submit, inspect, wait for, and cancel work executed by a
separately operated Hatchet runtime.

The extension keeps Hatchet workers outside the Harnest process. Provider jobs
remain independently durable when an invocation ends or a Harnest replica
restarts, while Harnest owns continuation recovery, invocation scoping, and
privacy-safe audit events.

Version 0.1.x supports Harnest 0.13.x and 0.14.x and Hatchet SDK 1.x. The
extension requires an invocation-scoped `hatchet` credential for normal calls
and `HATCHET_CLIENT_TOKEN` as the application service credential used to recover
pending jobs during startup.

See the [Harnest documentation](https://docs.usefused.com/harnest) for extension
installation, credential configuration, and runtime guidance.
