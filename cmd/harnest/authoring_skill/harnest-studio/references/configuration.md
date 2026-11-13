# Studio launch and company configuration

## Launch and validate

```sh
harnest pack validate ./company-pack ./team-pack
harnest studio --workspace ./agents --pack ./company-pack --pack ./team-pack
```

The workspace must exist. Both an agent root and a directory containing agents are supported. `--port` selects a loopback port. `harnest-studio` is the standalone entrypoint; it requires Harnest on the device. Check `--help` when working with an older installed release rather than inventing unsupported flags.

A Harnest pack is one folder: `pack.yaml` (identity, plus Studio content under `studio:`) and optional `pack.py` (init and migration hooks and company commands). Studio reads only `pack.yaml` and the files it names; it never imports `pack.py`. Packs are loaded at process startup, not installed by copying them into an agent. When Studio creates an agent, every `--pack` also runs that pack's init hooks, exactly like `harnest init --pack`. A minimal settings pack is:

```yaml
apiVersion: harnest.dev/pack/v1
name: company-studio
version: 1.0.0
title: Company Studio
studio:
  builder:
    model: openai/company-coding-model
    api_base: https://ai.company.example/v1
    api_key_env: COMPANY_AI_KEY
    timeout: 240
    max_tokens: 12000
```

`studio-pack.yaml` is no longer supported: move its `builder`, `ui` and `resources` under `studio:` and keep `name`, `version`, `title`, `description` and `requires` at the top level.

Replace example model and endpoint values with the team's actual service. Only set fields the team needs. `api_key_env` is a variable name, not `${NAME}` interpolation and not the credential itself. Do not print resolved credentials. Standard provider environment variables remain usable without a builder-specific key.

## Precedence and TLS

Later loaded packs overlay only declared fields, in `--pack` order. Explicit local environment settings override pack defaults:

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

`studio.resources` entries use `id`, `kind`, `title`, and a `files` mapping from **destination in the agent** to **source in the pack**. Supported kinds are `component`, `extension`, `template`, `agent-skill`, `builder-skill`, `profile`, `service`, and `bundle`.

- Templates include `agent.py` and `config.yaml`, plus required dependencies and instructions.
- Profiles alone use `config` overlays for agent `config.yaml`; they do not configure Studio's builder.
- Bundles use `includes` instead of `files` or `config`. Place a template before its profile. Unqualified IDs name resources in the same pack; `pack-name/resource-id` names another loaded pack.
- Builder skills contain Markdown guidance. Agent Skills remain agent-owned runtime resources.
- Installation is a reviewed project change through Studio. Loading a catalog does not install or execute its Python files. Preserve installation receipts and locally modified files during upgrades.

Use the existing company's pack as the starting point. When a Harnest checkout is available, `examples/packs/company-support/` demonstrates native resources, `examples/packs/retail-demo/` demonstrates another catalog and `examples/packs/acme/` combines project hooks with a Studio screen; inspect available directories before copying an example. The public manifest examples are at https://usefused.com/docs/harnest/studio/packs.

## Package and recover

A pack and its company command are the same thing. Build the pack into a wheel that installs its branded command (`command:` in `pack.yaml`, default the pack name):

```sh
harnest pack build ./company-pack --output ./dist
pip install ./dist/harnest_pack_company_pack-1.0.0-py3-none-any.whl
```

`acme init|upgrade|studio` run Harnest with that pack attached, the pack's own `@pack.command` handlers run as `acme <name>`, and every other command is passed to Harnest unchanged. The command calls the `harnest` executable on PATH, or `HARNEST_CLI` when set. Pass required pack options with `--option key=value`, for example `acme studio --option team=support`. `harnest pack build` validates the studio sub-pack and ships the whole pack folder except caches, build output, virtual environments and `.env` files; it does not vendor Harnest, Python or credentials. Repeated pack names, even different versions, are invalid; distribute an updated wheel instead. UI modules still require explicit launch-time trust with `--trust-ui-pack`. `ProjectCLI`, `add_studio` and `harnest studio pack package` were removed with no compatibility layer.

To recover from a broken pack, relaunch without its `--pack` and run `harnest pack validate` on it. In the browser, `?safe-ui=1` (the "Restore default UI" link) keeps the loaded backend pack configuration while recovering the bundled UI. Fix the original configuration and relaunch to verify recovery.
