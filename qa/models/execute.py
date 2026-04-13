# qa/models/execute.py — Pipeline 3 (Test Execution) contracts

from enum import Enum

from pydantic import BaseModel, Field

from .common import TargetApp
from .knowledge import KnowledgeBase
from .plan import TestCase


class TestStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    BLOCKED = "blocked"


class TestResult(BaseModel):
    tc_id: str
    element_id: str
    field_name: str
    test_value: str = ""
    expected: str = ""
    actual: str = ""
    status: TestStatus
    notes: str = ""
    evidence: dict = Field(default_factory=dict)
    duration_ms: int = 0


class BugReport(BaseModel):
    bug_id: str                     # "BUG1", "BUG2"
    field_name: str
    description: str
    severity: str                   # "high", "medium", "low"
    evidence: str = ""
    related_tc: str = ""            # Which TC found it


class TestSummary(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: int = 0


class ExecuteInput(BaseModel):
    app: TargetApp
    test_cases: list[TestCase] = Field(default_factory=list)
    knowledge: KnowledgeBase
    auto_explore: bool = False      # If True, run Pipeline 1 first when no knowledge
    screens: list[str] = Field(default_factory=list)    # Which screens to test
    element_filter: str = ""                             # "dropdown" | "date_picker" | element name substring
    test_values_hint: list[str] = Field(default_factory=list)  # Specific values to test with
    avoid_recent_values: bool = False                          # Use values not seen in recent L2 history
    model: str = "gpt-5.1"
    provider: str = "openai_chat"
    max_turns_per_screen: int = 15
    budget: float = 2.0


class ExecuteOutput(BaseModel):
    results: list[TestResult] = Field(default_factory=list)
    bugs: list[BugReport] = Field(default_factory=list)
    summary: TestSummary = Field(default_factory=TestSummary)
    knowledge_updates: KnowledgeBase | None = None  # Updated L2 with run records
    model: str = ""
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    turns_used: int = 0
