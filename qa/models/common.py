# qa/models/common.py — Shared types for the QA testing suite

import re
from enum import Enum

from pydantic import BaseModel, Field


class Platform(str, Enum):
    WEB = "web"
    MOBILE = "mobile"


class ElementType(str, Enum):
    TEXT_INPUT = "text_input"
    DROPDOWN = "dropdown"
    DATE_PICKER = "date_picker"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    FILE_UPLOAD = "file_upload"
    BUTTON = "button"
    LINK = "link"
    COMBOBOX = "combobox"
    NAV_TAB = "nav_tab"
    OTHER = "other"


class Locator(BaseModel):
    """A single locator strategy for finding an element."""
    strategy: str           # "css", "xpath", "label", "coordinates", "uid", "accessibility_id"
    value: str              # The actual selector/coords/uid
    confidence: float = 1.0 # 1.0 = always works, 0.5 = flaky


class TargetApp(BaseModel):
    """Identifies the application under test — works for both web and mobile."""
    platform: Platform
    url: str | None = None              # Web: page URL
    package_name: str | None = None     # Mobile: Android package
    app_name: str                       # Human-readable name
    device_id: str | None = None        # Mobile: ADB device ID


def make_element_id(screen_name: str, element_label: str, element_type: str) -> str:
    """Deterministic, stable element ID.

    Format: {screen_name}:{element_label}:{element_type}
    Lowercased, spaces/special chars replaced with underscores.

    Examples:
        make_element_id("iTeller", "Select Transaction Type", "dropdown")
        → "iteller:select_transaction_type:dropdown"

        make_element_id("Contact Info", "First Name", "text_input")
        → "contact_info:first_name:text_input"
    """
    def _normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")

    return f"{_normalize(screen_name)}:{_normalize(element_label)}:{_normalize(element_type)}"
