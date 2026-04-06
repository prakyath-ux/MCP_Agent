# mobile_version/compound_tools.py — Task-level tools for mobile testing
# These do ENTIRE test sequences in one LLM call.
# LLM says what to test, Python handles ALL execution.

import json
import asyncio
from agents import function_tool
from agents.mcp import MCPServerStdio

# Module-level reference to the MCP server — set by orchestrator before agent runs
_mcp_server: MCPServerStdio | None = None
_device_id: str = ""

# Cache: element positions with and without keyboard
_coords_no_keyboard: dict[str, tuple[int, int]] = {}
_coords_with_keyboard: dict[str, tuple[int, int]] = {}
_keyboard_active: bool = False


def set_server(server: MCPServerStdio, device_id: str) -> None:
    """Called by orchestrator to inject the MCP server reference."""
    global _mcp_server, _device_id
    _mcp_server = server
    _device_id = device_id


# ── Internal helpers (not exposed to LLM) ────────────────────────────────────

async def _call_mcp(tool_name: str, args: dict) -> str:
    if not _mcp_server:
        return "ERROR: MCP server not initialized"
    args["device"] = _device_id
    result = await _mcp_server.call_tool(tool_name, args)
    if result.content and len(result.content) > 0:
        return result.content[0].text
    return ""


def _parse_elements(raw: str) -> list[dict]:
    try:
        start = raw.index("[")
        return json.loads(raw[start:])
    except (ValueError, json.JSONDecodeError):
        return []


def _find_element(elements: list[dict], label: str) -> dict | None:
    label_lower = label.lower()
    # Exact match first
    for el in elements:
        if (el.get("label", "").lower() == label_lower or
            el.get("text", "").lower() == label_lower or
            el.get("identifier", "").lower() == label_lower):
            return el
    # Partial match fallback
    for el in elements:
        if (label_lower in el.get("label", "").lower() or
            label_lower in el.get("text", "").lower()):
            return el
    return None


def _center(el: dict) -> tuple[int, int]:
    coords = el.get("coordinates", {})
    cx = coords.get("x", 0) + coords.get("width", 0) // 2
    cy = coords.get("y", 0) + coords.get("height", 0) // 2
    return cx, cy


def _find_errors(elements: list[dict]) -> str:
    """Look for error/validation text in the element list."""
    error_keywords = ["error", "invalid", "required", "must", "cannot", "please enter"]
    for el in elements:
        text = (el.get("text", "") or "").lower()
        label = (el.get("label", "") or "").lower()
        for kw in error_keywords:
            if kw in text or kw in label:
                return el.get("text", "") or el.get("label", "")
    return "NO_ERROR"


async def _dismiss_keyboard():
    """Dismiss keyboard by tapping outside the input area. NEVER uses BACK (navigates away)."""
    global _keyboard_active
    if _keyboard_active:
        await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
        await asyncio.sleep(0.5)
        _keyboard_active = False


async def _scan_and_cache(state: str = "no_keyboard") -> list[dict]:
    """Scan screen and cache element positions."""
    global _coords_no_keyboard, _coords_with_keyboard, _keyboard_active
    raw = await _call_mcp("mobile_list_elements_on_screen", {})
    elements = _parse_elements(raw)

    cache = _coords_no_keyboard if state == "no_keyboard" else _coords_with_keyboard
    for el in elements:
        name = el.get("label", "") or el.get("text", "") or el.get("identifier", "")
        if name:
            cache[name.lower()] = _center(el)

    # Detect keyboard: if EditText is focused, keyboard is likely up
    _keyboard_active = any(el.get("focused") for el in elements)
    return elements


# ── Task-level tools (exposed to LLM) ────────────────────────────────────────

@function_tool
async def test_text_field(field_label: str, test_values: str) -> str:
    """Test a text field with multiple values in ONE call. Handles everything:
    find element, tap, type each value, check for errors, clear between tests.
    Returns pass/fail results for ALL test values.

    Args:
        field_label: The label of the text field (e.g. "Enter Details")
        test_values: Comma-separated test values (e.g. ",!@#$%,MEM123456,AAAAAAAAAA")
                     Use empty string for empty-field test.
    """
    values = [v.strip() for v in test_values.split(",")]
    results = []

    # Step 1: Scan to find the element (before keyboard)
    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, field_label)
    if not el:
        return json.dumps([{"value": v, "status": "SKIP", "error": f"ELEMENT_NOT_FOUND: '{field_label}'"} for v in values])

    cx, cy = _center(el)

    for value in values:
        # Tap the field
        await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
        await asyncio.sleep(0.3)

        # Rescan to get keyboard-adjusted coordinates
        elements = await _scan_and_cache("with_keyboard")
        el = _find_element(elements, field_label)
        if el:
            cx, cy = _center(el)  # Update coords for keyboard shift

        # Long-press to select all existing text, then type new value
        await _call_mcp("mobile_long_press_on_screen_at_coordinates", {"x": cx, "y": cy})
        await asyncio.sleep(0.2)

        if value:
            await _call_mcp("mobile_type_keys", {"text": value, "submit": False})
        else:
            # Empty value: just delete selected text
            await _call_mcp("mobile_press_button", {"button": "DEL"})

        await asyncio.sleep(0.3)

        # Check for errors
        elements = await _scan_and_cache("with_keyboard")
        error = _find_errors(elements)

        # Get actual field value
        el = _find_element(elements, field_label)
        actual = el.get("text", "unknown") if el else "unknown"

        results.append({
            "value": value if value else "(empty)",
            "actual": actual,
            "error": error,
            "status": "PASS" if (value and error == "NO_ERROR") else
                      "FAIL" if (not value and error == "NO_ERROR") else
                      "PASS" if (not value and error != "NO_ERROR") else "FAIL"
        })

    # Dismiss keyboard after all tests
    await _dismiss_keyboard()

    return json.dumps(results, indent=2)


@function_tool
async def test_dropdown(dropdown_label: str, select_option: str = "") -> str:
    """Test a dropdown by tapping it, listing options, optionally selecting one, then verifying.
    Returns: whether it opened, what options were shown, whether selection worked.

    Args:
        dropdown_label: The label of the dropdown (e.g. "Select Transaction Type")
        select_option: Option text to select (e.g. "Cash Deposit"). Leave empty to just verify dropdown opens.
    """
    # Dismiss keyboard first if active
    await _dismiss_keyboard()

    # Scan current state
    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, dropdown_label)
    if not el:
        return json.dumps({"status": "SKIP", "error": f"ELEMENT_NOT_FOUND: '{dropdown_label}'"})

    cx, cy = _center(el)
    labels_before = {e.get("text", "") or e.get("label", "") for e in elements}

    # Tap the dropdown
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
    await asyncio.sleep(0.5)

    # Scan after tap — see what appeared
    raw_after = await _call_mcp("mobile_list_elements_on_screen", {})
    elements_after = _parse_elements(raw_after)
    labels_after = {e.get("text", "") or e.get("label", "") for e in elements_after}

    new_elements = sorted(n for n in (labels_after - labels_before) if n)

    if not new_elements:
        return json.dumps({
            "status": "FAIL",
            "opened": False,
            "error": "No new elements appeared after tapping dropdown",
        })

    # If select_option is specified, find and tap it
    if select_option:
        option_el = _find_element(elements_after, select_option)
        if option_el:
            ox, oy = _center(option_el)
            await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": ox, "y": oy})
            await asyncio.sleep(0.5)

            # Dismiss keyboard if app re-focused a text field after selection
            await _dismiss_keyboard()
            await asyncio.sleep(0.3)

            # Verify selection
            raw_verify = await _call_mcp("mobile_list_elements_on_screen", {})
            elements_verify = _parse_elements(raw_verify)
            dropdown_after = _find_element(elements_verify, dropdown_label) or _find_element(elements_verify, select_option)
            dropdown_text = dropdown_after.get("text", "") or dropdown_after.get("label", "") if dropdown_after else "unknown"

            return json.dumps({
                "status": "PASS",
                "opened": True,
                "options": new_elements[:20],
                "count": len(new_elements),
                "selected": select_option,
                "verified": dropdown_text,
            })
        else:
            # Option not found — close by tapping outside (not BACK)
            await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
            await asyncio.sleep(0.3)
            return json.dumps({
                "status": "FAIL",
                "opened": True,
                "options": new_elements[:20],
                "count": len(new_elements),
                "selected": None,
                "error": f"Option '{select_option}' not found in dropdown options",
            })

    # No selection requested — just close by tapping outside (not BACK)
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
    await asyncio.sleep(0.3)

    # Dismiss keyboard if app re-focused a text field
    await _dismiss_keyboard()

    return json.dumps({
        "status": "PASS",
        "opened": True,
        "options": new_elements[:20],
        "count": len(new_elements),
    })


@function_tool
async def test_date_picker(picker_label: str) -> str:
    """Test a date picker by tapping it, checking if the picker UI appears, then closing it.

    Args:
        picker_label: The label of the date picker (e.g. "Date of Birth")
    """
    # Dismiss keyboard first
    await _dismiss_keyboard()

    # Scan current state
    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, picker_label)
    if not el:
        return json.dumps({"status": "SKIP", "error": f"ELEMENT_NOT_FOUND: '{picker_label}'"})

    cx, cy = _center(el)
    labels_before = {e.get("text", "") or e.get("label", "") for e in elements}

    # Tap the date picker
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
    await asyncio.sleep(0.5)

    # Check if picker opened
    raw_after = await _call_mcp("mobile_list_elements_on_screen", {})
    elements_after = _parse_elements(raw_after)
    labels_after = {e.get("text", "") or e.get("label", "") for e in elements_after}

    new_elements = sorted(n for n in (labels_after - labels_before) if n)

    # Look for picker indicators
    picker_signs = ["select date", "cancel", "confirm", "ok", "january", "february",
                    "2025", "2026", "2027", "sun", "mon", "tue"]
    has_picker = any(sign in " ".join(new_elements).lower() for sign in picker_signs)

    if not has_picker:
        await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
        await asyncio.sleep(0.3)
        await _dismiss_keyboard()
        return json.dumps({
            "status": "FAIL",
            "picker_opened": False,
            "new_elements": new_elements[:15],
        })

    # Picker is open — find and tap Confirm/OK to accept the default date
    confirm_el = None
    for label in ["Confirm", "confirm", "OK", "Ok", "ok", "Done", "done", "SET", "Set"]:
        confirm_el = _find_element(elements_after, label)
        if confirm_el:
            break

    if confirm_el:
        ccx, ccy = _center(confirm_el)
        await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": ccx, "y": ccy})
        await asyncio.sleep(0.5)

        # Dismiss keyboard if app re-focused text field after date confirm
        await _dismiss_keyboard()
        await asyncio.sleep(0.3)

        # Verify — rescan and check if date field updated
        # After confirming, the field label changes from "Date of Birth" to the date value
        import re
        raw_verify = await _call_mcp("mobile_list_elements_on_screen", {})
        elements_verify = _parse_elements(raw_verify)
        date_value = "unknown"

        # Try 1: Find by original label
        date_el = _find_element(elements_verify, picker_label)
        if date_el:
            date_value = date_el.get("text", "") or date_el.get("label", "")

        # Try 2: Find any element with a date value (YYYY-MM-DD pattern)
        if date_value in ("unknown", picker_label, ""):
            for el in elements_verify:
                for field in ["text", "label"]:
                    val = el.get(field, "")
                    if re.match(r"\d{4}-\d{2}-\d{2}", val):
                        date_value = val
                        break
                if date_value not in ("unknown", picker_label, ""):
                    break

        return json.dumps({
            "status": "PASS",
            "picker_opened": True,
            "confirmed": True,
            "date_value": date_value,
        })
    else:
        # No Confirm button found — close by tapping outside (not BACK)
        await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
        await asyncio.sleep(0.3)
        await _dismiss_keyboard()
        return json.dumps({
            "status": "PASS",
            "picker_opened": True,
            "confirmed": False,
            "error": "Picker opened but Confirm/OK button not found",
            "new_elements": new_elements[:15],
        })


@function_tool
async def verify_elements_exist(element_labels: str) -> str:
    """Check if multiple elements exist on screen. One scan for all checks.

    Args:
        element_labels: Comma-separated labels to check (e.g. "Find Member,Select Transaction Type")
    """
    labels = [l.strip() for l in element_labels.split(",")]

    # Single scan
    elements = await _scan_and_cache("no_keyboard")

    results = []
    for label in labels:
        el = _find_element(elements, label)
        if el:
            results.append({
                "label": label,
                "status": "EXISTS",
                "type": el.get("type", "").split(".")[-1],
                "text": el.get("text", ""),
            })
        else:
            results.append({
                "label": label,
                "status": "NOT_FOUND",
            })

    return json.dumps(results, indent=2)


@function_tool
async def scan_screen_summary(include_nav: str = "no") -> str:
    """Get a compact summary of all interactive elements on screen.
    Use this at the start to understand the screen layout.

    Args:
        include_nav: "yes" to include navigation tabs, "no" to skip them (default: "no")
    """
    elements = await _scan_and_cache("no_keyboard")

    # Filter to interactive elements only
    skip_types = {
        "android.widget.FrameLayout", "android.widget.LinearLayout",
        "android.view.View", "android.view.TextureView",
    }
    skip_ids = {"android:id/content", "android:id/statusBarBackground",
                "android:id/navigationBarBackground"}

    summary = []
    for el in elements:
        el_type = el.get("type", "")
        identifier = el.get("identifier", "")
        if el_type in skip_types and not el.get("label") and not el.get("text"):
            continue
        if identifier in skip_ids:
            continue

        label = el.get("label", "") or el.get("text", "") or identifier
        if not label:
            continue

        short_type = el_type.split(".")[-1]
        cx, cy = _center(el)
        focused = " [FOCUSED]" if el.get("focused") else ""
        summary.append(f"{short_type}: '{label}' at ({cx},{cy}){focused}")

    return "Screen elements:\n" + "\n".join(summary)


# ── Simple compound tools (for GPT-5 — fixed with all 4 features) ────────────

@function_tool
async def fill_field_and_verify(field_label: str, text: str) -> str:
    """Tap a text field, type text, and verify. Handles keyboard coordinate shifts.

    Args:
        field_label: The label of the field (e.g. "Enter Details")
        text: The text to type
    """
    # Scan before keyboard (get fresh coordinates)
    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, field_label)
    if not el:
        return f"ELEMENT_NOT_FOUND: '{field_label}'"
    cx, cy = _center(el)

    # Tap to focus (keyboard will appear)
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
    await asyncio.sleep(0.3)

    # Rescan with keyboard to get shifted coordinates
    elements = await _scan_and_cache("with_keyboard")

    if text:
        await _call_mcp("mobile_type_keys", {"text": text, "submit": False})
        await asyncio.sleep(0.3)

    # Verify
    elements = await _scan_and_cache("with_keyboard")
    el = _find_element(elements, field_label)
    actual = el.get("text", "unknown") if el else "unknown"
    error = _find_errors(elements)
    return f"FILLED: field='{field_label}', value='{actual}', error={error}"


@function_tool
async def clear_and_fill_simple(field_label: str, text: str) -> str:
    """Long-press to select all, then type new text. Handles keyboard shifts.

    Args:
        field_label: The label of the field
        text: The new text to type
    """
    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, field_label)
    if not el:
        return f"ELEMENT_NOT_FOUND: '{field_label}'"
    cx, cy = _center(el)

    # Long-press to select all
    await _call_mcp("mobile_long_press_on_screen_at_coordinates", {"x": cx, "y": cy})
    await asyncio.sleep(0.3)

    # Rescan with keyboard
    elements = await _scan_and_cache("with_keyboard")
    el = _find_element(elements, field_label)
    if el:
        cx, cy = _center(el)

    if text:
        await _call_mcp("mobile_type_keys", {"text": text, "submit": False})
    else:
        await _call_mcp("mobile_press_button", {"button": "DEL"})
    await asyncio.sleep(0.3)

    elements = await _scan_and_cache("with_keyboard")
    el = _find_element(elements, field_label)
    actual = el.get("text", "unknown") if el else "unknown"
    error = _find_errors(elements)
    return f"CLEARED_AND_FILLED: field='{field_label}', value='{actual}', error={error}"


@function_tool
async def tap_dropdown_and_select(dropdown_label: str, select_option: str = "") -> str:
    """Tap a dropdown, see options, optionally select one. Dismisses keyboard first.

    Args:
        dropdown_label: The label of the dropdown (e.g. "Select Transaction Type", "Cash Withdrawal")
        select_option: Option to select (e.g. "Cash Deposit"). Leave empty to just list options.
    """
    # Dismiss keyboard by tapping outside (NOT BACK which navigates away)
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
    await asyncio.sleep(0.3)

    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, dropdown_label)
    if not el:
        return f"ELEMENT_NOT_FOUND: '{dropdown_label}'"
    cx, cy = _center(el)
    labels_before = {e.get("text", "") or e.get("label", "") for e in elements}

    # Tap dropdown
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
    await asyncio.sleep(0.5)

    # Scan for options
    raw_after = await _call_mcp("mobile_list_elements_on_screen", {})
    elements_after = _parse_elements(raw_after)
    labels_after = {e.get("text", "") or e.get("label", "") for e in elements_after}
    new_elements = sorted(n for n in (labels_after - labels_before) if n)

    if not new_elements:
        return f"TAPPED: '{dropdown_label}'. No options appeared."

    # Select option if specified
    if select_option:
        option_el = _find_element(elements_after, select_option)
        if option_el:
            ox, oy = _center(option_el)
            await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": ox, "y": oy})
            await asyncio.sleep(0.3)
            raw_verify = await _call_mcp("mobile_list_elements_on_screen", {})
            elements_verify = _parse_elements(raw_verify)
            updated = _find_element(elements_verify, dropdown_label) or _find_element(elements_verify, select_option)
            updated_text = updated.get("text", "") or updated.get("label", "") if updated else "unknown"
            return f"SELECTED: '{select_option}' from '{dropdown_label}'. Field now shows: '{updated_text}'. Options were: {', '.join(new_elements[:10])}"
        else:
            await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
            return f"TAPPED: '{dropdown_label}'. Options: {', '.join(new_elements[:10])}. '{select_option}' NOT FOUND in options."

    # Just report options, close by tapping outside
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
    await asyncio.sleep(0.3)
    return f"TAPPED: '{dropdown_label}'. Options: {', '.join(new_elements[:10])}"


@function_tool
async def tap_date_and_confirm(picker_label: str) -> str:
    """Open date picker, accept the default date by tapping Confirm. Dismisses keyboard first.

    Args:
        picker_label: The label of the date field (e.g. "Date of Birth")
    """
    # Dismiss keyboard by tapping outside (NOT BACK)
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
    await asyncio.sleep(0.3)

    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, picker_label)
    if not el:
        return f"ELEMENT_NOT_FOUND: '{picker_label}'"
    cx, cy = _center(el)

    # Tap to open picker
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": cx, "y": cy})
    await asyncio.sleep(0.5)

    # Check if picker opened
    raw_after = await _call_mcp("mobile_list_elements_on_screen", {})
    elements_after = _parse_elements(raw_after)

    picker_signs = ["select date", "cancel", "confirm", "ok", "january", "february",
                    "2025", "2026", "2027", "sun", "mon", "tue"]
    all_text = " ".join((e.get("text", "") + " " + e.get("label", "")).lower() for e in elements_after)
    has_picker = any(sign in all_text for sign in picker_signs)

    if not has_picker:
        return f"TAPPED: '{picker_label}'. Picker did NOT open."

    # Find and tap Confirm/OK
    confirm_el = None
    for label in ["Confirm", "confirm", "OK", "Ok", "ok", "Done", "done", "SET", "Set"]:
        confirm_el = _find_element(elements_after, label)
        if confirm_el:
            break

    if confirm_el:
        ccx, ccy = _center(confirm_el)
        await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": ccx, "y": ccy})
        await asyncio.sleep(0.3)

        # Verify date field updated
        raw_verify = await _call_mcp("mobile_list_elements_on_screen", {})
        elements_verify = _parse_elements(raw_verify)
        date_el = _find_element(elements_verify, picker_label)
        date_value = date_el.get("text", "") or date_el.get("label", "") if date_el else "unknown"
        return f"DATE_CONFIRMED: '{picker_label}' = '{date_value}'"
    else:
        # Close with tap outside
        await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
        return f"PICKER_OPENED but Confirm button not found. Closed by tapping outside."


@function_tool
async def verify_element_exists_simple(element_label: str) -> str:
    """Check if an element exists on screen.

    Args:
        element_label: The label to search for
    """
    elements = await _scan_and_cache("no_keyboard")
    el = _find_element(elements, element_label)
    if el:
        return f"EXISTS: '{element_label}' type={el.get('type','').split('.')[-1]}"
    return f"NOT_FOUND: '{element_label}'"


@function_tool
async def dismiss_keyboard_safe(reason: str = "done typing") -> str:
    """Dismiss keyboard by tapping outside the input area. NEVER uses BACK button.

    Args:
        reason: Why dismissing (for logging)
    """
    # Tap header area — safe, won't navigate away
    await _call_mcp("mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100})
    await asyncio.sleep(0.3)
    elements = await _scan_and_cache("no_keyboard")
    labels = [e.get("label", "") or e.get("text", "") for e in elements if e.get("label") or e.get("text")]
    return f"KEYBOARD_DISMISSED: {', '.join(labels[:8])}"


# ── Tool lists ───────────────────────────────────────────────────────────────

# Task-level tools (for Groq / gpt-oss-120b — batches multiple tests per call)
TASK_TOOLS = [
    test_text_field,
    test_dropdown,
    test_date_picker,
    verify_elements_exist,
    scan_screen_summary,
]

# Simple compound tools (for GPT-5 — one action per call, all 4 features fixed)
SIMPLE_TOOLS = [
    fill_field_and_verify,
    clear_and_fill_simple,
    tap_dropdown_and_select,
    tap_date_and_confirm,
    verify_element_exists_simple,
    dismiss_keyboard_safe,
]

# Exploration tools (for Pass 1 — fill once, explore, collect knowledge)
EXPLORE_TOOLS = [
    scan_screen_summary,
    fill_field_and_verify,
    test_dropdown,
    test_date_picker,
    verify_elements_exist,
]

# Default — orchestrator picks based on provider
COMPOUND_TOOLS = TASK_TOOLS
