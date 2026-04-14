# qa/adapters/snapshot_filter.py — Trim Chrome DevTools accessibility snapshots
# before the LLM sees them. Collapses long dropdown option lists and removes
# decorative / structural noise.

MAX_DROPDOWN_OPTIONS = 5
REMOVE_DECORATIVE = True

DECORATIVE_ROLES = {"image", "separator", "presentation", "none"}
STRUCTURAL_ROLES = {"generic", "group"}
INTERACTIVE_ROLES = {
    "textbox", "button", "link", "checkbox", "radio",
    "combobox", "listbox", "option", "menuitem",
    "tab", "switch", "slider", "spinbutton",
}
CONTENT_ROLES = {
    "heading", "alert", "status", "progressbar",
    "dialog", "alertdialog", "banner",
}


def filter_snapshot(raw_snapshot: str) -> str:
    if not raw_snapshot:
        return raw_snapshot

    lines = raw_snapshot.split("\n")
    out: list[str] = []
    in_option_block = False
    option_count = 0
    option_kept = 0
    first_option_indent = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if _is_option_line(stripped):
            if not in_option_block:
                in_option_block = True
                option_count = 0
                option_kept = 0
                first_option_indent = _get_indent(line)
            option_count += 1
            if option_kept < MAX_DROPDOWN_OPTIONS:
                out.append(line)
                option_kept += 1
            continue
        else:
            if in_option_block and option_count > MAX_DROPDOWN_OPTIONS:
                collapsed = option_count - option_kept
                out.append(
                    f"{first_option_indent}... ({collapsed} more options, {option_count} total)"
                )
            in_option_block = False
            option_count = 0

        if REMOVE_DECORATIVE and _has_role(stripped, DECORATIVE_ROLES):
            continue

        if REMOVE_DECORATIVE and _has_role(stripped, STRUCTURAL_ROLES):
            if not _has_useful_text(stripped):
                continue

        out.append(line)

    if in_option_block and option_count > MAX_DROPDOWN_OPTIONS:
        collapsed = option_count - option_kept
        out.append(
            f"{first_option_indent}... ({collapsed} more options, {option_count} total)"
        )

    return "\n".join(out)


def _is_option_line(stripped: str) -> bool:
    return (
        stripped.startswith("option ")
        or stripped.startswith("menuitem ")
        or stripped.startswith("listitem ")
        or 'role="option"' in stripped
    )


def _has_role(stripped: str, roles: set[str]) -> bool:
    first_word = stripped.split(" ")[0].split('"')[0].lower()
    return first_word in roles


def _has_useful_text(stripped: str) -> bool:
    useful_patterns = [
        "required", "invalid", "error", "warning",
        "step ", "progress", "section",
    ]
    lower = stripped.lower()
    return any(p in lower for p in useful_patterns)


def _get_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]
