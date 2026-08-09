"""HTML -> PNG via a pooled headless Chromium.

Launching Chromium costs far more than rendering a page, so one browser is kept
alive for the process and each render gets a fresh context. The instance is
re-launched automatically if it dies, and a semaphore bounds how many pages
exist at once so a burst of `.proof` calls cannot exhaust memory.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .html import build_html
from .model import Conversation

log = logging.getLogger("chatshot.render")

#: Subpixel antialiasing produces colour fringing that looks wrong in a
#: screenshot, so it is disabled in favour of grayscale AA.
_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-lcd-text",
    "--font-render-hinting=none",
    "--force-color-profile=srgb",
    "--hide-scrollbars",
]

_READY_TIMEOUT_MS = 15_000
_MAX_CONCURRENT = 2


class RenderError(RuntimeError):
    pass


def device_scale(options) -> float:
    """Device pixel ratio for a render.

    Kept as a float on purpose: `int()` here silently turned a 1.75x request
    into a 1x render. Playwright accepts a fractional device_scale_factor.
    """
    try:
        scale = float(options.scale)
    except (TypeError, ValueError):
        scale = 1.0
    return max(1.0, scale)


def viewport_for(options) -> dict:
    return {
        "width": int(options.width) + 2 * int(options.padding) + 40,
        "height": 900,
    }


_MISSING_LIB_MARKER = "error while loading shared libraries: "


def launch_failure_hint(detail: str) -> str:
    """A one-line, actionable summary of a Chromium launch failure.

    Playwright reports a launch failure with several thousand characters of
    command line and call log. Relaying that verbatim to Telegram is useless (and
    too long to send), while the single line that matters is usually a missing
    shared library.
    """
    if _MISSING_LIB_MARKER in detail:
        library = detail.split(_MISSING_LIB_MARKER, 1)[1].split(":", 1)[0].strip()
        return (
            f"Chromium is missing the system library {library}. "
            "Run: sudo $(which python) -m playwright install-deps chromium"
        )
    first_line = next((ln.strip() for ln in detail.splitlines() if ln.strip()), "")
    return (first_line[:300] or "Chromium failed to start.")


class Renderer:
    def __init__(self, max_concurrent: int = _MAX_CONCURRENT) -> None:
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_concurrent)

    # -- lifecycle ---------------------------------------------------------
    async def _browser_ready(self):
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._browser is not None:
                log.warning("chromium died; relaunching")
                self._browser = None
            if self._playwright is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as error:  # pragma: no cover
                    raise RenderError(
                        "playwright is not installed: pip install playwright "
                        "&& python -m playwright install chromium"
                    ) from error
                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(args=_ARGS)
            except Exception as error:  # noqa: BLE001
                # Leaving the driver running after a failed launch keeps a
                # subprocess alive whose transport then complains at interpreter
                # shutdown ("Event loop is closed"), so tear it down and let the
                # next attempt start clean.
                try:
                    await self._playwright.stop()
                finally:
                    self._playwright = None
                raise RenderError(launch_failure_hint(f"{error}")) from error
            return self._browser

    async def start(self) -> None:
        """Warm the browser so the first render is not slow."""
        await self._browser_ready()

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None

    # -- rendering ---------------------------------------------------------
    async def png(
        self,
        conversation: Conversation,
        *,
        emoji_src=None,
        timeout_ms: int = _READY_TIMEOUT_MS,
    ) -> bytes:
        html = build_html(conversation, emoji_src=emoji_src)
        options = conversation.options
        browser = await self._browser_ready()

        async with self._slots:
            context = await browser.new_context(
                device_scale_factor=device_scale(options),
                viewport=viewport_for(options),
                color_scheme="dark" if options.theme == "dark" else "light",
            )
            try:
                page = await context.new_page()
                await page.set_content(html, wait_until="load")
                try:
                    await page.wait_for_selector(
                        'html[data-ready="1"]', timeout=timeout_ms
                    )
                except Exception:  # noqa: BLE001
                    # Fonts or an image never settled. Rendering anyway is far
                    # better than returning nothing; only the timestamp inset
                    # may be slightly off.
                    log.warning("render readiness timed out; capturing anyway")
                shot = page.locator("#shot")
                return await shot.screenshot(type="png", timeout=timeout_ms)
            finally:
                await context.close()


_renderer: Optional[Renderer] = None


def get_renderer() -> Renderer:
    """Process-wide renderer. Safe to call from anywhere in the event loop."""
    global _renderer
    if _renderer is None:
        _renderer = Renderer()
    return _renderer


async def render_png(conversation: Conversation, *, emoji_src=None) -> bytes:
    return await get_renderer().png(conversation, emoji_src=emoji_src)


async def close_renderer() -> None:
    """Close the process-wide renderer, if one was ever created.

    Called on shutdown so a `.proof` does not leave a Chromium process behind.
    """
    global _renderer
    if _renderer is None:
        return
    renderer, _renderer = _renderer, None
    await renderer.close()


def render_png_sync(conversation: Conversation, *, emoji_src=None) -> bytes:
    """Blocking helper for the CLI and tests."""
    from playwright.sync_api import sync_playwright

    html = build_html(conversation, emoji_src=emoji_src)
    options = conversation.options
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=_ARGS)
        try:
            context = browser.new_context(
                device_scale_factor=device_scale(options),
                viewport=viewport_for(options),
                color_scheme="dark" if options.theme == "dark" else "light",
            )
            page = context.new_page()
            page.set_content(html, wait_until="load")
            try:
                page.wait_for_selector('html[data-ready="1"]', timeout=_READY_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                log.warning("render readiness timed out; capturing anyway")
            return page.locator("#shot").screenshot(type="png")
        finally:
            browser.close()
