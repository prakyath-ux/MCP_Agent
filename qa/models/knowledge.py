# qa/models/knowledge.py — Layered knowledge base (L0/L1/L2)

from datetime import datetime

from pydantic import BaseModel, Field

from .common import ElementType, Locator, TargetApp


# ── L0: Planning Index (what LLM reads, ~100 tokens/element) ─────────────────

class L0Element(BaseModel):
    element_id: str                                     # "iteller:transaction_type:dropdown"
    name: str                                           # "Select Transaction Type"
    type: ElementType                                   # ElementType.DROPDOWN
    required: bool = False
    behavior: str = ""                                  # "opens a list of transaction types"
    options: list[str] = Field(default_factory=list)     # ["Cash Deposit", "Cash Withdrawal"]
    interaction_order: int = 0                           # Suggested fill order on screen
    default_value: str = ""                              # Pre-filled value if any
    validation_rules: str = ""                           # "required (asterisk)", "max 50 chars"
    screen_name: str = ""
    # File-upload-only hints captured during explore so plan can auto-resolve
    # a test file path without prompting for one.
    accept: str = ""                                     # "image/*", ".pdf,application/pdf"
    semantic_hint: str = ""                              # profile_picture | id_document | bank_statement | proof_of_address | signature | other
    # Wall 1.1 — element_ids of fields that must be set before this one
    # becomes testable (cascaded dropdowns: Employment Status → Sector →
    # Employment Type). Extract populates when it can detect; callers can
    # also supply via the defaults sidecar for manual control.
    depends_on: list[str] = Field(default_factory=list)


# ── L1: Execution Details (what tools use, never in LLM context) ─────────────

class L1Element(BaseModel):
    element_id: str
    locators: list[Locator] = Field(default_factory=list)   # Ordered by confidence (highest first)
    retry_strategy: str = "standard"                         # "standard", "js_fallback", "coordinate_tap"
    last_known_coordinates: dict | None = None               # {"x": 573, "y": 1189} for mobile
    widget_type: str = ""                                    # "android.widget.EditText", "input"
    identifier: str = ""                                     # Android resource-id or CSS data-testid
    screen_name: str = ""


# ── L2: Evidence/History (append-only, audit trail) ──────────────────────────

class RunRecord(BaseModel):
    date: str = Field(default_factory=lambda: datetime.now().isoformat())
    model: str = ""
    value_entered: str = ""
    accepted: bool = False
    issues: str | None = None
    error_text: str = ""
    duration_ms: int = 0


class L2Element(BaseModel):
    element_id: str
    runs: list[RunRecord] = Field(default_factory=list)
    change_log: list[str] = Field(default_factory=list)             # ["2026-04-10: discovered"]
    accessibility_issues: list[str] = Field(default_factory=list)
    first_seen: str = Field(default_factory=lambda: datetime.now().isoformat())
    last_seen: str = Field(default_factory=lambda: datetime.now().isoformat())


# ── Screen Knowledge (one screen/page) ──────────────────────────────────────

class ScreenKnowledge(BaseModel):
    screen_name: str
    screen_url: str = ""                    # Web: page URL; Mobile: package/screen
    l0: list[L0Element] = Field(default_factory=list)
    l1: list[L1Element] = Field(default_factory=list)
    l2: list[L2Element] = Field(default_factory=list)


# ── Knowledge Base (entire application, multiple screens) ────────────────────

def _lookup_tolerant(screens, element_id, pick_list):
    """Exact-then-fuzzy element lookup across screens.

    Extract may emit a 4-part element_id (`screen:section:label:type`)
    when a section heading is detected, or a 3-part one
    (`screen:label:type`) when no section is found. The defaults sidecar
    typically uses the canonical 3-part form. This helper lets callers
    look up an element using either shape.

    Strategy:
      1. Exact match on `element_id` across all screens
      2. If given 4-part, try the 3-part canonical (drop section at [1])
      3. If given 3-part, match any 4-part entry with the same
         (screen, label, type)
    """
    if not element_id:
        return None

    # 1. exact
    for s in screens:
        for el in pick_list(s):
            if el.element_id == element_id:
                return el

    parts = element_id.split(":")
    # 2. 4-part → 3-part
    if len(parts) == 4:
        alt = f"{parts[0]}:{parts[2]}:{parts[3]}"
        for s in screens:
            for el in pick_list(s):
                if el.element_id == alt:
                    return el
    # 3. 3-part → any matching 4-part
    if len(parts) == 3:
        screen, label, etype = parts
        for s in screens:
            for el in pick_list(s):
                ep = el.element_id.split(":")
                if (
                    len(ep) == 4
                    and ep[0] == screen and ep[2] == label and ep[3] == etype
                ):
                    return el
    return None


class KnowledgeBase(BaseModel):
    app: TargetApp
    screens: list[ScreenKnowledge] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    version: int = 1

    def get_screen(self, screen_name: str) -> ScreenKnowledge | None:
        name_lower = screen_name.lower()
        for s in self.screens:
            if s.screen_name.lower() == name_lower:
                return s
        return None

    def get_l0_index(self, screen_name: str | None = None) -> list[L0Element]:
        """Return L0 elements — the compact planning index the LLM reads."""
        if screen_name:
            screen = self.get_screen(screen_name)
            return screen.l0 if screen else []
        return [el for s in self.screens for el in s.l0]

    def get_l0_for_element(self, element_id: str) -> L0Element | None:
        return _lookup_tolerant(self.screens, element_id, lambda s: s.l0)

    def get_l1_for_element(self, element_id: str) -> L1Element | None:
        return _lookup_tolerant(self.screens, element_id, lambda s: s.l1)

    def get_l2_for_element(self, element_id: str) -> L2Element | None:
        return _lookup_tolerant(self.screens, element_id, lambda s: s.l2)

    def screen_names(self) -> list[str]:
        return [s.screen_name for s in self.screens]
