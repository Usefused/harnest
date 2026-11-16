# Acme example pack

A complete Harnest pack: typed options, a configuration contract, a CI
workflow template, a 1 → 2 migration, a company command (`whoami`) and a
Studio welcome screen. The public guide is
[Harnest packs](https://usefused.com/docs/harnest/build/project-packs).

Use it straight from this folder, starting with the first release so the
migration has something to do:

```sh
ACME_DEMO_PACK_VERSION=1 harnest init /tmp/support-bot --pack examples/packs/acme --option team=support
harnest upgrade /tmp/support-bot --pack examples/packs/acme
harnest upgrade /tmp/support-bot --pack examples/packs/acme --apply
harnest pack run examples/packs/acme whoami /tmp/support-bot
harnest studio --pack examples/packs/acme
```

Or build the branded `acme` command and use that instead:

```sh
harnest pack build examples/packs/acme --output dist
pip install dist/harnest_pack_acme-2.0.0-py3-none-any.whl
acme init /tmp/billing-bot --option team=billing
acme whoami /tmp/billing-bot
```

`ACME_DEMO_PACK_VERSION` exists only for this demo; a real company publishes
each pack release. The CI templates assume a runner labelled `acme-agent`.
