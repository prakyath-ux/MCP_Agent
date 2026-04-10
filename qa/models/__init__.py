# qa/models — Pydantic contracts for the QA testing suite
from .common import Platform, ElementType, Locator, TargetApp, make_element_id
from .knowledge import (
    L0Element, L1Element, L2Element, RunRecord,
    ScreenKnowledge, KnowledgeBase,
)
from .explore import ExploreInput, ExploreOutput, DeltaReport, DeltaChange
from .plan import PlanInput, PlanOutput, TestCase, TestApproach, TestCasePriority
from .execute import (
    ExecuteInput, ExecuteOutput, TestResult, BugReport,
    TestSummary, TestStatus,
)
