"""HTTP control plane for one visible, persistent Linux desktop and Chrome tab."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import re
import secrets
import subprocess

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from mss import mss
from mss.tools import to_png
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from url_policy import allowed_url


WIDTH = 1280
HEIGHT = 800
ALLOWED_HOSTS = frozenset(
    host.strip().lower() for host in os.environ.get("ALLOWED_HOSTS", "").split(",")
    if host.strip()
)
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_+ -]{1,64}$")


class Point(BaseModel):
    """Bound a mouse click to the desktop display."""

    x: int = Field(ge=0, lt=WIDTH)
    y: int = Field(ge=0, lt=HEIGHT)


class TypedText(BaseModel):
    """Bound one desktop text insertion."""

    text: str = Field(min_length=1, max_length=4000)


class KeyChord(BaseModel):
    """Represent one xdotool key chord."""

    key: str = Field(min_length=1, max_length=64)


class Navigation(BaseModel):
    """Represent one browser navigation."""

    url: str = Field(min_length=1, max_length=2048)


def _allowed_url(url: str) -> bool:
    """Apply the optional host filter to a top-level URL or intercepted request."""
    return allowed_url(url, ALLOWED_HOSTS)


async def _authorize(authorization: str | None = Header(default=None)) -> None:
    """Restrict the host-published control API to its owning agent instance."""
    expected = "Bearer " + os.environ["DESKTOP_TOKEN"]
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=403, detail="desktop access denied")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Launch Chrome once on the live X11 display and close it with the desktop."""
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch_persistent_context(
            "/tmp/chrome-profile",
            channel="chrome",
            headless=False,
            viewport={"width": WIDTH, "height": HEIGHT - 60},
            service_workers="block",
            args=["--disable-dev-shm-usage"],
        )
        app.state.browser = browser
        app.state.page = browser.pages[0] if browser.pages else await browser.new_page()
        app.state.lock = asyncio.Lock()

        if ALLOWED_HOSTS:
            # Route interception is only needed for an authored host list.
            # Normal browser use must allow a page's redirects and subresources.
            async def admit(route):
                """Keep routed browser requests inside the configured host list."""
                if _allowed_url(route.request.url):
                    await route.continue_()
                else:
                    await route.abort()

            await browser.route("**/*", admit)
        yield
    finally:
        if "browser" in locals():
            await browser.close()
        await playwright.stop()


app = FastAPI(lifespan=_lifespan)


@app.get("/healthz", dependencies=[Depends(_authorize)])
async def healthz() -> dict[str, bool]:
    """Report readiness only after the visible Chrome context has launched."""
    return {"ready": True}


def _capture() -> bytes:
    """Capture the actual X11 display, including non-browser windows."""
    with mss() as camera:
        frame = camera.grab({"left": 0, "top": 0, "width": WIDTH, "height": HEIGHT})
        return to_png(frame.rgb, frame.size)


@app.get("/screenshot", dependencies=[Depends(_authorize)])
async def screenshot() -> Response:
    """Return a bounded full-desktop PNG."""
    async with app.state.lock:
        png = await asyncio.to_thread(_capture)
    return Response(png, media_type="image/png")


async def _xdotool(*arguments: str) -> None:
    """Run a fixed desktop input program without invoking a shell."""
    result = await asyncio.to_thread(
        subprocess.run, ["xdotool", *arguments], capture_output=True, timeout=5, check=False
    )
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail="desktop input failed")


@app.post("/click", dependencies=[Depends(_authorize)])
async def click(point: Point) -> dict[str, bool]:
    """Click one display coordinate and keep input ordered with screenshots."""
    async with app.state.lock:
        await _xdotool("mousemove", str(point.x), str(point.y), "click", "1")
    return {"ok": True}


@app.post("/type", dependencies=[Depends(_authorize)])
async def type_text(value: TypedText) -> dict[str, bool]:
    """Type into the focused window through X11."""
    async with app.state.lock:
        await _xdotool("type", "--clearmodifiers", "--", value.text)
    return {"ok": True}


@app.post("/key", dependencies=[Depends(_authorize)])
async def key(value: KeyChord) -> dict[str, bool]:
    """Press a validated X11 key chord."""
    if not KEY_PATTERN.fullmatch(value.key):
        raise HTTPException(status_code=422, detail="invalid key chord")
    async with app.state.lock:
        await _xdotool("key", "--clearmodifiers", value.key)
    return {"ok": True}


@app.post("/navigate", dependencies=[Depends(_authorize)])
async def navigate(value: Navigation) -> dict[str, object]:
    """Navigate the same headed Chrome tab and return bounded page facts."""
    if not _allowed_url(value.url):
        raise HTTPException(status_code=422, detail="URL must be HTTP(S) and match DESKTOP_ALLOWED_HOSTS if set")
    async with app.state.lock:
        try:
            response = await app.state.page.goto(value.url, wait_until="domcontentloaded", timeout=15000)
            return {
                "url": app.state.page.url,
                "status": response.status if response else None,
                "title": await app.state.page.title(),
                "text": (await app.state.page.locator("body").inner_text(timeout=5000))[:4000],
            }
        except Exception as error:
            raise HTTPException(status_code=502, detail=f"browser navigation failed ({type(error).__name__})") from None
