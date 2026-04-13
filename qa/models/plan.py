# qa/models/plan.py — Pipeline 2 (Test Case Planning) contracts

from enum import Enum

from pydantic import BaseModel, Field

from .knowledge import KnowledgeBase


class TestCasePriority(str, Enum):
    HIGH = "high"
    MED = "med"
    LOW = "low"


class TestApproach(str, Enum):
    FILL_CHECK = "fill_check"       # Set value, verify acceptance/error
    TAP_VERIFY = "tap_verify"       # Tap element, verify response
    VERIFY_ONLY = "verify_only"     # Check existence/state only
    SKIP = "skip"                   # Cannot be tested


class TestCase(BaseModel):
    tc_id: str                      # "TC1", "TC2"
    element_id: str                 # Links to L0/L1/L2
    field_name: str                 # Human-readable
    approach: TestApproach
    test_value: str = ""            # The input to set
    expected_result: str = ""       # "error_shown", "no_error", "value_rejected"
    priority: TestCasePriority = TestCasePriority.MED
    description: str = ""           # "empty required field validation"
    screen_name: str = ""


class PlanInput(BaseModel):
    knowledge: KnowledgeBase
    screen_names: list[str] = Field(default_factory=list)   # Empty = all screens
    element_filter: str = ""                                 # "dropdown" | "date_picker" | "text_input" | element name substring
    test_values_hint: list[str] = Field(default_factory=list)  # Specific values the LLM should prefer
    avoid_recent_values: bool = False                          # If True, suggest NOT using values from last L2 run
    max_cases_per_field: int = 3
    max_total_cases: int = 30
    model: str = "gpt-5.1"
    provider: str = "openai_chat"


class PlanOutput(BaseModel):
    test_cases: list[TestCase] = Field(default_factory=list)
    coverage_summary: str = ""      # "15 cases covering 8/8 fields"
    model: str = ""
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    raw_plan_text: str = ""         # Original LLM output for debugging
