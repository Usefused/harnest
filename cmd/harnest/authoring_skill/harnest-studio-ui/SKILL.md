---
name: harnest-studio-ui
description: "Customize Harnest Studio through UI packs: themes, layouts, isolated views, trusted modules, or shell replacements. Use for Studio styling, Tailwind or Panda CSS builds, section swaps, and UI recovery; not agent runtime configuration."
---

# Customize Studio through packs

Produce a validated UI pack that changes the requested surface, with reproducible assets and evidence that surrounding Studio behavior remains intact.

## Choose the smallest supported boundary

- Use `ui.themes` for literal colors, fonts, and dimensions; use `ui.layouts` for region order or visibility.
- Use an isolated `mode: frame` for a section that needs only declared state. Compile CSS into its HTML so framework resets cannot affect the surrounding interface.
- Use an explicitly trusted `mode: module` when the view needs Studio commands or host APIs. Trust grants full page authority; manifest permissions do not restrict modules.
- Replace the shell only when the task requires it. The default pack exposes shell, welcome, appearance, and deployment contributions. Sidebar, editor, and assistant are not independently replaceable contributions; expose a slot or replace the shell if those boundaries must change.

Read [references/ui-contract.md](references/ui-contract.md) for manifest fields, slots, replacement rules, module APIs, and recovery. For Tailwind, Panda CSS, or a one-section framework swap, also read [references/section-builds.md](references/section-builds.md).

## Build and prove the change

1. Inspect the loaded packs and target contribution. Preserve unrelated resource and builder configuration. Use `$harnest-studio` when those settings also need changes.
2. Author in a separate pack using the ordinary contract. The default Fused UI uses that same contract; avoid modifying its source for a customization already supported by slots or tokens.
3. Declare every runtime asset, build with locked dependencies, and validate the complete intended pack set. Do not resolve replacement conflicts by reordering packs.
4. Restart Studio with the generated pack. Test an empty workspace for a welcome replacement. Check visible rendering, interaction, declared state, desktop and narrow widths, and default-UI recovery. Report browser validation as unverified when unavailable.
5. Record source inputs, generated files, exact manifest and launch changes, trust requirements, and checks. Distinguish a partial section swap from replacing all of Studio.

Use existing trust authorization; do not add trust solely to fix a styling error. Browser recovery must remain reachable. UI preferences and hidden controls never change backend permissions.

Public documentation: https://usefused.com/docs/harnest/studio/mods
