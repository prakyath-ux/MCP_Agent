# version_2/snapshot_filter.py — ACI layer: trim snapshots before agent sees them
import re

from config import MAX_DROPDOWN_OPTIONS, REMOVE_DECORATIVE


# Elements that carry no useful information for QA testing
DECORATIVE_ROLES = {
    "image",           # logos, icons, avatars
    "separator",       # horizontal rules
    "presentation",    # aria decorative
    "none",            # aria hidden
}

# Structural wrappers that just add nesting noise
STRUCTURAL_ROLES = {
    "generic",         # <div>, <span> with no semantic meaning
    "group",           # generic grouping
}

# Elements we always keep — these are what the agent interacts with
INTERACTIVE_ROLES = {
    "textbox", "button", "link", "checkbox", "radio",
    "combobox", "listbox", "option", "menuitem",
    "tab", "switch", "slider", "spinbutton",
}

# Content roles we keep — these give the agent context
CONTENT_ROLES = {
    "heading", "alert", "status", "progressbar",
    "dialog", "alertdialog", "banner",
}


def filter_snapshot(raw_snapshot: str) -> str:
    """Trim a raw accessibility snapshot to only useful elements.
    
    Removes decorative images, structural wrappers, and collapses
    long dropdown option lists to a count + current selection.
    """
    if not raw_snapshot:
        return raw_snapshot

    lines = raw_snapshot.split("\n")
    filtered_lines: list[str] = []
    in_option_block = False
    option_count = 0
    option_kept = 0
    first_option_indent = ""

    for line in lines:
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            continue

        # Detect option blocks (dropdown lists) — collapse after MAX_DROPDOWN_OPTIONS
        if _is_option_line(stripped):
            if not in_option_block:
                in_option_block = True
                option_count = 0
                option_kept = 0
                first_option_indent = _get_indent(line)

            option_count += 1

            # Keep first few options so agent sees the pattern
            if option_kept < MAX_DROPDOWN_OPTIONS:
                filtered_lines.append(line)
                option_kept += 1
            continue
        else:
            # End of option block — add summary if we collapsed any
            if in_option_block and option_count > MAX_DROPDOWN_OPTIONS:
                collapsed = option_count - option_kept
                filtered_lines.append(
                    f"{first_option_indent}... ({collapsed} more options, {option_count} total)"
                )
            in_option_block = False
            option_count = 0

        # Remove decorative elements
        if REMOVE_DECORATIVE and _has_role(stripped, DECORATIVE_ROLES):
            continue

        # Remove structural wrappers (keep their children, just skip the wrapper line)
        if REMOVE_DECORATIVE and _has_role(stripped, STRUCTURAL_ROLES):
            # Only skip if the line has no useful text content
            if not _has_useful_text(stripped):
                continue

        # Keep everything else
        filtered_lines.append(line)

    # Handle case where snapshot ends mid-option-block
    if in_option_block and option_count > MAX_DROPDOWN_OPTIONS:
        collapsed = option_count - option_kept
        filtered_lines.append(
            f"{first_option_indent}... ({collapsed} more options, {option_count} total)"
        )

    return "\n".join(filtered_lines)


def _is_option_line(stripped: str) -> bool:
    """Check if a line represents a dropdown option."""
    return (
        stripped.startswith("option ")
        or stripped.startswith("menuitem ")
        or stripped.startswith("listitem ")
        or 'role="option"' in stripped
    )


def _has_role(stripped: str, roles: set[str]) -> bool:
    """Check if a line starts with or contains one of the given roles."""
    first_word = stripped.split(" ")[0].split('"')[0].lower()
    return first_word in roles


def _has_useful_text(stripped: str) -> bool:
    """Check if a structural element contains text worth keeping."""
    # If it has a label, name, or meaningful text content, keep it
    useful_patterns = [
        "required", "invalid", "error", "warning",
        "step ", "progress", "section",
    ]
    lower = stripped.lower()
    return any(p in lower for p in useful_patterns)


def _get_indent(line: str) -> str:
    """Extract leading whitespace from a line."""
    return line[: len(line) - len(line.lstrip())]


if __name__ == "__main__":
    # Quick test with a mock snapshot
    test_snapshot = """heading "New Member Application"
image "logo.png"
generic "wrapper"
  generic "form-section"
    textbox "First Name" uid=1_93 required
    textbox "Last Name" uid=1_102 required
    combobox "Country Code" uid=1_121
      option "Afghanistan (+93)"
      option "Albania (+355)"
      option "Algeria (+213)"
      option "Andorra (+376)"
      option "Angola (+244)"
      option "Argentina (+54)"
      option "Armenia (+374)"
      option "Australia (+61)"
      option "Austria (+43)"
      option "Trinidad (+1868)"
    button "Save & Continue" uid=1_30
generic "footer"
  link "Privacy Policy"
  image "footer-logo.svg"
  separator"""

    result = filter_snapshot(test_snapshot)
    print("=== FILTERED ===")
    print(result)
    print(f"\nOriginal: {len(test_snapshot)} chars")
    print(f"Filtered: {len(result)} chars")
    print(f"Reduction: {(1 - len(result)/len(test_snapshot))*100:.0f}%")
