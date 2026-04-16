# qa/adapters/web.py — WebAdapter: wraps Chrome DevTools MCP for web testing

import asyncio
import json
import time

from agents.mcp import MCPServerStdio, create_static_tool_filter

from qa.adapters.snapshot_filter import filter_snapshot
from qa.models.common import Platform, TargetApp
from qa.models.knowledge import L1Element


class WebAdapter:
    """Platform adapter for web apps via Chrome DevTools MCP."""

    platform = Platform.WEB

    def __init__(self) -> None:
        self._server: MCPServerStdio | None = None

    # ── Lifecycle ────────────────────────────────────────────

    async def launch(self, app: TargetApp) -> None:
        # Clean up any leftover chrome process / lock file from a previous run
        # that was Ctrl+C'd. Without this, persistent profile mode would error
        # with "browser already running for /path/to/chrome-profile".
        self._cleanup_stale_chrome()

        # Block tools that the LLM keeps reaching for as "shortcuts" and
        # that bypass our compound logic (OCR wait, modal cascade, etc.).
        # Raw `upload_file` in particular leaves the app in a broken state
        # — use upload_file_for_field compound tool instead.
        # Tab management tools hidden so LLM doesn't create new tabs.
        blocked_tools = ["upload_file", "new_page", "close_page", "list_pages", "select_page"]

        self._server = MCPServerStdio(
            name="Chrome DevTools MCP",
            params={
                "command": "npx",
                # NOTE: no --isolated → uses persistent profile so site
                # permissions ("Allow camera/storage/etc") persist across runs.
                # Stale locks are handled by _cleanup_stale_chrome above.
                # --no-performance-crux: suppresses Google CrUX telemetry noise.
                # --no-usage-statistics: skips usage telemetry.
                "args": [
                    "-y", "chrome-devtools-mcp@latest",
                    "--no-performance-crux",
                    "--no-usage-statistics",
                ],
            },
            cache_tools_list=True,
            client_session_timeout_seconds=30.0,
            tool_filter=create_static_tool_filter(blocked_tool_names=blocked_tools),
        )
        await self._server.__aenter__()

        if app.url:
            await self._call("navigate_page", {"url": app.url})

    @staticmethod
    def _cleanup_stale_chrome() -> None:
        """Kill leftover chrome-devtools-mcp processes and remove the profile
        lock file so we can reuse the persistent profile cleanly."""
        import subprocess
        import os
        from pathlib import Path

        # Best-effort kill — don't error if nothing to kill.
        for pattern in ("chrome-devtools-mcp", "Google Chrome for Testing"):
            subprocess.run(
                ["pkill", "-f", pattern],
                capture_output=True,
                check=False,
            )

        # Remove the SingletonLock that chrome leaves behind on uncleaned exits.
        profile_dir = Path.home() / ".cache" / "chrome-devtools-mcp" / "chrome-profile"
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock = profile_dir / lock_name
            try:
                if lock.exists() or lock.is_symlink():
                    os.remove(lock)
            except OSError:
                pass

    async def close(self) -> None:
        if self._server:
            await self._server.__aexit__(None, None, None)
            self._server = None

    # ── Observation ──────────────────────────────────────────

    async def snapshot(self) -> list[dict]:
        raw = await self._call("take_snapshot", {})
        trimmed = filter_snapshot(raw)
        return self._parse_snapshot(trimmed)

    async def raw_snapshot_text(self) -> str:
        """Return the trimmed accessibility tree as text — for LLM context."""
        raw = await self._call("take_snapshot", {})
        return filter_snapshot(raw)

    async def screenshot(self) -> bytes | None:
        raw = await self._call("take_screenshot", {})
        return raw.encode() if raw else None

    # ── Interaction ──────────────────────────────────────────

    async def click(self, element: L1Element) -> str:
        # Try locators in confidence order
        for locator in sorted(element.locators, key=lambda l: -l.confidence):
            if locator.strategy == "uid":
                result = await self._call("click", {"uid": locator.value})
                if "error" not in result.lower():
                    return "OK"
            elif locator.strategy == "css":
                # Use evaluate_script to click by CSS selector
                fn = f"() => {{ document.querySelector('{locator.value}')?.click(); return 'OK'; }}"
                result = await self._call("evaluate_script", {"function": fn})
                if "error" not in result.lower():
                    return "OK"
        return "ELEMENT_NOT_FOUND"

    async def fill(self, element: L1Element, value: str) -> str:
        # Try uid first
        uid_loc = next((l for l in element.locators if l.strategy == "uid"), None)
        if uid_loc:
            result = await self._call("fill", {"uid": uid_loc.value, "value": value})
            if "error" not in result.lower():
                return f"FILLED: {value}"

        # Fallback to CSS + evaluate_script with React-safe native setter
        css_loc = next((l for l in element.locators if l.strategy == "css"), None)
        if css_loc:
            fn = (
                "() => {"
                f"  const el = document.querySelector('{css_loc.value}');"
                "   if (!el) return 'ELEMENT_NOT_FOUND';"
                "   const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
                "   const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
                f"  setter.call(el, '{value}');"
                "   el.dispatchEvent(new Event('input', {bubbles: true}));"
                "   el.dispatchEvent(new Event('change', {bubbles: true}));"
                "   el.blur();"
                "   return 'OK';"
                "}"
            )
            result = await self._call("evaluate_script", {"function": fn})
            if "OK" in result:
                return f"FILLED: {value}"

        return f"ERROR: Could not fill '{element.element_id}'"

    async def select_option(self, element: L1Element, option: str) -> str:
        uid_loc = next((l for l in element.locators if l.strategy == "uid"), None)
        if uid_loc:
            result = await self._call("select_option", {"uid": uid_loc.value, "value": option})
            if "error" not in result.lower():
                return f"SELECTED: {option}"
        return f"ERROR: Could not select '{option}' on '{element.element_id}'"

    async def dismiss_keyboard(self) -> None:
        pass  # No-op on web — browser handles keyboard natively

    async def scroll(self, direction: str = "down") -> None:
        pixels = 500 if direction in ("down", "right") else -500
        axis = "y" if direction in ("up", "down") else "x"
        key = "top" if axis == "y" else "left"
        fn = f"() => {{ window.scrollBy({{{key}: {pixels}, behavior: 'smooth'}}); return 'OK'; }}"
        await self._call("evaluate_script", {"function": fn})
        await asyncio.sleep(0.5)

    async def navigate_to_screen(self, screen_name: str) -> bool:
        # Case 1: absolute URL → direct navigation
        if screen_name.startswith("http"):
            await self._call("navigate_page", {"url": screen_name})
            await asyncio.sleep(1.5)
            return True

        # Case 2: text-match a link / button / tab whose visible text matches.
        # Works for in-page tabs (e.g. TECU page 2's section buttons).
        target = screen_name.lower()
        # Escape single quotes for safe JS string embedding.
        target_js = target.replace("\\", "\\\\").replace("'", "\\'")
        fn = (
            "() => {"
            "  const els = [...document.querySelectorAll('a, button, [role=\"tab\"]')];"
            f"  const match = els.find(el => el.textContent.trim().toLowerCase().includes('{target_js}'));"
            "   if (match) { match.click(); return 'OK'; } return 'NOT_FOUND';"
            "}"
        )
        result = await self._call("evaluate_script", {"function": fn})
        if "OK" in result:
            await asyncio.sleep(1.5)
            return True

        # Case 3: no matching tab — try clicking a wizard advance button
        # (Save & Continue / Continue / Next). Used when moving between
        # validation-gated pages of a multi-step form. The current page's
        # required fields must be filled with valid data; otherwise the
        # button may be disabled or the click has no effect.
        advance_fn = (
            "() => {"
            "  const labels = ["
            "    'Save and Continue', 'Save & Continue',"
            "    'Continue', 'Next Step', 'Next', 'Proceed'"
            "  ];"
            "  const btns = [...document.querySelectorAll('button, [role=\"button\"]')];"
            "  for (const label of labels) {"
            "    const btn = btns.find(b => {"
            "      const t = (b.textContent || '').trim();"
            "      return (t === label || t.includes(label))"
            "        && !b.disabled && b.offsetParent !== null;"
            "    });"
            "    if (btn) { btn.click(); return 'ADVANCED:' + label; }"
            "  }"
            "  return 'NO_ADVANCE_BUTTON';"
            "}"
        )
        adv_result = await self._call("evaluate_script", {"function": advance_fn})
        if "ADVANCED:" in adv_result:
            # Page transitions take longer than in-page tab switches — give
            # the app time to validate, submit, and render the next page.
            await asyncio.sleep(3.0)
            return True
        return False

    # ── Element Resolution ───────────────────────────────────

    async def find_element(self, element: L1Element) -> dict | None:
        elements = await self.snapshot()
        for locator in sorted(element.locators, key=lambda l: -l.confidence):
            if locator.strategy == "uid":
                for el in elements:
                    if el.get("uid") == locator.value:
                        return el
            elif locator.strategy in ("css", "xpath"):
                # Match by name/label from the element
                from qa.knowledge.element_id import parse_element_id
                try:
                    _, label_part, _ = parse_element_id(element.element_id)
                    label_text = label_part.replace("_", " ").lower()
                    for el in elements:
                        if label_text in (el.get("name", "").lower() + el.get("text", "").lower()):
                            return el
                except ValueError:
                    pass
        return None

    # ── Script Execution ─────────────────────────────────────

    async def evaluate_script(self, expression: str) -> str:
        # Auto-wrap non-arrow bodies so callers can pass either shape.
        body = expression.strip()
        if not body.startswith("()"):
            body = f"() => {{ return ({body}); }}"
        return await self._call("evaluate_script", {"function": body})

    # ── MCP Server Access ────────────────────────────────────

    def get_mcp_server(self):
        return self._server

    # ── Internal helpers ─────────────────────────────────────

    async def _call(self, tool_name: str, args: dict) -> str:
        if not self._server:
            return "ERROR: MCP server not initialized"
        t0 = time.time()
        result = await self._server.call_tool(tool_name, args)
        elapsed = time.time() - t0
        if elapsed > 5:
            print(f"    ⚠ MCP slow: {tool_name} took {elapsed:.1f}s")
        if result.content and len(result.content) > 0:
            return result.content[0].text
        return ""

    def _parse_snapshot(self, raw: str) -> list[dict]:
        """Parse Chrome DevTools accessibility tree snapshot into element dicts."""
        # The snapshot format varies — try JSON array first, then parse tree
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Accessibility tree is text-based — return as-is wrapped in a dict
            return [{"raw_snapshot": raw}]
