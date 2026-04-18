# qa/knowledge/element_id.py — Deterministic element ID generation

import re


def make_element_id(
    screen_name: str,
    element_label: str,
    element_type: str,
    section: str = "",
) -> str:
    """Generate a stable, deterministic element ID.

    Formats:
        • 3-part: {screen}:{label}:{type}            (no section)
        • 4-part: {screen}:{section}:{label}:{type}  (with section)

    - Lowercased
    - Non-alphanumeric characters replaced with underscores
    - Leading/trailing underscores stripped

    Section is optional. Use it when a screen contains repeated sub-sections
    that reuse field names (e.g. TECU page 4's Beneficiary 1 / Beneficiary 2
    both having "First Name").

    Examples:
        >>> make_element_id("iTeller", "Select Transaction Type", "dropdown")
        'iteller:select_transaction_type:dropdown'
        >>> make_element_id("Contact Info", "First Name", "text_input")
        'contact_info:first_name:text_input'
        >>> make_element_id("Other Products", "First Name", "text_input",
        ...                 section="Beneficiary 1 Details")
        'other_products:beneficiary_1_details:first_name:text_input'
    """
    def _normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")

    screen = _normalize(screen_name)
    label = _normalize(element_label)
    etype = _normalize(element_type)
    if section:
        return f"{screen}:{_normalize(section)}:{label}:{etype}"
    return f"{screen}:{label}:{etype}"


def parse_element_id(element_id: str) -> tuple[str, str, str]:
    """Parse an element_id back into (screen_name, element_label, element_type).

    Handles both 3-part and 4-part IDs. For 4-part IDs (with a section
    segment), the section is dropped here — callers that need it should
    use parse_element_id_full() instead.

    >>> parse_element_id("iteller:select_transaction_type:dropdown")
    ('iteller', 'select_transaction_type', 'dropdown')
    >>> parse_element_id("other_products:beneficiary_1_details:first_name:text_input")
    ('other_products', 'first_name', 'text_input')
    """
    parts = element_id.split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 4:
        # Drop the section segment (index 1); return (screen, label, type)
        return parts[0], parts[2], parts[3]
    raise ValueError(
        f"Invalid element_id format: '{element_id}' "
        f"(expected 3 or 4 colon-separated parts, got {len(parts)})"
    )


def parse_element_id_full(element_id: str) -> tuple[str, str, str, str]:
    """Parse an element_id into all four components, including section.

    Returns (screen, section, label, type). For 3-part IDs (no section),
    `section` is an empty string.

    >>> parse_element_id_full("iteller:select_transaction_type:dropdown")
    ('iteller', '', 'select_transaction_type', 'dropdown')
    >>> parse_element_id_full("other_products:beneficiary_1_details:first_name:text_input")
    ('other_products', 'beneficiary_1_details', 'first_name', 'text_input')
    """
    parts = element_id.split(":")
    if len(parts) == 3:
        return parts[0], "", parts[1], parts[2]
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    raise ValueError(
        f"Invalid element_id format: '{element_id}' "
        f"(expected 3 or 4 colon-separated parts, got {len(parts)})"
    )
