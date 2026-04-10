# qa/tools/mobile_tools.py — Task-level tools for mobile testing
# These wrap the existing compound_tools pattern for use with the new pipeline architecture.
# The tools are registered with the Agent so the LLM can call them.

import sys
from pathlib import Path

# Import the existing compound tools — they work, no need to rewrite
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mobile_version"))

from compound_tools import (
    # Task tools (for test execution)
    test_text_field,
    test_dropdown,
    test_date_picker,
    verify_elements_exist,
    scan_screen_summary,
    # Explore tools (for discovery)
    fill_field_and_verify,
    # Server injection
    set_server,
    clear_caches,
)


def get_task_tools(mcp_server, device_id: str) -> list:
    """Get task-level tools for test execution (Pipeline 3).
    Injects MCP server reference and clears caches."""
    clear_caches()
    set_server(mcp_server, device_id)
    return [
        test_text_field,
        test_dropdown,
        test_date_picker,
        verify_elements_exist,
        scan_screen_summary,
    ]


def get_explore_tools(mcp_server, device_id: str) -> list:
    """Get exploration tools for discovery (Pipeline 1).
    Uses fill_field_and_verify instead of test_text_field (fill once, not multi-value)."""
    clear_caches()
    set_server(mcp_server, device_id)
    return [
        scan_screen_summary,
        fill_field_and_verify,
        test_dropdown,
        test_date_picker,
        verify_elements_exist,
    ]
