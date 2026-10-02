# Build a section with a CSS framework

Studio accepts built assets, not a framework name. Select the requested framework and retain its locked build dependencies. A framework swap requires rebuilding that pack; it does not require converting the rest of Studio.

## Isolated welcome workflow

In a Harnest source checkout, inspect `examples/studio-packs/tailwind-welcome/`. Otherwise use the complete public walkthrough at https://usefused.com/docs/harnest/studio/tailwind. Do not assume example files are installed alongside the CLI.

The example's exact files are:

| File | Purpose |
| --- | --- |
| `studio-pack.yaml` | `welcome` frame replacing `fused-studio/welcome`, requesting connection state |
| `package.json`, `package-lock.json` | Pinned Tailwind CLI and compiler; `build` and `check` commands |
| `src/input.css` | Tailwind import and explicit source detection |
| `src/welcome.html` | Section markup, complete utility class names, CSS marker, state listener |
| `build.mjs` | Compile CSS and embed it into HTML; `--check` detects stale output |
| `assets/welcome.html` | Self-contained generated runtime asset |
| `.gitignore` | Exclude `node_modules/` |

Tailwind v4 example input:

```css
@import "tailwindcss" source(none);
@source "./welcome.html";
```

The template's `<style>/* STUDIO_TAILWIND_CSS */</style>` marker receives the compiled stylesheet. Keep license comments. Do not add a CDN script or relative CSS link: both are blocked by the frame policy. Tailwind's reset remains inside the frame. No `ui.styles`, trust flag, Vite configuration, or Studio source edit is needed for this example.

From the example directory:

```sh
npm ci
npm run build
npm run check
harnest studio pack validate .
mkdir -p /tmp/harnest-tailwind-agents
harnest studio --workspace /tmp/harnest-tailwind-agents --pack .
```

Use an appropriate isolated test directory. Do not combine the Blueprint welcome replacement with this example: both claim the same target. Do not launch in safe UI mode when verifying a custom view. Browser refresh does not replace a running process's snapshot.

Check connection status, keyboard interaction, compiled computed styles, unchanged surrounding UI, desktop/narrow layout, and default recovery. Frame responsive breakpoints use the frame's width; long content scrolls within the frame. Record exactly what was changed, including generated output and whether any shell changes were needed.

## Panda CSS or another build system

Use the framework's supported compiler to generate the chosen section's static CSS and optional JavaScript. Keep framework configuration and code generation inside the pack's source project. Embed the final CSS and any required bundled script in the isolated HTML, and list that HTML in `ui.assets`. Test the actual compiler output rather than relabeling a Tailwind example as Panda CSS. Consult current official framework documentation when choosing versions and build commands.

For a section that needs Studio commands, use the trusted module contract instead. Shared-document CSS must avoid unrelated selectors and global resets; a utility prefix alone does not isolate a framework reset. Decide on scoped output or a supported component isolation boundary, and verify the result under Studio's content security policy. Do not weaken the host policy to make a framework load.

Full shell replacement is a separate scope: own the shell layout, slots, state publication, and required command implementations. Do not promise that existing sidebar/editor/assistant internals are replaceable one-by-one through today's manifest.

## React components

Trusted module entries can mount React with `createRoot(host)`. A service receives no container: create one module-owned root, then render the dialog and controls as React components. Bundle React, React DOM, and compiled JSX into declared production assets; do not rely on global React or unresolved browser package imports. Call `root.unmount()` during contribution cleanup. Register commands synchronously in `activate` rather than waiting for a component effect, because the shell becomes interactive once activation finishes. The deployment guide includes the service adapter: https://usefused.com/docs/harnest/studio/deployment-ui
