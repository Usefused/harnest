# Studio launch and company configuration

## Launch and validate

```sh
harnest studio pack validate ./company-pack ./team-pack
harnest studio --workspace ./agents --pack ./company-pack --pack ./team-pack
```

The workspace must exist. Both an agent root and a directory containing agents are supported. `--port` selects a loopback port. `harnest-studio` is the standalone entrypoint; it requires Harnest on the device. Check `--help` when working with an older installed release rather than inventing unsupported flags.

Packs are loaded at process startup, not installed by copying them into an agent. One company pack may combine resources, builder defaults, and UI. A minimal settings pack is:

```yaml
apiVersion: harnest.dev/studio-pack/v1
name: company-studio
version: 1.0.0
title: Company Studio
resources: []
builder:
  model: openai/company-coding-model
  api_base: https://ai.company.example/v1
  api_key_env: COMPANY_AI_KEY
  timeout: 240
  max_tokens: 12000
```

Replace example model and endpoint values with the team's actual service. Only set fields the team needs. `api_key_env` is a variable name, not `${NAME}` interpolation and not the credential itself. Do not print resolved credentials. Standard provider environment variables remain usable without a builder-specific key.

## Precedence and TLS

Later loaded packs overlay only declared fields; command-line packs follow embedded packs. Explicit local environment settings override pack defaults:

| Pack field | Environment override |
| --- | --- |
| `model` | `HARNEST_BUILDER_MODEL` |
| `api_base` | `HARNEST_BUILDER_API_BASE` |
| `api_key_env` lookup | `HARNEST_BUILDER_API_KEY` (actual secret value) |
| `ca_bundle` | `HARNEST_BUILDER_CA_BUNDLE` (local PEM path) |
| `timeout` | `HARNEST_BUILDER_TIMEOUT_SECONDS` |
| `max_tokens` | `HARNEST_BUILDER_MAX_TOKENS` |

Request model and budget selections override launch defaults. Timeout is 1–1800 seconds; output budget is 1–131072 tokens. Defaults are 120 seconds and 12000 tokens. `api_base` must not contain embedded credentials or query parameters.

An optional pack-relative `ca_bundle: certificates/company-ca.pem` snapshots trusted PEM certificates. A local CA override replaces that bundle. Include all required CAs, retain hostname/certificate verification, and never put a private key in the pack. This is server trust, not an mTLS client identity.

## Project resources

`resources` entries use `id`, `kind`, `title`, and a `files` mapping from **destination in the agent** to **source in the pack**. Supported kinds are `component`, `extension`, `template`, `agent-skill`, `builder-skill`, `profile`, `service`, and `bundle`.

- Templates include `agent.py` and `config.yaml`, plus required dependencies and instructions.
- Profiles alone use `config` overlays for agent `config.yaml`; they do not configure Studio's builder.
- Bundles use `includes` instead of `files` or `config`. Place a template before its profile. Unqualified IDs name resources in the same pack; `pack-name/resource-id` names another loaded pack.
- Builder skills contain Markdown guidance. Agent Skills remain agent-owned runtime resources.
- Installation is a reviewed project change through Studio. Loading a catalog does not install or execute its Python files. Preserve installation receipts and locally modified files during upgrades.

Use the existing company's pack as the starting point. When a Harnest checkout is available, `examples/studio-packs/company-support/` demonstrates native resources and `examples/studio-packs/retail-demo/` demonstrates another catalog; inspect available directories before copying an example. The public manifest examples are at https://usefused.com/docs/harnest/studio/packs.

## Package and recover

For a company **CLI project pack**, register `ProjectCLI.add_studio(command=("acme",), packs=[Path(__file__).parent / "studio"])` in its installed entrypoint. Ship that Studio directory as package data and declare a pinned `harnest-agent-builder` optional dependency. Teams launch `acme studio --workspace .`; Studio jobs use `acme`, so Create agent runs the company's pack initialization. `init_args=("--team", "support")` supplies non-secret required pack options. Ordinary init/upgrade remains available without Studio installed. Register custom handlers before `add_studio`; otherwise its core command forwarding owns those names. See https://usefused.com/docs/harnest/build/project-packs/cli.

`--safe-ui` removes bundled Studio packs but retains the company CLI and its initialization hooks. It does not turn a company CLI into plain Harnest.

```sh
harnest studio pack package --pack ./company-pack --name acme-studio --version 1.0.0 --output ./dist
```

Packaging includes declared snapshots and an exact Studio dependency. It does not vendor Harnest CLI, Python, credentials, or arbitrary local files. Repeated pack names, even different versions, are invalid; distribute an updated wheel instead. UI modules still require explicit launch-time trust after packaging.

`harnest studio --safe-ui` bypasses external packs entirely, including broken paths, resources, and builder defaults. `?safe-ui=1` keeps the loaded backend pack configuration while recovering the bundled UI. Fix the original configuration and relaunch to verify recovery; do not present safe mode as retaining the same company behavior.
