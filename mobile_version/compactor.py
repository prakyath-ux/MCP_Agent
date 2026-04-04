# version_2/compactor.py — History compaction: trim old turns, build rolling summary

import json

from config import KEEP_LAST_N_TURNS

def compact_history(
    input_items: list[dict],
    rolling_summary: str,
    keep_last_n: int = KEEP_LAST_N_TURNS,
) -> list[dict]:
    """Replace old conversation turns with a rolling summary

     Keeps:
      - The first item (original user task message)
      - The rolling summary (injected as a user message)
      - The last N turns (so the agent has immediate context)

    Removes:
      - Everything in between (old thoughts, old tool calls, old snapshots)

    Also runs snapshot_filter on any kept tool results.
    """

    if len(input_items) <= keep_last_n * 2 + 2:
        # Not enough history to compact - jsut filter snapshots in place
        return _filter_snapshots_in_items(input_items)

        # 1. Extract the first user message (the original task)
    first_item = input_items[0]

    # 2. Build the summary injection message
    summary_item = {
        "role": "user",
        "content": f"CONTEXT (do not repeat this, just use it): {rolling_summary}",
    }

    # 3. Keep the last N turns worth of items
    #    Each "turn" is roughly: assistant message + tool_call + tool_result
    #    We keep the tail of the list
    recent_items = _get_last_n_turns(input_items[1:], keep_last_n)

    # 4. Filter snapshots in the kept items
    recent_items = _filter_snapshots_in_items(recent_items)

    # 5. Assemble: [task] + [summary] + [recent turns]
    return [first_item, summary_item] + recent_items


def update_summary(
    current_summary: str,
    turn_items: list[dict],
) -> str:
    """Update the rolling summary based on what happened in the latest turn.

    Extracts tool names and key results from the turn's items and appends
    a short description to the existing summary.
    """
    tool_name = ""
    tool_target = ""
    tool_uid = ""
    tool_result_snippet = ""

    for item in turn_items:
        # Extract tool call info
        item_type = item.get("type", "")

        if item_type == "function_call":
            tool_name = item.get("name", "")
            args_raw = item.get("arguments", "{}")
            try:
                args = json.loads(args_raw)
                tool_uid = str(args.get("uid", ""))[:20]
                tool_target = (
                    args.get("value")
                    or args.get("uid")
                    or args.get("url", "")
                )
                tool_target = str(tool_target)[:50]
            except (json.JSONDecodeError, TypeError):
                pass

        # Extract tool result snippet
        if item_type == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, str):
                tool_result_snippet = output[:200]

    # Build a short description of what happened
    if not tool_name:
        return current_summary

    failed = _is_failure(tool_result_snippet)
    action_desc = _describe_action(tool_name, tool_target, tool_uid, tool_result_snippet, failed)
    if not action_desc:
        return current_summary

    # Append to summary (keep it concise)
    if current_summary:
        return f"{current_summary} {action_desc}"
    return action_desc


def _is_failure(result: str) -> bool:
    """Detect if a tool result indicates failure."""
    if not result:
        return False
    lower = result.lower()
    failure_signals = [
        "error", "fail", "not found", "unable", "cannot", "could not",
        "timeout", "timed out", "exception", "no such element",
        "not visible", "not interactable", "blocked", "overlay",
    ]
    return any(signal in lower for signal in failure_signals)


def _describe_action(tool_name: str, target: str, uid: str, result: str, failed: bool) -> str:
    """Turn a tool call into a short human-readable description.

    Includes failure context so the agent remembers what didn't work.
    """
    fail_suffix = ""
    if failed:
        # Extract a short reason from the result
        reason = result[:80].strip()
        fail_suffix = f" — FAILED: {reason}"

    match tool_name:
        # ── Web tools ──
        case "navigate_page":
            return f"Navigated to {target}."
        case "take_snapshot":
            return ""
        case "take_screenshot":
            return ""
        case "fill":
            if failed:
                return f"fill({uid}, '{target}'){fail_suffix}."
            return f"Filled '{target}'."
        case "click":
            if failed:
                return f"click({uid}){fail_suffix}."
            if target:
                return f"Clicked {target}."
            return "Clicked element."
        case "select_option":
            if failed:
                return f"select_option({uid}, '{target}'){fail_suffix}."
            return f"Selected '{target}'."
        case "upload_file":
            if failed:
                return f"upload_file({uid}){fail_suffix}."
            return f"Uploaded file to {target}."
        case "evaluate_script":
            return ""
        case "check" | "uncheck":
            if failed:
                return f"{tool_name}({uid}){fail_suffix}."
            return f"Toggled checkbox {target}."
        case "type_text" | "press_key":
            if failed:
                return f"{tool_name}({uid}){fail_suffix}."
            return f"Typed into {target}."
        case "list_network_requests" | "list_console_messages":
            return ""
        # ── Mobile tools ──
        case "mobile_click_on_screen_at_coordinates":
            if failed:
                return f"Tapped ({target}){fail_suffix}."
            return f"Tapped ({target})."
        case "mobile_type_keys":
            if failed:
                return f"Typed '{target}'{fail_suffix}."
            return f"Typed '{target}'."
        case "mobile_swipe_on_screen":
            return f"Swiped {target}."
        case "mobile_press_button":
            return f"Pressed {target}."
        case "mobile_launch_app":
            return f"Launched {target}."
        case "mobile_terminate_app":
            return f"Terminated {target}."
        case "mobile_open_url":
            return f"Opened URL {target}."
        case "mobile_list_elements_on_screen":
            return ""  # Observation, not action
        case "mobile_take_screenshot" | "mobile_save_screenshot":
            return ""
        case "mobile_list_available_devices" | "mobile_get_screen_size" | "mobile_get_orientation":
            return ""
        case "mobile_double_tap_on_screen":
            return f"Double-tapped ({target})."
        case "mobile_long_press_on_screen_at_coordinates":
            return f"Long-pressed ({target})."
        case _:
            return ""


def _get_last_n_turns(items: list[dict], n: int) -> list[dict]:
    """Extract the last N logical turns from the item list.

    A 'turn boundary' is an assistant message (role=assistant or type=message).
    We count backwards to find where the last N turns start.

    GPT-5 produces 'reasoning' items that MUST stay paired with their
    following 'message' item. We treat reasoning as part of the turn,
    and back up the cut point to include any preceding reasoning item.
    """
    if n <= 0:
        return items

    # Find turn boundaries (assistant messages mark the start of each turn)
    # Reasoning items are NOT boundaries — they belong to the next message
    boundaries: list[int] = []
    for i, item in enumerate(items):
        role = item.get("role", "")
        item_type = item.get("type", "")
        if role == "assistant" or item_type == "message":
            boundaries.append(i)

    if len(boundaries) <= n:
        return items  # Not enough turns to trim

    # Keep from the Nth-from-last boundary onwards
    cut_point = boundaries[-n]

    # Back up to include any reasoning item that precedes the cut point
    while cut_point > 0 and items[cut_point - 1].get("type") == "reasoning":
        cut_point -= 1

    return items[cut_point:]


def _filter_snapshots_in_items(items: list[dict]) -> list[dict]:
    """Compress mobile element lists in history to reduce token bloat.

    Replaces full element JSON (~1,500 tokens) with a short summary (~200 tokens).
    Only interactive elements are kept (EditText, ViewGroup with labels, buttons).
    Nav tabs, status bar, and decorative elements are stripped.
    """
    filtered = []
    for item in items:
        item_type = item.get("type", "")

        if item_type == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, str) and "Found these elements on screen" in output:
                item = dict(item)  # Don't mutate original
                item["output"] = _compress_element_list(output)

        filtered.append(item)
    return filtered


def _compress_element_list(raw_output: str) -> str:
    """Convert full element JSON to a short summary of interactive elements only."""
    try:
        # Extract JSON array from the output
        json_start = raw_output.index("[")
        elements = json.loads(raw_output[json_start:])
    except (ValueError, json.JSONDecodeError):
        return raw_output[:300]  # Fallback: truncate

    # Filter to interactive elements only
    skip_types = {
        "android.widget.FrameLayout",
        "android.widget.LinearLayout",
        "android.view.View",
        "android.view.TextureView",
    }
    skip_identifiers = {
        "android:id/content",
        "android:id/statusBarBackground",
        "android:id/navigationBarBackground",
    }
    skip_labels = {"DASHBOARD", "iTELLER", "iBRANCH", "LOAN", "MORE"}

    summary_lines = ["Elements on screen:"]
    for el in elements:
        el_type = el.get("type", "")
        identifier = el.get("identifier", "")
        label = el.get("label", "")
        text = el.get("text", "")
        coords = el.get("coordinates", {})

        # Skip non-interactive and nav elements
        if el_type in skip_types and not label and not text:
            continue
        if identifier in skip_identifiers:
            continue
        if label in skip_labels or text in skip_labels:
            continue
        if not label and not text and not identifier:
            continue

        # Build compact description
        cx = coords.get("x", 0) + coords.get("width", 0) // 2
        cy = coords.get("y", 0) + coords.get("height", 0) // 2
        name = label or text or identifier
        short_type = el_type.split(".")[-1]  # "android.widget.EditText" → "EditText"
        focused = " [FOCUSED]" if el.get("focused") else ""
        summary_lines.append(f"  {short_type}: \"{name}\" at ({cx},{cy}){focused}")

    return "\n".join(summary_lines)


if __name__ == "__main__":
    # Quick self-test
    summary = ""
    summary = update_summary(summary, [
        {"type": "function_call", "name": "navigate_page", "arguments": '{"url": "https://example.com"}'},
        {"type": "function_call_output", "output": "Navigated to https://example.com"},
    ])
    print(f"After navigate: '{summary}'")

    summary = update_summary(summary, [
        {"type": "function_call", "name": "fill", "arguments": '{"uid": "1_93", "value": "Roman"}'},
        {"type": "function_call_output", "output": "Filled value Roman"},
    ])
    print(f"After fill (success): '{summary}'")

    summary = update_summary(summary, [
        {"type": "function_call", "name": "click", "arguments": '{"uid": "1_33"}'},
        {"type": "function_call_output", "output": "Error: element not interactable, overlay blocking click"},
    ])
    print(f"After click (FAIL): '{summary}'")

    summary = update_summary(summary, [
        {"type": "function_call", "name": "upload_file", "arguments": '{"uid": "1_79", "paths": ["pp.png"]}'},
        {"type": "function_call_output", "output": "File uploaded successfully"},
    ])
    print(f"After upload (success): '{summary}'")

    print(f"\nFinal summary ({len(summary)} chars): {summary}")
