# qa/engine/compactor.py — History compaction (merged from mobile + web compactors)

import json


def compact_history(
    input_items: list[dict],
    rolling_summary: str,
    keep_last_n: int = 2,
) -> list[dict]:
    """Replace old conversation turns with a rolling summary.

    Keeps:
      - The first item (original user task message)
      - The rolling summary (injected as a user message)
      - The last N turns (so the agent has immediate context)
    """
    if len(input_items) <= keep_last_n * 2 + 2:
        return _filter_snapshots_in_items(input_items)

    first_item = input_items[0]

    summary_item = {
        "role": "user",
        "content": f"CONTEXT (do not repeat this, just use it): {rolling_summary}",
    }

    recent_items = _get_last_n_turns(input_items[1:], keep_last_n)
    recent_items = _filter_snapshots_in_items(recent_items)

    return [first_item, summary_item] + recent_items


def update_summary(current_summary: str, turn_items: list[dict]) -> str:
    """Update rolling summary based on latest turn's tool calls."""
    tool_name = ""
    tool_target = ""
    tool_uid = ""
    tool_result_snippet = ""

    for item in turn_items:
        item_type = item.get("type", "")

        if item_type == "function_call":
            tool_name = item.get("name", "")
            args_raw = item.get("arguments", "{}")
            try:
                args = json.loads(args_raw)
                tool_uid = str(args.get("uid", ""))[:20]
                tool_target = str(
                    args.get("value") or args.get("uid") or args.get("url", "")
                    or args.get("text", "") or args.get("packageName", "")
                )[:50]
            except (json.JSONDecodeError, TypeError):
                pass

        if item_type == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, str):
                tool_result_snippet = output[:200]

    if not tool_name:
        return current_summary

    failed = _is_failure(tool_result_snippet)
    action_desc = _describe_action(tool_name, tool_target, tool_uid, tool_result_snippet, failed)
    if not action_desc:
        return current_summary

    if current_summary:
        return f"{current_summary} {action_desc}"
    return action_desc


def _is_failure(result: str) -> bool:
    if not result:
        return False
    lower = result.lower()
    signals = ["error", "fail", "not found", "unable", "cannot", "timeout", "exception"]
    return any(s in lower for s in signals)


def _describe_action(tool_name: str, target: str, uid: str, result: str, failed: bool) -> str:
    fail_suffix = f" — FAILED: {result[:80].strip()}" if failed else ""

    match tool_name:
        # Web
        case "navigate_page": return f"Navigated to {target}."
        case "take_snapshot" | "take_screenshot": return ""
        case "fill":
            return f"fill({uid}, '{target}'){fail_suffix}." if failed else f"Filled '{target}'."
        case "click":
            return f"click({uid}){fail_suffix}." if failed else f"Clicked {target or 'element'}."
        case "select_option":
            return f"select_option({uid}, '{target}'){fail_suffix}." if failed else f"Selected '{target}'."
        case "upload_file":
            return f"upload_file({uid}){fail_suffix}." if failed else f"Uploaded to {target}."
        case "evaluate_script": return ""
        # Mobile
        case "mobile_click_on_screen_at_coordinates":
            return f"Tapped ({target}){fail_suffix}."
        case "mobile_type_keys":
            return f"Typed '{target}'{fail_suffix}."
        case "mobile_swipe_on_screen": return f"Swiped {target}."
        case "mobile_press_button": return f"Pressed {target}."
        case "mobile_launch_app": return f"Launched {target}."
        case "mobile_list_elements_on_screen": return ""
        case "mobile_take_screenshot" | "mobile_save_screenshot": return ""
        # Compound tools
        case "test_text_field": return f"Tested text field{fail_suffix}."
        case "test_dropdown": return f"Tested dropdown{fail_suffix}."
        case "test_date_picker": return f"Tested date picker{fail_suffix}."
        case "verify_elements_exist": return "Verified elements."
        case "scan_screen_summary": return ""
        case _: return ""


def _get_last_n_turns(items: list[dict], n: int) -> list[dict]:
    if n <= 0:
        return items

    boundaries: list[int] = []
    for i, item in enumerate(items):
        role = item.get("role", "")
        item_type = item.get("type", "")
        if role == "assistant" or item_type == "message":
            boundaries.append(i)

    if len(boundaries) <= n:
        return items

    cut_point = boundaries[-n]

    # Include preceding reasoning items
    while cut_point > 0 and items[cut_point - 1].get("type") == "reasoning":
        cut_point -= 1

    return items[cut_point:]


def _filter_snapshots_in_items(items: list[dict]) -> list[dict]:
    """Compress element lists in history to reduce token bloat."""
    filtered = []
    for item in items:
        if item.get("type") == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, str) and "Found these elements on screen" in output:
                item = dict(item)
                item["output"] = _compress_element_list(output)
        filtered.append(item)
    return filtered


def _compress_element_list(raw_output: str) -> str:
    try:
        json_start = raw_output.index("[")
        elements = json.loads(raw_output[json_start:])
    except (ValueError, json.JSONDecodeError):
        return raw_output[:300]

    skip_types = {
        "android.widget.FrameLayout", "android.widget.LinearLayout",
        "android.view.View", "android.view.TextureView",
    }
    skip_ids = {"android:id/content", "android:id/statusBarBackground", "android:id/navigationBarBackground"}
    skip_labels = {"DASHBOARD", "iTELLER", "iBRANCH", "LOAN", "MORE"}

    lines = ["Elements on screen:"]
    for el in elements:
        el_type = el.get("type", "")
        identifier = el.get("identifier", "")
        label = el.get("label", "")
        text = el.get("text", "")
        coords = el.get("coordinates", {})

        if el_type in skip_types and not label and not text:
            continue
        if identifier in skip_ids:
            continue
        if label in skip_labels or text in skip_labels:
            continue
        if not label and not text and not identifier:
            continue

        cx = coords.get("x", 0) + coords.get("width", 0) // 2
        cy = coords.get("y", 0) + coords.get("height", 0) // 2
        name = label or text or identifier
        short_type = el_type.split(".")[-1]
        focused = " [FOCUSED]" if el.get("focused") else ""
        lines.append(f"  {short_type}: \"{name}\" at ({cx},{cy}){focused}")

    return "\n".join(lines)
