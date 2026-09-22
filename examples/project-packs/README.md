# Company CLI fixture

This runnable example exercises the project-pack contract. The public guide is
[Company project packs](https://docs.usefused.com/harnest/build/project-packs).

From a Harnest authoring environment, with the matching `harnest` executable on
PATH, initialise using the old example pack and then upgrade using the current
pack:

```sh
ACME_DEMO_PACK_VERSION=1 python examples/project-packs/acme.py init /tmp/support-bot --team support
python examples/project-packs/acme.py upgrade /tmp/support-bot
python examples/project-packs/acme.py upgrade /tmp/support-bot --apply
```

Use `HARNEST_CLI=/path/to/harnest` to select an executable. The v1/v2 switch exists
only for this fixture; a real company publishes versioned pack packages. The
sample CI assumes a company runner labelled `acme-agent` with Harnest installed.
Tests also compile the resulting agent for ADK and LangGraph without model calls.
