# qa/knowledge/element_id.py — Deterministic element ID generation

import re


def make_element_id(screen_name: str, element_label: str, element_type: str) -> str:
    """Generate a stable, deterministic element ID.

    Format: {screen_name}:{element_label}:{element_type}
    - Lowercased
    - Non-alphanumeric characters replaced with underscores
    - Leading/trailing underscores stripped

    Examples:
        >>> make_element_id("iTeller", "Select Transaction Type", "dropdown")
        'iteller:select_transaction_type:dropdown'
        >>> make_element_id("Contact Info", "First Name", "text_input")
        'contact_info:first_name:text_input'
        >>> make_element_id("MORE", "Date of Birth", "date_picker")
        'more:date_of_birth:date_picker'
    """
    def _normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")

    return f"{_normalize(screen_name)}:{_normalize(element_label)}:{_normalize(element_type)}"


def parse_element_id(element_id: str) -> tuple[str, str, str]:
    """Parse an element_id back into (screen_name, element_label, element_type).

    >>> parse_element_id("iteller:select_transaction_type:dropdown")
    ('iteller', 'select_transaction_type', 'dropdown')
    """
    parts = element_id.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid element_id format: '{element_id}' (expected screen:label:type)")
    return parts[0], parts[1], parts[2]
