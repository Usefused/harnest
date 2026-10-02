# Studio UI pack contract

## Manifest and ownership

Use a regular `harnest.dev/studio-pack/v1` manifest with a `ui` block:

```yaml
apiVersion: harnest.dev/studio-pack/v1
name: team-ui
version: 1.0.0
title: Team Studio
resources: []
ui:
  apiVersion: harnest.dev/studio-ui/v1
  assets: [assets/welcome.html]
  contributions:
    - id: welcome
      title: Team welcome
      slot: welcome
      mode: frame
      entry: assets/welcome.html
      replaces: fused-studio/welcome
      permissions: [state.connection]
```

Runtime text assets live under `assets/` and must be explicitly listed: HTML, JS, CSS, JSON, or TXT. Limits are 128 assets, 1 MiB per file, and 8 MiB total. Links and traversal are rejected. Assets are immutable snapshots; edits require restarting Studio. Build inputs and `node_modules` are not runtime assets.

| Slot | Contract |
| --- | --- |
| `welcome` | One resolved view; default shell shows it with no selected project |
| `workspace.tabs` | Additional views presented as tabs by the default shell |
| `inspector.sections` | Additional sections in the default inspector |
| `shell` | One resolved trusted module implementing the workspace |
| `service` | Trusted module with a null mount container |

IDs are `pack-name/local-id`. `replaces` must identify an existing contribution in the same slot. Missing targets, cycles, two claimants for one target, and multiple shell/welcome owners fail composition. Pack order cannot select a winner. Bundled IDs are `fused-studio/shell`, `fused-studio/welcome`, `fused-studio/appearance`, and `fused-studio/deployment`. Appearance and deployment are service contributions.

## Themes and layouts

```yaml
ui:
  apiVersion: harnest.dev/studio-ui/v1
  themes:
    - id: ocean
      title: Ocean
      tokens:
        accent: '#76b7ff'
        accent-ink: '#081729'
        font-ui: 'Inter, sans-serif'
  layouts:
    - id: assistant-first
      title: Assistant first
      order: [inspector, workspace, sidebar]
      hidden: []
```

Merge these fields into an existing `ui` mapping; do not duplicate the YAML key. Common tokens include `bg`, `panel`, `soft`, `hover`, `line`, `text`, `muted`, `accent`, `accent-ink`, `danger`, `font-ui`, `font-code`, `font-size`, `editor-bg`, `editor-gutter`, and `editor-size`. Values are literals, not `url()`, CSS functions, or arbitrary stylesheet syntax. `studio-` token names are reserved.

Order lists all three regions exactly once; `hidden` may include sidebar or inspector. The default shell applies column order on desktop; at 960px or narrower its assistant/inspector uses a collapsible drawer. Appearance selection is per workspace in browser storage: preview, Save, Cancel, and reset. One selected theme overlays bundled tokens. This preference does not automatically theme isolated frames.

## Frames

Frames are sandboxed with scripts but without same-origin access. Embed all CSS and JavaScript directly in the HTML. Network calls and external styles, scripts, and fonts are blocked. Data images are permitted. There is no command bridge.

Listen for a parent `message` whose type is `studio:init`, verify `event.source === parent`, and obtain `event.ports[0]`. Its messages carry `type: studio:state`, `name`, and `value`. Only requested permissions are sent:

- `state.connection`: `connection` value `{ ready }`.
- `state.project`: `project` value `{ id, name, framework }`, or null.

No tokens, source, filesystem paths, or provider credentials are sent. State listeners should handle disconnected or null states.

## Trusted modules

Change `mode` to `module`, use a declared `.js` entry, and export `activate(studio, host)`. Return a cleanup function for listeners, subscriptions, registered commands, and owned views. `ui.styles` lists declared `.css` assets loaded into the shared document. Launch with `--trust-ui-pack team-ui` only when the user has authorized that pack's page access. Without trust, modules and global styles are omitted; frames/themes/layouts remain.

Core context APIs:

- `mountHTML(host, path)` mounts a declared fragment relative to the module.
- `request(path, method?, body?)` and `transport(path, options)` provide authenticated JSON/streaming access relative to `/api/`.
- `state.publish(name, value)` and `state.subscribe(name, listener)` share cloned snapshots.
- `commands.register(id, handler)` and `commands.execute(id, ...args)` supply command routing.
- `slots.register(name, adapter)` lets the shell present `(host, contribution)`.
- `navigation.guard(handler)` registers a synchronous unsaved-change guard.
- `appearance.catalog/current/preview/save/reset` expose appearance controls.

Subscriptions and registrations return disposers. The default shell publishes project/connection and registers `studio.project.create`, `studio.project.refresh`, and `studio.file.open`; a replacement shell must supply any commands its contributions expect. It declares `data-studio-region` regions and `data-studio-slot` containers. Do not assume arbitrary default-shell DOM IDs exist in a replacement.

The shell also publishes `jobs` snapshots and provides `studio.editor.isDirty`, `studio.jobs.run(body)`, `studio.notice(message)`, and `studio.proposal.review(projectId, proposal)`. Job submission returns a job, not a deployment result; observe its terminal state. Proposal review presents proposed changes without applying them automatically. These interfaces are for trusted modules, not frame permissions.

To own deployment UI, replace `fused-studio/deployment` in `slot: service` and register `studio.deployment.open`. Create and dispose your own dialog, subscribe to project/jobs, and release the command on cleanup. Default deployment is opt-in via `HARNEST_ENABLE_DEPLOYMENT=true`; custom UI cannot bypass the server gate. Check unsaved changes, review the selected environment, and submit `manifest_revision` from the matching overview for apply. Never deploy on module activation or on opening the modal. The runnable `examples/studio-packs/team-deployment/` has its own options and review/apply buttons. Full contract: https://usefused.com/docs/harnest/studio/deployment-ui

## Validation and recovery

```sh
harnest studio pack validate ./team-ui
harnest studio --workspace ./agents --pack ./team-ui
```

Validation inspects declarations without executing modules. Add the trust flag only for authorized module packs. Use an empty existing workspace to test welcome. Test `?safe-ui=1` to restore bundled UI while preserving backend pack configuration; `--safe-ui` excludes every external pack and its builder/resource settings. Public reference: https://usefused.com/docs/harnest/studio/mods
