# qa/models/explore.py — Pipeline 1 (Explore/Discovery) contracts

from pydantic import BaseModel, Field

from .common import TargetApp
from .knowledge import KnowledgeBase


class DeltaChange(BaseModel):
    """A single field change detected between scans."""
    element_id: str
    field: str          # "type", "required", "options", "label"
    old_value: str
    new_value: str


class DeltaReport(BaseModel):
    """What changed between the previous and current exploration."""
    added: list[str] = Field(default_factory=list)      # New element_ids
    removed: list[str] = Field(default_factory=list)    # Disappeared element_ids
    changed: list[DeltaChange] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


class ExploreInput(BaseModel):
    app: TargetApp
    screens: list[str] = Field(default_factory=list)    # Empty = discover all screens
    existing_knowledge: KnowledgeBase | None = None     # For delta detection
    model: str = "gpt-5.1"
    provider: str = "openai_chat"
    max_turns: int = 20
    budget: float = 2.0


class ExploreOutput(BaseModel):
    knowledge: KnowledgeBase
    delta: DeltaReport | None = None
    duration_sec: float = 0.0
    cost_usd: float = 0.0
    turns_used: int = 0
    model: str = ""
