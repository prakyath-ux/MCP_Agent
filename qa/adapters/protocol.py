# qa/adapters/protocol.py — PlatformAdapter Protocol (structural subtyping)

from typing import Protocol, runtime_checkable

from qa.models.common import Platform, TargetApp
from qa.models.knowledge import L1Element


@runtime_checkable
class PlatformAdapter(Protocol):
    """What every platform backend must provide to a pipeline.

    Web and Mobile implement this independently — no base class inheritance.
    Python checks structural conformance at runtime via isinstance().
    """

    platform: Platform

    # ── Lifecycle ────────────────────────────────────────────

    async def launch(
        self,
        app: TargetApp,
        *,
        extra_blocked_tools: list[str] | None = None,
    ) -> None:
        """Open the app/URL and start MCP server.
        Web: start Chrome DevTools MCP, navigate to URL.
        Mobile: start mobile-mcp, launch app package.

        `extra_blocked_tools`: MCP tool names to hide from the LLM on top of
        the adapter's default block list. Wall 1.8 uses this to lock out
        navigate_page / go_back / go_forward during Execute runs.
        """
        ...

    async def close(self) -> None:
        """Close the MCP server connection."""
        ...

    # ── Observation ──────────────────────────────────────────

    async def snapshot(self) -> list[dict]:
        """Get all elements on screen/page as raw dicts.
        Web: accessibility tree from take_snapshot.
        Mobile: element JSON array from mobile_list_elements_on_screen.
        """
        ...

    async def screenshot(self) -> bytes | None:
        """Capture visual screenshot. Returns image bytes or None."""
        ...

    # ── Interaction ──────────────────────────────────────────

    async def click(self, element: L1Element) -> str:
        """Click/tap an element using its L1 locators (tries in order of confidence).
        Returns: 'OK' or 'ELEMENT_NOT_FOUND' or 'ERROR: ...'
        """
        ...

    async def fill(self, element: L1Element, value: str) -> str:
        """Fill a text field. Handles focus, typing, verification.
        Returns: 'FILLED: actual_value' or 'ERROR: ...'
        """
        ...

    async def select_option(self, element: L1Element, option: str) -> str:
        """Select a dropdown option. Opens dropdown, selects, verifies.
        Returns: 'SELECTED: option_text' or 'ERROR: ...'
        """
        ...

    async def dismiss_keyboard(self) -> None:
        """Dismiss on-screen keyboard. No-op on web."""
        ...

    async def scroll(self, direction: str = "down") -> None:
        """Scroll the viewport. direction: 'up', 'down', 'left', 'right'."""
        ...

    async def navigate_to_screen(self, screen_name: str) -> bool:
        """Navigate to a named screen/page.
        Web: navigate to URL.
        Mobile: tap nav tab.
        Returns True if navigation succeeded.
        """
        ...

    # ── Element Resolution ───────────────────────────────────

    async def find_element(self, element: L1Element) -> dict | None:
        """Find an element on screen using L1 locators.
        Tries locators in order of confidence (highest first).
        Returns raw element dict or None.
        """
        ...

    # ── Script Execution ─────────────────────────────────────

    async def evaluate_script(self, expression: str) -> str:
        """Execute JavaScript. Web: evaluate_script MCP tool.
        Mobile: returns 'NOT_SUPPORTED'.
        """
        ...

    # ── MCP Server Access ────────────────────────────────────

    def get_mcp_server(self):
        """Return the underlying MCP server (for agent tool registration)."""
        ...
