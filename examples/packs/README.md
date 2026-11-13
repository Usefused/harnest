# Example packs

Each folder is a Harnest pack: `pack.yaml` names it, and optional `pack.py`
adds project hooks and company commands.

- `acme/`: project hooks, a migration, a company command and a Studio screen.
- `company-support/`, `retail-demo/`: Studio-only packs with installable resources.
- `blueprint-ui/`, `tailwind-welcome/`, `team-deployment/`: Studio-only UI packs.

Use any of them with `harnest init|upgrade|studio --pack <folder>`.
