# qa/adapters/web.py — WebAdapter: wraps Chrome DevTools MCP for web testing

import asyncio
import json
import time

from agents.mcp import MCPServerStdio

from qa.models.common import Platform, TargetApp
from qa.models.knowledge import L1Element


class WebAdapter:
    """Platform adapter for web apps via Chrome DevTools MCP."""

    platform = Platform.WEB

    def __init__(self) -> None:
        self._server: MCPServerStdio | None = None

    # ── Lifecycle ────────────────────────────────────────────

    async def launch(self, app: TargetApp) -> None:
        self._server = MCPServerStdio(
            name="Chrome DevTools MCP",
            params={
                "command": "npx",
                "args": ["-y", "chrome-devtools-mcp@latest"],
            },
            cache_tools_list=True,
            client_session_timeout_seconds=30.0,
        )
        await self._server.__aenter__()

        if app.url:
            await self._call("navigate_page", {"url": app.url})

    async def close(self) -> None:
        if self._server:
            await self._server.__aexit__(None, None, None)
            self._server = None

    # ── Observation ──────────────────────────────────────────

    async def snapshot(self) -> list[dict]:
        raw = await self._call("take_snapshot", {})
        return self._parse_snapshot(raw)

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
                script = f"document.querySelector('{locator.value}')?.click()"
                result = await self._call("evaluate_script", {"expression": script})
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

        # Fallback to CSS + evaluate_script
        css_loc = next((l for l in element.locators if l.strategy == "css"), None)
        if css_loc:
            script = f"""
                const el = document.querySelector('{css_loc.value}');
                if (el) {{ el.value = '{value}'; el.dispatchEvent(new Event('input', {{bubbles: true}})); 'OK'; }}
                else {{ 'ELEMENT_NOT_FOUND'; }}
            """
            result = await self._call("evaluate_script", {"expression": script})
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
        script = f"window.scrollBy({{{'top' if axis == 'y' else 'left'}: {pixels}, behavior: 'smooth'}})"
        await self._call("evaluate_script", {"expression": script})
        await asyncio.sleep(0.5)

    async def navigate_to_screen(self, screen_name: str) -> bool:
        # Web: screen_name is a URL or a relative path
        if screen_name.startswith("http"):
            await self._call("navigate_page", {"url": screen_name})
        else:
            # Try clicking a link/tab with matching text
            script = f"""
                const links = [...document.querySelectorAll('a, button, [role="tab"]')];
                const match = links.find(el => el.textContent.trim().toLowerCase().includes('{screen_name.lower()}'));
                if (match) {{ match.click(); 'OK'; }} else {{ 'NOT_FOUND'; }}
            """
            result = await self._call("evaluate_script", {"expression": script})
            if "NOT_FOUND" in result:
                return False
        await asyncio.sleep(1.5)
        return True

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
        return await self._call("evaluate_script", {"expression": expression})

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
