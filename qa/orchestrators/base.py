# qa/orchestrators/base.py — Orchestrator Protocol + shared RunContext

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from qa.adapters.protocol import PlatformAdapter
from qa.engine.budget import BudgetTracker
from qa.models import ExploreInput, KnowledgeBase


@dataclass
class RunContext:
    """Shared state passed to an orchestrator for one run.

    The adapter is already launched and the page is already loaded
    (the caller — explore pipeline or CLI — handles that).
    """
    adapter: PlatformAdapter
    inp: ExploreInput
    knowledge: KnowledgeBase
    budget: BudgetTracker
    available_files: list[str] = field(default_factory=list)


@runtime_checkable
class Orchestrator(Protocol):
    """Orchestrates a multi-step UI pattern using Python control flow +
    narrow LLM sub-tasks. See GatedMultiSectionFlow for a reference impl.
    """
    pattern_name: str

    async def run(self, ctx: RunContext) -> KnowledgeBase: ...
