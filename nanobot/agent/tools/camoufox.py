"""Camoufox browser tools: stealth web interaction via anti-detect browser.

Provides a suite of Playwright-based stealth browser tools that bypass
bot-detection (Cloudflare, DataDome, etc.):

- **CamoufoxFetchTool** — fetch & extract page content (same format as WebFetchTool)
- **CamoufoxScreenshotTool** — capture screenshots of protected pages
- **CamoufoxActionTool** — interact with pages (click, type, select, scroll, wait)
- **CamoufoxScriptTool** — execute arbitrary JavaScript on stealth pages

All tools share a **CamoufoxSessionManager** that keeps browser instances alive
across sequential tool calls so the agent can perform multi-step workflows
(login → navigate → extract) without re-launching the browser each time.

Existing WebSearchTool / WebFetchTool in web.py are NOT touched.
"""

import asyncio
import base64
import html
import json
import re
import time as _time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage

# ---------------------------------------------------------------------------
# Shared helpers (same signatures as web.py so both modules stay self-contained)
# ---------------------------------------------------------------------------


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


def _to_markdown(raw_html: str) -> str:
    """Convert HTML to markdown (shared across tools)."""
    text = re.sub(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        lambda m: f'[{_strip_tags(m[2])}]({m[1]})',
        raw_html,
        flags=re.I,
    )
    text = re.sub(
        r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
        lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n',
        text,
        flags=re.I,
    )
    text = re.sub(
        r'<li[^>]*>([\s\S]*?)</li>',
        lambda m: f'\n- {_strip_tags(m[1])}',
        text,
        flags=re.I,
    )
    text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
    text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
    return _normalize(_strip_tags(text))


# ---------------------------------------------------------------------------
# CamoufoxSessionManager — keeps browser sessions alive across tool calls
# ---------------------------------------------------------------------------

class CamoufoxSessionManager:
    """Manage long-lived Camoufox browser sessions.

    Sessions are identified by a string *session_id*.  When a tool requests a
    session that doesn't exist yet, a new browser + page is launched.  Subsequent
    calls with the same *session_id* reuse the existing page, enabling multi-step
    workflows (login → navigate → extract) without re-launching the browser.

    Idle sessions are automatically reaped after ``ttl`` seconds.
    """

    def __init__(self, ttl: float = 300.0):
        self._ttl = ttl
        # session_id → {browser, page, last_used, lock}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._reaper_task: asyncio.Task | None = None

    # -- public API ----------------------------------------------------------

    async def get_or_create(
        self,
        session_id: str,
        *,
        headless: bool = True,
        proxy: dict[str, str] | None = None,
    ) -> Any:
        """Return the Playwright *Page* for *session_id*, creating if needed.

        The returned page is ready for navigation / interaction.
        """
        if session_id in self._sessions:
            entry = self._sessions[session_id]
            entry["last_used"] = _time.monotonic()
            return entry["page"]

        # Lazy import
        from camoufox.async_api import AsyncCamoufox

        cfx_kwargs: dict[str, Any] = {
            "headless": headless,
            "block_webrtc": True,
        }
        if proxy:
            cfx_kwargs["proxy"] = proxy
            cfx_kwargs["geoip"] = True

        browser = await AsyncCamoufox(**cfx_kwargs).__aenter__()
        page = await browser.new_page()

        self._sessions[session_id] = {
            "browser": browser,
            "page": page,
            "last_used": _time.monotonic(),
        }

        # Ensure reaper is running
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

        return page

    async def close_session(self, session_id: str) -> bool:
        """Close a specific session. Returns True if it existed."""
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return False
        await self._close_entry(entry)
        return True

    async def close_all(self) -> int:
        """Close every open session. Returns count closed."""
        count = len(self._sessions)
        for entry in list(self._sessions.values()):
            await self._close_entry(entry)
        self._sessions.clear()
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        return count

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return metadata about open sessions."""
        now = _time.monotonic()
        result = []
        for sid, entry in self._sessions.items():
            try:
                current_url = entry["page"].url
            except Exception:
                current_url = "<unknown>"
            result.append({
                "session_id": sid,
                "url": current_url,
                "idle_seconds": round(now - entry["last_used"], 1),
            })
        return result

    # -- internals -----------------------------------------------------------

    @staticmethod
    async def _close_entry(entry: dict[str, Any]) -> None:
        try:
            await entry["browser"].__aexit__(None, None, None)
        except Exception:
            pass

    async def _reaper_loop(self) -> None:
        """Periodically close sessions that have been idle longer than TTL."""
        while self._sessions:
            await asyncio.sleep(30)
            now = _time.monotonic()
            expired = [
                sid for sid, e in self._sessions.items()
                if now - e["last_used"] > self._ttl
            ]
            for sid in expired:
                logger.debug("Reaping idle camoufox session '{}'", sid)
                await self.close_session(sid)


# Module-level singleton so all tools share the same session pool.
_session_manager = CamoufoxSessionManager()


def get_session_manager() -> CamoufoxSessionManager:
    """Return the module-level session manager singleton."""
    return _session_manager


# ---------------------------------------------------------------------------
# Mixin: shared progress-callback plumbing
# ---------------------------------------------------------------------------

class _CamoufoxProgressMixin:
    """Shared progress-callback plumbing for all Camoufox tools."""

    _progress_callback: Callable[[OutboundMessage], Awaitable[None]] | None
    _channel: str
    _chat_id: str

    def _init_progress(
        self,
        progress_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._progress_callback = progress_callback
        self._channel = ""
        self._chat_id = ""

    def set_progress_callback(
        self, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        self._progress_callback = callback

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    async def _emit_status(self, status: str) -> None:
        logger.debug("camoufox status: {}", status)
        if not self._progress_callback or not self._channel:
            return
        try:
            await self._progress_callback(OutboundMessage(
                channel=self._channel,
                chat_id=self._chat_id,
                content=status,
                metadata={"_progress": True, "_tool_hint": True},
            ))
        except Exception:
            pass  # progress is best-effort


# ---------------------------------------------------------------------------
# Helper: resolve page (session-aware or ephemeral)
# ---------------------------------------------------------------------------

async def _resolve_page(
    session_id: str | None,
    headless: bool,
    proxy: dict[str, str] | None,
) -> tuple[Any, bool]:
    """Return (page, is_ephemeral).

    If *session_id* is given, the page comes from the session manager and
    ``is_ephemeral`` is ``False``.  Otherwise a one-shot browser is launched
    and the caller is responsible for closing it.
    """
    if session_id:
        page = await _session_manager.get_or_create(
            session_id, headless=headless, proxy=proxy,
        )
        return page, False

    from camoufox.async_api import AsyncCamoufox

    cfx_kwargs: dict[str, Any] = {"headless": headless, "block_webrtc": True}
    if proxy:
        cfx_kwargs["proxy"] = proxy
        cfx_kwargs["geoip"] = True

    browser = await AsyncCamoufox(**cfx_kwargs).__aenter__()
    page = await browser.new_page()
    # Stash browser ref on page so caller can close it
    page._cfx_browser = browser  # type: ignore[attr-defined]
    return page, True


async def _close_ephemeral(page: Any) -> None:
    """Close a page that was opened without a session."""
    browser = getattr(page, "_cfx_browser", None)
    if browser:
        try:
            await browser.__aexit__(None, None, None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CamoufoxFetchTool
# ---------------------------------------------------------------------------

class CamoufoxFetchTool(_CamoufoxProgressMixin, Tool):
    """Fetch a URL using the Camoufox anti-detect browser.

    Unlike the lightweight ``web_fetch`` (httpx + readability), this tool
    launches a real headless Firefox instance with fingerprint spoofing so it
    can handle pages protected by Cloudflare, DataDome, and similar WAFs.

    The response JSON schema is intentionally identical to ``WebFetchTool``
    so the agent can treat both tools interchangeably.

    Supports optional *sessionId* to reuse a browser session across calls.
    """

    name = "camoufox_fetch"
    description = (
        "Fetch a URL using a stealth anti-detect browser (Camoufox). "
        "Use this when web_fetch fails due to bot-detection / Cloudflare / JS-rendered pages. "
        "Returns the same JSON format as web_fetch. "
        "Supports sessionId for multi-step workflows (login then fetch)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {
                "type": "string",
                "enum": ["markdown", "text", "html"],
                "default": "markdown",
                "description": "Content extraction mode (markdown, text, or raw html)",
            },
            "maxChars": {
                "type": "integer",
                "minimum": 100,
                "description": "Max characters to return (default 50 000)",
            },
            "waitSeconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 30,
                "description": "Extra seconds to wait after page load for JS rendering (default 2)",
            },
            "waitForSelector": {
                "type": "string",
                "description": "CSS selector to wait for before extracting content (e.g. '#main-content')",
            },
            "headless": {
                "type": "boolean",
                "description": "Run browser headless (default true)",
            },
            "proxy": {
                "type": "object",
                "description": 'Playwright proxy dict, e.g. {"server": "http://host:port"}',
                "properties": {
                    "server": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                },
            },
            "sessionId": {
                "type": "string",
                "description": "Reuse a named browser session (for multi-step workflows). Omit for one-shot fetch.",
            },
        },
        "required": ["url"],
    }

    def __init__(
        self,
        max_chars: int = 50_000,
        default_wait: float = 2.0,
        progress_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self.max_chars = max_chars
        self.default_wait = default_wait
        self._init_progress(progress_callback)

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        url: str,
        extractMode: str = "markdown",
        maxChars: int | None = None,
        waitSeconds: float | None = None,
        waitForSelector: str | None = None,
        headless: bool = True,
        proxy: dict[str, str] | None = None,
        sessionId: str | None = None,
        **kwargs: Any,
    ) -> str:
        max_chars = maxChars or self.max_chars
        wait = waitSeconds if waitSeconds is not None else self.default_wait

        # Validate URL
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps(
                {"error": f"URL validation failed: {error_msg}", "url": url},
                ensure_ascii=False,
            )

        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: F401 — availability check
        except ImportError:
            return json.dumps(
                {
                    "error": (
                        "camoufox is not installed. "
                        "Run: pip install -U camoufox[geoip] && python -m camoufox fetch"
                    ),
                    "url": url,
                },
                ensure_ascii=False,
            )

        domain = urlparse(url).netloc

        try:
            await self._emit_status(f"🦊 Launching stealth browser for {domain}…")

            page, ephemeral = await _resolve_page(sessionId, headless, proxy)

            try:
                await self._emit_status(f"🌐 Navigating to {domain}…")
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                # Wait for specific selector if requested
                if waitForSelector:
                    await self._emit_status(f"⏳ Waiting for selector '{waitForSelector}'…")
                    try:
                        await page.wait_for_selector(waitForSelector, timeout=int(wait * 1000) or 10_000)
                    except Exception:
                        pass  # best-effort; continue with extraction

                # Extra wait for JS-heavy pages
                if wait > 0:
                    await self._emit_status(
                        f"⏳ Waiting {wait:.0f}s for JS rendering on {domain}…"
                    )
                    await page.wait_for_timeout(int(wait * 1000))

                await self._emit_status(f"📄 Extracting content from {domain}…")

                final_url = page.url
                status = response.status if response else 0
                raw_html = await page.content()
            finally:
                if ephemeral:
                    await _close_ephemeral(page)

            # --- content extraction ---
            if extractMode == "html":
                text = raw_html
                extractor = "camoufox+raw"
            else:
                from readability import Document

                doc = Document(raw_html)
                if extractMode == "markdown":
                    content = _to_markdown(doc.summary())
                else:
                    content = _strip_tags(doc.summary())

                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "camoufox+readability"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            await self._emit_status(f"✅ Fetched {domain} — {len(text):,} chars")

            return json.dumps(
                {
                    "url": url,
                    "finalUrl": final_url,
                    "status": status,
                    "extractor": extractor,
                    "truncated": truncated,
                    "length": len(text),
                    "text": text,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            await self._emit_status(f"❌ Failed to fetch {domain}: {e}")
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    # kept for backward compat but delegates to module-level helper
    def _to_markdown(self, raw_html: str) -> str:
        return _to_markdown(raw_html)


# ---------------------------------------------------------------------------
# CamoufoxScreenshotTool
# ---------------------------------------------------------------------------

class CamoufoxScreenshotTool(_CamoufoxProgressMixin, Tool):
    """Take a screenshot of a URL using the Camoufox anti-detect browser.

    Returns the screenshot as a base64-encoded PNG (or JPEG) string inside a
    JSON envelope.  Useful for visually inspecting pages behind bot-detection,
    capturing rendered state after JS execution, or debugging page layouts.
    """

    name = "camoufox_screenshot"
    description = (
        "Take a screenshot of a URL using a stealth anti-detect browser (Camoufox). "
        "Returns base64-encoded image. Use when you need to see what a protected page "
        "looks like, or to capture visual state during a multi-step workflow."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to screenshot. Omit if using an existing sessionId and the page is already navigated.",
            },
            "fullPage": {
                "type": "boolean",
                "description": "Capture the full scrollable page (default false, viewport only)",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector of a specific element to screenshot (e.g. '#chart')",
            },
            "waitSeconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 30,
                "description": "Extra seconds to wait before taking the screenshot (default 2)",
            },
            "format": {
                "type": "string",
                "enum": ["png", "jpeg"],
                "default": "png",
                "description": "Image format",
            },
            "quality": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "JPEG quality (1-100). Only used when format is jpeg.",
            },
            "savePath": {
                "type": "string",
                "description": "Optional file path to save the screenshot to disk (relative to workspace).",
            },
            "headless": {
                "type": "boolean",
                "description": "Run browser headless (default true)",
            },
            "proxy": {
                "type": "object",
                "description": 'Playwright proxy dict',
                "properties": {
                    "server": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                },
            },
            "sessionId": {
                "type": "string",
                "description": "Reuse a named browser session.",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        default_wait: float = 2.0,
        progress_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self.default_wait = default_wait
        self._init_progress(progress_callback)

    async def execute(
        self,
        url: str | None = None,
        fullPage: bool = False,
        selector: str | None = None,
        waitSeconds: float | None = None,
        format: str = "png",
        quality: int | None = None,
        savePath: str | None = None,
        headless: bool = True,
        proxy: dict[str, str] | None = None,
        sessionId: str | None = None,
        **kwargs: Any,
    ) -> str:
        wait = waitSeconds if waitSeconds is not None else self.default_wait

        if url:
            is_valid, error_msg = _validate_url(url)
            if not is_valid:
                return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        if not url and not sessionId:
            return json.dumps({"error": "Either 'url' or 'sessionId' is required."}, ensure_ascii=False)

        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: F401
        except ImportError:
            return json.dumps({"error": "camoufox is not installed."}, ensure_ascii=False)

        domain = urlparse(url).netloc if url else "(session)"

        try:
            await self._emit_status(f"📸 Preparing screenshot for {domain}…")

            page, ephemeral = await _resolve_page(sessionId, headless, proxy)

            try:
                if url:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                if wait > 0:
                    await page.wait_for_timeout(int(wait * 1000))

                # Screenshot options
                ss_kwargs: dict[str, Any] = {"type": format}
                if format == "jpeg" and quality:
                    ss_kwargs["quality"] = quality
                if fullPage and not selector:
                    ss_kwargs["full_page"] = True

                if selector:
                    await self._emit_status(f"📸 Screenshotting element '{selector}'…")
                    element = await page.query_selector(selector)
                    if not element:
                        return json.dumps({"error": f"Selector '{selector}' not found on page."}, ensure_ascii=False)
                    screenshot_bytes = await element.screenshot(**ss_kwargs)
                else:
                    screenshot_bytes = await page.screenshot(**ss_kwargs)

                current_url = page.url
            finally:
                if ephemeral:
                    await _close_ephemeral(page)

            # Optionally save to disk
            if savePath:
                save_target = Path(savePath)
                save_target.parent.mkdir(parents=True, exist_ok=True)
                save_target.write_bytes(screenshot_bytes)
                await self._emit_status(f"💾 Screenshot saved to {savePath}")

            b64 = base64.b64encode(screenshot_bytes).decode("ascii")

            await self._emit_status(f"✅ Screenshot captured — {len(screenshot_bytes):,} bytes")

            return json.dumps(
                {
                    "url": current_url,
                    "format": format,
                    "size_bytes": len(screenshot_bytes),
                    "saved_to": savePath or None,
                    "image_base64": b64,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            await self._emit_status(f"❌ Screenshot failed for {domain}: {e}")
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CamoufoxActionTool
# ---------------------------------------------------------------------------

class CamoufoxActionTool(_CamoufoxProgressMixin, Tool):
    """Interact with a web page using the Camoufox anti-detect browser.

    Supports a sequence of actions: click, type, select, scroll, wait,
    hover, and navigate.  This enables multi-step workflows like logging in,
    filling forms, or navigating through paginated results — all while
    bypassing bot-detection.

    Best used with a *sessionId* so state persists across calls.
    """

    name = "camoufox_action"
    description = (
        "Interact with a web page using a stealth browser (Camoufox). "
        "Perform actions like click, type, select, scroll, hover, navigate, and wait. "
        "Use sessionId to maintain state across multiple calls (e.g. login then scrape). "
        "Returns the page state after all actions complete."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to navigate to before performing actions. Omit to act on current page in session.",
            },
            "actions": {
                "type": "array",
                "description": "Ordered list of actions to perform on the page.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["click", "type", "select", "scroll", "wait", "hover", "navigate", "press_key"],
                            "description": "Action type",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the target element (for click, type, select, hover)",
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to type (for 'type' action) or URL (for 'navigate') or key name (for 'press_key')",
                        },
                        "value": {
                            "type": "string",
                            "description": "Value to select (for 'select' action on <select> elements)",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down"],
                            "description": "Scroll direction (default 'down')",
                        },
                        "amount": {
                            "type": "integer",
                            "description": "Scroll amount in pixels (default 500)",
                        },
                        "seconds": {
                            "type": "number",
                            "description": "Seconds to wait (for 'wait' action, default 1)",
                        },
                        "waitForSelector": {
                            "type": "string",
                            "description": "CSS selector to wait for after this action completes",
                        },
                    },
                    "required": ["action"],
                },
            },
            "extractAfter": {
                "type": "boolean",
                "description": "Extract page content after actions complete (default true)",
            },
            "extractMode": {
                "type": "string",
                "enum": ["markdown", "text", "html"],
                "default": "markdown",
                "description": "Content extraction mode after actions",
            },
            "maxChars": {
                "type": "integer",
                "minimum": 100,
                "description": "Max characters to return (default 30 000)",
            },
            "headless": {
                "type": "boolean",
                "description": "Run browser headless (default true)",
            },
            "proxy": {
                "type": "object",
                "description": 'Playwright proxy dict',
                "properties": {
                    "server": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                },
            },
            "sessionId": {
                "type": "string",
                "description": "Named browser session to reuse. Highly recommended for multi-step workflows.",
            },
        },
        "required": ["actions"],
    }

    def __init__(
        self,
        max_chars: int = 30_000,
        progress_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self.max_chars = max_chars
        self._init_progress(progress_callback)

    async def execute(
        self,
        actions: list[dict[str, Any]],
        url: str | None = None,
        extractAfter: bool = True,
        extractMode: str = "markdown",
        maxChars: int | None = None,
        headless: bool = True,
        proxy: dict[str, str] | None = None,
        sessionId: str | None = None,
        **kwargs: Any,
    ) -> str:
        max_chars = maxChars or self.max_chars

        if url:
            is_valid, error_msg = _validate_url(url)
            if not is_valid:
                return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        if not url and not sessionId:
            return json.dumps({"error": "Either 'url' or 'sessionId' is required."}, ensure_ascii=False)

        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: F401
        except ImportError:
            return json.dumps({"error": "camoufox is not installed."}, ensure_ascii=False)

        try:
            await self._emit_status(f"🦊 Preparing stealth browser for actions…")

            page, ephemeral = await _resolve_page(sessionId, headless, proxy)
            action_results: list[dict[str, Any]] = []

            try:
                if url:
                    await self._emit_status(f"🌐 Navigating to {urlparse(url).netloc}…")
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                for i, act in enumerate(actions):
                    action_type = act.get("action", "")
                    result = await self._perform_action(page, act, i)
                    action_results.append(result)

                    # Post-action wait for selector
                    if wfs := act.get("waitForSelector"):
                        try:
                            await page.wait_for_selector(wfs, timeout=10_000)
                        except Exception:
                            action_results[-1]["warning"] = f"waitForSelector '{wfs}' timed out"

                final_url = page.url
                title = await page.title()

                # Extract content if requested
                extracted_text = None
                if extractAfter:
                    await self._emit_status("📄 Extracting page content…")
                    raw_html = await page.content()

                    if extractMode == "html":
                        extracted_text = raw_html
                    else:
                        from readability import Document
                        doc = Document(raw_html)
                        if extractMode == "markdown":
                            extracted_text = _to_markdown(doc.summary())
                        else:
                            extracted_text = _strip_tags(doc.summary())
                        if doc.title():
                            extracted_text = f"# {doc.title()}\n\n{extracted_text}"

                    if extracted_text and len(extracted_text) > max_chars:
                        extracted_text = extracted_text[:max_chars]

            finally:
                if ephemeral:
                    await _close_ephemeral(page)

            await self._emit_status(f"✅ Completed {len(actions)} action(s)")

            response: dict[str, Any] = {
                "finalUrl": final_url,
                "title": title,
                "actionsPerformed": len(actions),
                "actionResults": action_results,
            }
            if extracted_text is not None:
                response["text"] = extracted_text
                response["length"] = len(extracted_text)

            return json.dumps(response, ensure_ascii=False)

        except Exception as e:
            await self._emit_status(f"❌ Action failed: {e}")
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    async def _perform_action(
        self, page: Any, act: dict[str, Any], index: int
    ) -> dict[str, Any]:
        """Execute a single action and return a result dict."""
        action_type = act.get("action", "")
        selector = act.get("selector")
        result: dict[str, Any] = {"action": action_type, "index": index, "ok": True}

        try:
            if action_type == "click":
                if not selector:
                    raise ValueError("'click' requires a 'selector'")
                await self._emit_status(f"🖱️ Clicking '{selector}'…")
                await page.click(selector, timeout=10_000)
                result["selector"] = selector

            elif action_type == "type":
                if not selector:
                    raise ValueError("'type' requires a 'selector'")
                text = act.get("text", "")
                await self._emit_status(f"⌨️ Typing into '{selector}'…")
                await page.fill(selector, text, timeout=10_000)
                result["selector"] = selector
                result["typed_length"] = len(text)

            elif action_type == "select":
                if not selector:
                    raise ValueError("'select' requires a 'selector'")
                value = act.get("value", "")
                await self._emit_status(f"📋 Selecting '{value}' in '{selector}'…")
                await page.select_option(selector, value, timeout=10_000)
                result["selector"] = selector
                result["value"] = value

            elif action_type == "hover":
                if not selector:
                    raise ValueError("'hover' requires a 'selector'")
                await page.hover(selector, timeout=10_000)
                result["selector"] = selector

            elif action_type == "scroll":
                direction = act.get("direction", "down")
                amount = act.get("amount", 500)
                delta = amount if direction == "down" else -amount
                await page.mouse.wheel(0, delta)
                result["direction"] = direction
                result["amount"] = amount

            elif action_type == "wait":
                seconds = act.get("seconds", 1)
                await self._emit_status(f"⏳ Waiting {seconds}s…")
                await page.wait_for_timeout(int(seconds * 1000))
                result["seconds"] = seconds

            elif action_type == "navigate":
                nav_url = act.get("text", "")
                if not nav_url:
                    raise ValueError("'navigate' requires 'text' (the URL)")
                await self._emit_status(f"🌐 Navigating to {urlparse(nav_url).netloc}…")
                await page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000)
                result["navigated_to"] = nav_url

            elif action_type == "press_key":
                key = act.get("text", "")
                if not key:
                    raise ValueError("'press_key' requires 'text' (the key name, e.g. 'Enter')")
                await page.keyboard.press(key)
                result["key"] = key

            else:
                result["ok"] = False
                result["error"] = f"Unknown action type: {action_type}"

        except Exception as e:
            result["ok"] = False
            result["error"] = str(e)

        return result


# ---------------------------------------------------------------------------
# CamoufoxScriptTool
# ---------------------------------------------------------------------------

class CamoufoxScriptTool(_CamoufoxProgressMixin, Tool):
    """Execute JavaScript on a page using the Camoufox anti-detect browser.

    This is the most flexible tool — it lets the agent run arbitrary JS in the
    context of a stealth-loaded page.  Useful for extracting structured data,
    interacting with SPAs, or calling page-level APIs that aren't exposed
    through simple DOM queries.
    """

    name = "camoufox_script"
    description = (
        "Execute JavaScript on a page loaded in a stealth browser (Camoufox). "
        "Use for advanced data extraction, SPA interaction, or calling page APIs. "
        "The script runs in the page context and its return value is serialized to JSON."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to navigate to before running the script. Omit to run on current session page.",
            },
            "script": {
                "type": "string",
                "description": (
                    "JavaScript code to execute in the page context. "
                    "Must be an expression or IIFE that returns a value. "
                    "Example: '(() => { return document.title; })()'"
                ),
            },
            "waitSeconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 30,
                "description": "Seconds to wait after page load before running script (default 2)",
            },
            "headless": {
                "type": "boolean",
                "description": "Run browser headless (default true)",
            },
            "proxy": {
                "type": "object",
                "description": 'Playwright proxy dict',
                "properties": {
                    "server": {"type": "string"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                },
            },
            "sessionId": {
                "type": "string",
                "description": "Named browser session to reuse.",
            },
        },
        "required": ["script"],
    }

    def __init__(
        self,
        default_wait: float = 2.0,
        progress_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self.default_wait = default_wait
        self._init_progress(progress_callback)

    async def execute(
        self,
        script: str,
        url: str | None = None,
        waitSeconds: float | None = None,
        headless: bool = True,
        proxy: dict[str, str] | None = None,
        sessionId: str | None = None,
        **kwargs: Any,
    ) -> str:
        wait = waitSeconds if waitSeconds is not None else self.default_wait

        if url:
            is_valid, error_msg = _validate_url(url)
            if not is_valid:
                return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        if not url and not sessionId:
            return json.dumps({"error": "Either 'url' or 'sessionId' is required."}, ensure_ascii=False)

        try:
            from camoufox.async_api import AsyncCamoufox  # noqa: F401
        except ImportError:
            return json.dumps({"error": "camoufox is not installed."}, ensure_ascii=False)

        domain = urlparse(url).netloc if url else "(session)"

        try:
            await self._emit_status(f"🦊 Preparing stealth browser for script on {domain}…")

            page, ephemeral = await _resolve_page(sessionId, headless, proxy)

            try:
                if url:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                if wait > 0:
                    await page.wait_for_timeout(int(wait * 1000))

                await self._emit_status("🔧 Executing JavaScript…")
                result = await page.evaluate(script)

                current_url = page.url
            finally:
                if ephemeral:
                    await _close_ephemeral(page)

            # Serialize the result
            try:
                result_json = json.dumps(result, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                result_json = str(result)

            await self._emit_status(f"✅ Script executed on {domain}")

            return json.dumps(
                {
                    "url": current_url,
                    "result": json.loads(result_json) if isinstance(result, (dict, list)) else result,
                    "resultType": type(result).__name__,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            await self._emit_status(f"❌ Script failed on {domain}: {e}")
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CamoufoxSessionTool — manage browser sessions
# ---------------------------------------------------------------------------

class CamoufoxSessionTool(_CamoufoxProgressMixin, Tool):
    """Manage Camoufox browser sessions.

    List, close, or close-all stealth browser sessions.  Sessions are created
    implicitly when any Camoufox tool is called with a *sessionId*; this tool
    lets the agent inspect and clean up sessions explicitly.
    """

    name = "camoufox_session"
    description = (
        "Manage stealth browser sessions. "
        "List active sessions, close a specific session, or close all sessions. "
        "Sessions are created automatically when using sessionId with other camoufox tools."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["list", "close", "close_all"],
                "description": "Session management command",
            },
            "sessionId": {
                "type": "string",
                "description": "Session to close (required for 'close' command)",
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        progress_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._init_progress(progress_callback)

    async def execute(
        self,
        command: str,
        sessionId: str | None = None,
        **kwargs: Any,
    ) -> str:
        mgr = get_session_manager()

        if command == "list":
            sessions = mgr.list_sessions()
            return json.dumps(
                {"sessions": sessions, "count": len(sessions)},
                ensure_ascii=False,
            )

        elif command == "close":
            if not sessionId:
                return json.dumps({"error": "'sessionId' is required for 'close' command."}, ensure_ascii=False)
            closed = await mgr.close_session(sessionId)
            return json.dumps(
                {"closed": closed, "sessionId": sessionId},
                ensure_ascii=False,
            )

        elif command == "close_all":
            count = await mgr.close_all()
            await self._emit_status(f"🧹 Closed {count} browser session(s)")
            return json.dumps(
                {"closed_count": count},
                ensure_ascii=False,
            )

        else:
            return json.dumps({"error": f"Unknown command: {command}"}, ensure_ascii=False)
