"""Expose a narrow browser operation backed by the assigned Chrome sandbox."""

import json
from urllib.parse import urlsplit

from harnest.context import context
from harnest.sandbox import SandboxStatus
from harnest.tool import tool


ALLOWED_HOSTS = frozenset({"example.com", "playwright.dev"})


def _browser_code(url: str) -> str:
    """Serialize the URL as data and bound the page content returned over stdout."""
    payload = json.dumps({"url": url, "allowed_hosts": sorted(ALLOWED_HOSTS)})
    return f'''\
import json
import os
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

request = json.loads({payload!r})
os.environ["HOME"] = "/tmp"
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        chromium_sandbox=False,
        args=["--disable-dev-shm-usage"],
    )
    browser_context = browser.new_context(service_workers="block")

    def admit(route):
        host = (urlsplit(route.request.url).hostname or "").lower()
        route.continue_() if host in request["allowed_hosts"] else route.abort()

    browser_context.route("**/*", admit)
    page = browser_context.new_page()
    response = page.goto(
        request["url"], wait_until="domcontentloaded", timeout=15_000
    )
    result = {{
        "url": page.url,
        "status": response.status if response else None,
        "title": page.title(),
        "text": page.locator("body").inner_text(timeout=5_000)[:4_000],
    }}
    browser_context.close()
    browser.close()
print(json.dumps(result))
'''


@tool
async def browse_page(url: str) -> dict[str, object]:
    """Open an approved HTTP page and return its title and visible text."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or host not in ALLOWED_HOSTS
    ):
        return {"error": "That URL is not on this agent's approved host list."}

    result = await context.sandboxes["chrome"].aexecute(_browser_code(url))
    if result.status != SandboxStatus.SUCCEEDED:
        return {"error": f"The browser sandbox finished with status {result.status.value}."}
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"error": "The browser sandbox returned an invalid result."}
