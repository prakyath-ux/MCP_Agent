# qa/engine/budget.py — Budget tracking, cost calculation, runaway detection

from qa.engine.model_factory import get_pricing


class BudgetTracker:
    """Track token usage and cost during a run."""

    def __init__(self, model: str, max_budget: float = 2.0) -> None:
        self.model = model
        self.max_budget = max_budget
        self.total_input = 0
        self.total_output = 0
        self.total_cached = 0
        self._turn_costs: list[float] = []

    def record_turn(self, input_tokens: int, output_tokens: int, cached_tokens: int) -> None:
        self.total_input += input_tokens
        self.total_output += output_tokens
        self.total_cached += cached_tokens
        self._turn_costs.append(self._cost_for(input_tokens, output_tokens, cached_tokens))

    @property
    def current_cost(self) -> float:
        prices = get_pricing(self.model)
        uncached = self.total_input - self.total_cached
        return (
            uncached * prices["input"]
            + self.total_cached * prices["input_cached"]
            + self.total_output * prices["output"]
        )

    @property
    def budget_exceeded(self) -> bool:
        return self.current_cost >= self.max_budget

    @property
    def budget_warning(self) -> bool:
        return self.current_cost >= self.max_budget * 0.5

    @property
    def cache_hit_pct(self) -> float:
        if self.total_input == 0:
            return 0.0
        return self.total_cached / self.total_input * 100

    def is_runaway(self, multiplier: float = 2.0) -> bool:
        """Check if latest turn cost is suspiciously high vs average."""
        if len(self._turn_costs) < 4:
            return False
        avg = sum(self._turn_costs) / len(self._turn_costs)
        return self._turn_costs[-1] > avg * multiplier

    def _cost_for(self, input_tokens: int, output_tokens: int, cached_tokens: int) -> float:
        prices = get_pricing(self.model)
        uncached = input_tokens - cached_tokens
        return (
            uncached * prices["input"]
            + cached_tokens * prices["input_cached"]
            + output_tokens * prices["output"]
        )
