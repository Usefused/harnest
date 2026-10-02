---
name: harnest-studio
description: Configure Harnest Studio workspaces, loaded packs, company distributions, and builder model, endpoint, credential references, budgets, or CA settings. Use for Studio launch and pack configuration; use harnest-studio-ui for visual customization.
---

# Configure Harnest Studio

Produce a working Studio launch configuration or distributable company pack, with validated manifests and a reproducible launch command.

## Choose the configuration owner

- Inspect the requested workspace, existing `studio-pack.yaml` files, launch arguments, and installed CLI capabilities. Preserve unrelated packs and configuration.
- Put shared builder defaults and reusable project resources in a company pack. Use launch environment overrides for device-local settings. Agent `config.yaml` configures the agent being built, not Studio's builder.
- Use `$harnest-studio-ui` for themes, layouts, custom panels, CSS frameworks, or shell replacement. Use `$harnest-authoring` for agent implementation changes.
- Coding-agent skills installed by `harnest skills install` live outside runtime `skills/`. A pack's `builder-skill` guides Studio's builder; an `agent-skill` is installed into the selected agent. Do not interchange them.

Read [references/configuration.md](references/configuration.md) for launch commands, builder precedence, credential references, pack resources, and company packaging.

## Configure and verify

1. Make the smallest requested configuration change. Keep credential values out of manifests and generated packages; refer to their environment-variable names. Preserve TLS verification.
2. Validate the complete set of packs intended for the launch. Resolve duplicate identities, missing references, incompatible versions, and conflicts explicitly.
3. Launch against the intended workspace, or an isolated workspace for a test. Keep the private launch token out of documentation and screenshots. Restart after changing pack files or launch environment; refreshing the browser does not reload snapshots.
4. Verify the affected behavior. Model changes need a provider call only when that call is authorized; otherwise distinguish validated configuration from live connectivity. UI defaults and backend authority are separate.
5. Report changed files, the launch command without credentials, checks performed, and any unverified behavior. When packaging, verify that the wheel contains the declared snapshots and excludes secrets and local build dependencies.

Use `--safe-ui` when diagnosing external pack failures: it excludes all external packs, including resources and builder defaults. Browser `?safe-ui=1` restores only the interface. Do not silently discard company configuration to make a launch succeed.

Public documentation: https://usefused.com/docs/harnest/studio/packs
