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

    def get_l1_for_element(self, element_id: str) -> L1Element | None:
        for s in self.screens:
            for el in s.l1:
                if el.element_id == element_id:
                    return el
        return None

    def get_l2_for_element(self, element_id: str) -> L2Element | None:
        for s in self.screens:
            for el in s.l2:
                if el.element_id == element_id:
                    return el
        return None

    def screen_names(self) -> list[str]:
        return [s.screen_name for s in self.screens]
