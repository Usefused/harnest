# Chrome sandbox agent

This agent uses an authored `browse_page` tool to open approved sites in a
headless Chromium browser. The browser code runs in a fresh Harnest Docker
sandbox; the model never receives a general code execution tool.

## Run it

1. Start Docker.
2. Set `OPENAI_API_KEY`.
3. Run `harnest env sync examples/chrome-sandbox`.
4. Run `harnest serve examples/chrome-sandbox`.
5. Open `http://127.0.0.1:8080/` and ask the agent to describe
   `https://example.com`.

The first request builds the pinned Playwright image and can take a few
minutes. Add trusted hosts to `ALLOWED_HOSTS` in `tools/browse_page.py` before
using other sites. Page text is untrusted data and must never override the
agent's instructions.

The container has outbound network access because a browser needs it. Harnest
still runs it as a non-root user with a read-only root filesystem, dropped
capabilities, bounded resources, and a fresh container for every call.
