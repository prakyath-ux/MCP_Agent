# qa/adapters/mobile.py — MobileAdapter: wraps mobile-mcp + ADB for Android testing

import asyncio
import json
import subprocess
import time

from agents.mcp import MCPServerStdio

from qa.models.common import Platform, TargetApp
from qa.models.knowledge import L1Element

# Nav tab coordinates (fallback — from config)
_NAV_TAB_COORDINATES = {
    "iTELLER": (324, 2229),
    "LOAN": (756, 2229),
    "MORE": (972, 2229),
    "DASHBOARD": (108, 2229),
    "iBRANCH": (540, 2229),
}

_SCREEN_NAME_MAP = {
    "iteller": "iTELLER",
    "loan": "LOAN",
    "more": "MORE",
    "dashboard": "DASHBOARD",
    "ibranch": "iBRANCH",
}


class MobileAdapter:
    """Platform adapter for Android apps via mobile-mcp + ADB."""

    platform = Platform.MOBILE

    def __init__(self, device_id: str = "") -> None:
        self._server: MCPServerStdio | None = None
        self._device_id = device_id
        self._keyboard_active = False

    # ── Lifecycle ────────────────────────────────────────────

    async def launch(self, app: TargetApp) -> None:
        self._device_id = app.device_id or self._device_id
        self._server = MCPServerStdio(
            name="mobile-mcp",
            params={
                "command": "npx",
                "args": ["-y", "@mobilenext/mobile-mcp@latest"],
            },
            cache_tools_list=True,
            client_session_timeout_seconds=30.0,
        )
        await self._server.__aenter__()

        if app.package_name:
            await self._call("mobile_launch_app", {"packageName": app.package_name})

    async def close(self) -> None:
        if self._server:
            await self._server.__aexit__(None, None, None)
            self._server = None

    # ── Observation ──────────────────────────────────────────

    async def snapshot(self) -> list[dict]:
        raw = await self._call("mobile_list_elements_on_screen", {})
        elements = self._parse_elements(raw)

        # Detect keyboard from content frame height
        for el in elements:
            if el.get("identifier") == "android:id/content":
                content_height = el.get("coordinates", {}).get("height", 9999)
                self._keyboard_active = content_height < 2150
                break

        return elements

    async def screenshot(self) -> bytes | None:
        raw = await self._call("mobile_take_screenshot", {})
        return raw.encode() if raw else None

    # ── Interaction ──────────────────────────────────────────

    async def click(self, element: L1Element) -> str:
        # Try to find element by label first (most reliable)
        elements = await self.snapshot()
        el = self._find_by_l1(elements, element)

        if el:
            cx, cy = self._center(el)
            await self._call("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
            return "OK"

        # Fallback to last known coordinates
        if element.last_known_coordinates:
            x = element.last_known_coordinates.get("x", 0)
            y = element.last_known_coordinates.get("y", 0)
            await self._call("mobile_click_on_screen_at_coordinates", {"x": x, "y": y})
            return "OK"

        return "ELEMENT_NOT_FOUND"

    async def fill(self, element: L1Element, value: str) -> str:
        # Find and tap the field
        elements = await self.snapshot()
        el = self._find_by_l1(elements, element)
        if not el:
            return f"ERROR: ELEMENT_NOT_FOUND '{element.element_id}'"

        cx, cy = self._center(el)
        await self._call("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
        await asyncio.sleep(0.3)

        # Long-press to select all existing text
        await self._call("mobile_long_press_on_screen_at_coordinates", {"x": cx, "y": cy})
        await asyncio.sleep(0.2)

        if value:
            await self._call("mobile_type_keys", {"text": value, "submit": False})
        else:
            await self._call("mobile_press_button", {"button": "DEL"})
        await asyncio.sleep(0.3)

        # Verify
        elements = await self.snapshot()
        el = self._find_by_l1(elements, element)
        actual = el.get("text", "unknown") if el else "unknown"
        return f"FILLED: {actual}"

    async def select_option(self, element: L1Element, option: str) -> str:
        await self.dismiss_keyboard()

        elements = await self.snapshot()
        el = self._find_by_l1(elements, element)
        if not el:
            return f"ERROR: ELEMENT_NOT_FOUND '{element.element_id}'"

        cx, cy = self._center(el)
        labels_before = {e.get("text", "") or e.get("label", "") for e in elements}

        # Tap the dropdown
        await self._call("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
        await asyncio.sleep(0.5)

        # Scan for new options
        elements_after = await self.snapshot()
        labels_after = {e.get("text", "") or e.get("label", "") for e in elements_after}
        new_elements = sorted(n for n in (labels_after - labels_before) if n)

        if not new_elements:
            return "ERROR: No options appeared after tapping dropdown"

        # Find and tap the option
        option_el = self._find_by_label(elements_after, option)
        if not option_el:
            # Close by dismissing
            await self.dismiss_keyboard()
            return f"ERROR: Option '{option}' not found. Available: {', '.join(new_elements[:10])}"

        ox, oy = self._center(option_el)
        await self._call("mobile_click_on_screen_at_coordinates", {"x": ox, "y": oy})
        await asyncio.sleep(0.5)
        await self.dismiss_keyboard()

        # Verify
        elements_verify = await self.snapshot()
        dropdown_after = self._find_by_l1(elements_verify, element) or self._find_by_label(elements_verify, option)
        verified = dropdown_after.get("text", "") or dropdown_after.get("label", "") if dropdown_after else "unknown"

        return f"SELECTED: {verified}"

    async def dismiss_keyboard(self) -> None:
        subprocess.run(
            ["adb", "-s", self._device_id, "shell", "input", "keyevent", "111"],
            capture_output=True, timeout=5,
        )
        await asyncio.sleep(0.5)
        self._keyboard_active = False

    async def scroll(self, direction: str = "down") -> None:
        await self._call("mobile_swipe_on_screen", {"direction": direction})
        await asyncio.sleep(0.5)

    async def navigate_to_screen(self, screen_name: str) -> bool:
        nav_label = _SCREEN_NAME_MAP.get(screen_name.lower(), screen_name.upper())
        coords = _NAV_TAB_COORDINATES.get(nav_label)
        if not coords:
            return False

        # Dismiss keyboard first
        await self.dismiss_keyboard()

        # Tap nav tab
        x, y = coords
        await self._call("mobile_click_on_screen_at_coordinates", {"x": x, "y": y})
        await asyncio.sleep(1.5)
        return True

    # ── Element Resolution ───────────────────────────────────

    async def find_element(self, element: L1Element) -> dict | None:
        elements = await self.snapshot()
        return self._find_by_l1(elements, element)

    # ── Script Execution ─────────────────────────────────────

    async def evaluate_script(self, expression: str) -> str:
        return "NOT_SUPPORTED"

    # ── MCP Server Access ────────────────────────────────────

    def get_mcp_server(self):
        return self._server

    # ── Internal helpers ─────────────────────────────────────

    async def _call(self, tool_name: str, args: dict) -> str:
        if not self._server:
            return "ERROR: MCP server not initialized"
        args["device"] = self._device_id
        t0 = time.time()
        result = await self._server.call_tool(tool_name, args)
        elapsed = time.time() - t0
        if elapsed > 5:
            print(f"    ⚠ MCP slow: {tool_name} took {elapsed:.1f}s")
        if result.content and len(result.content) > 0:
            return result.content[0].text
        return ""

    def _parse_elements(self, raw: str) -> list[dict]:
        try:
            start = raw.index("[")
            return json.loads(raw[start:])
        except (ValueError, json.JSONDecodeError):
            return []

    def _center(self, el: dict) -> tuple[int, int]:
        coords = el.get("coordinates", {})
        cx = coords.get("x", 0) + coords.get("width", 0) // 2
        cy = coords.get("y", 0) + coords.get("height", 0) // 2
        return cx, cy

    def _find_by_l1(self, elements: list[dict], l1: L1Element) -> dict | None:
        """Find element using L1 locators in order of confidence."""
        for locator in sorted(l1.locators, key=lambda l: -l.confidence):
            if locator.strategy in ("label", "accessibility_id"):
                found = self._find_by_label(elements, locator.value)
                if found:
                    return found
        # Fallback: try element_id parts
        from qa.knowledge.element_id import parse_element_id
        try:
            _, label_part, _ = parse_element_id(l1.element_id)
            label_text = label_part.replace("_", " ")
            found = self._find_by_label(elements, label_text)
            if found:
                return found
        except ValueError:
            pass
        return None

    def _find_by_label(self, elements: list[dict], label: str) -> dict | None:
        label_lower = label.lower()
        # Exact match
        for el in elements:
            if (el.get("label", "").lower() == label_lower or
                el.get("text", "").lower() == label_lower or
                el.get("identifier", "").lower() == label_lower):
                return el
        # Partial match
        for el in elements:
            if (label_lower in el.get("label", "").lower() or
                label_lower in el.get("text", "").lower()):
                return el
        return None
