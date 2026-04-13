# qa/engine/orchestrator.py — Unified turn loop for all pipelines

import json
import time
from dataclasses import dataclass, field

from agents import Agent, Runner, ModelSettings
from agents.lifecycle import RunHooks
from agents.items import ModelResponse
from agents.run_context import RunContextWrapper
from agents.run_error_handlers import RunErrorHandlerInput, RunErrorHandlerResult

from qa.engine.budget import BudgetTracker
from qa.engine.compactor import compact_history, update_summary


@dataclass
class LoopResult:
    """Result of running the agent loop."""
    final_output: str = ""
    turns_used: int = 0
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    status: str = "completed"  # "completed", "max_turns", "budget_exceeded", "error"
    turn_log: list[dict] = field(default_factory=list)
    rolling_summary: str = ""


class TurnLogger(RunHooks):
    """Hook that tracks per-turn token usage and prints live stats."""

    def __init__(self, model: str = "") -> None:
        self.turn = 0
        self.model = model
        self._header_printed = False
        self.budget = BudgetTracker(model)

    async def on_llm_end(
        self, context: RunContextWrapper, agent: Agent, response: ModelResponse
    ) -> None:
        self.turn += 1
        usage = response.usage

        t_input = getattr(usage, "input_tokens", 0) or 0
        t_output = getattr(usage, "output_tokens", 0) or 0
        t_cached = 0
        details = getattr(usage, "input_tokens_details", None)
        if details:
            t_cached = getattr(details, "cached_tokens", 0) or 0

        self.budget.record_turn(t_input, t_output, t_cached)
        cache_pct = (t_cached / t_input * 100) if t_input > 0 else 0

        # Extract action for display
        action = "text_output"
        target = ""
        for item in response.output:
            name = getattr(item, "name", None)
            if name:
                action = name.replace("mobile_", "")[:28]
                args_raw = getattr(item, "arguments", "{}")
                try:
                    args = json.loads(args_raw)
                    if "x" in args and "y" in args:
                        target = f"({args['x']},{args['y']})"
                    elif "text" in args:
                        target = str(args["text"])[:20]
                    elif "packageName" in args:
                        target = str(args["packageName"])[:20]
                    elif "button" in args:
                        target = args["button"]
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        if not self._header_printed:
            print(f"  {'Turn':<5} {'Action':<30} {'Target':<20} {'Input':>7} {'Cached':>7} {'Out':>6} {'Cache%':>7}")
            print(f"  {'-'*5} {'-'*30} {'-'*20} {'-'*7} {'-'*7} {'-'*6} {'-'*7}")
            self._header_printed = True

        print(f"  {self.turn:<5} {action:<30} {target:<20} {t_input:>7,} {t_cached:>7,} {t_output:>6,} {cache_pct:>6.0f}%")


async def run_agent_loop(
    agent: Agent,
    task: str,
    max_turns: int = 15,
    model: str = "gpt-5.1",
    budget: float = 2.0,
    keep_last_n: int = 2,
    nudge_before_end: int = 3,
    nudge_message: str = "",
    phase_switch_marker: str = "",
    phase_switch_agent: Agent | None = None,
    phase_switch_task_fn: callable = None,
) -> LoopResult:
    """Run the agent in a managed turn loop with compaction, budget, and loop detection.

    Args:
        agent: The Agent to run
        task: Initial task string
        max_turns: Maximum turns allowed
        model: Model name (for cost tracking)
        budget: Max budget in USD
        keep_last_n: Turns to keep raw (rest gets compacted)
        nudge_before_end: Inject nudge message N turns before end
        nudge_message: Message to inject as end-of-run nudge
        phase_switch_marker: Text in final_output that triggers phase switch (e.g., "## TEST PLAN")
        phase_switch_agent: Agent to switch to when marker detected
        phase_switch_task_fn: Callable(final_output) → new task string for phase 2
    """
    logger = TurnLogger(model=model)
    logger.budget.max_budget = budget

    start_time = time.time()
    input_items: str | list = task
    rolling_summary = ""
    turn_log: list[dict] = []
    final_output = ""
    agent_done = False
    phase_switched = False
    recent_tools: list[str] = []  # Track last N tool names for loop detection

    for turn_num in range(1, max_turns + 1):

        def _on_max_turns(info: RunErrorHandlerInput) -> RunErrorHandlerResult:
            return RunErrorHandlerResult(final_output="__TURN_LIMIT__", include_in_history=False)

        t_start = time.time()
        print(f"\n  ⏱ Turn {turn_num}: sending to API...", end="", flush=True)

        result = await Runner.run(
            starting_agent=agent,
            input=input_items,
            max_turns=1,
            hooks=logger,
            error_handlers={"max_turns": _on_max_turns},
        )

        t_elapsed = time.time() - t_start
        print(f" done ({t_elapsed:.1f}s)")
        if t_elapsed > 30:
            print(f"  ⚠ SLOW RESPONSE: {t_elapsed:.1f}s — likely API latency")

        # ── Loop detection ────────────────────────────────────
        # Extract tool name from this turn's output
        turn_tool = ""
        for item in result.raw_responses[-1].output if result.raw_responses else []:
            name = getattr(item, "name", None)
            if name:
                turn_tool = name
                break
        if turn_tool:
            recent_tools.append(turn_tool)
            # Check if last 4 calls were the same tool
            if len(recent_tools) >= 4 and len(set(recent_tools[-4:])) == 1:
                repeat_tool = recent_tools[-1]
                print(f"\n  ⚠ LOOP DETECTED: '{repeat_tool}' called 4 times in a row")
                print(f"  Injecting nudge to break the loop...")
                # Force agent to produce final output
                input_items = [
                    *result.to_input_list(),
                    {
                        "role": "user",
                        "content": (
                            f"STOP. You've called {repeat_tool} {len(recent_tools[-4:])} times "
                            "in a row without making progress. Do NOT call any more tools. "
                            "Produce your FINAL OUTPUT (KNOWLEDGE JSON, TEST REPORT, or whatever "
                            "the task asked for) in THIS turn. No more tool calls."
                        ),
                    },
                ]
                recent_tools = []  # Reset so we don't re-trigger immediately
                continue

        # Check for final output
        if result.final_output and result.final_output != "__TURN_LIMIT__":
            final_output = result.final_output
            agent_done = True

        # Update rolling summary
        full_history = result.to_input_list()
        new_items_count = len(result.new_items) if hasattr(result, "new_items") else 3
        recent = full_history[-new_items_count:] if new_items_count > 0 else []
        rolling_summary = update_summary(rolling_summary, recent)

        # Budget check
        if logger.budget.budget_exceeded:
            print(f"\n  BUDGET LIMIT: ${logger.budget.current_cost:.4f} >= ${budget}")
            break
        if logger.budget.budget_warning:
            print(f"\n  BUDGET WARNING: ${logger.budget.current_cost:.4f} ({logger.budget.current_cost/budget*100:.0f}%)")

        # Runaway detection
        if logger.budget.is_runaway():
            print(f"\n  RUNAWAY WARNING: last turn cost unusually high")

        # Phase switch (e.g., plan → execute)
        if phase_switch_marker and agent_done and phase_switch_marker in final_output and not phase_switched:
            phase_switched = True
            final_output_phase1 = final_output
            final_output = ""
            agent_done = False

            if phase_switch_agent:
                agent = phase_switch_agent
            if phase_switch_task_fn:
                input_items = phase_switch_task_fn(final_output_phase1)

            rolling_summary = f"Phase 1 complete. {rolling_summary}"
            print(f"\n  PHASE SWITCH detected. Continuing with phase 2...\n")
            continue

        # Stop if done
        if agent_done:
            print(f"\n  Agent completed in {turn_num} turns.")
            break

        # End-of-run nudge
        turns_remaining = max_turns - turn_num
        if turns_remaining == nudge_before_end and nudge_message:
            print(f"\n  NUDGE: {turns_remaining} turns left")
            full_history.append({"role": "user", "content": nudge_message})

        # Compact history
        input_items = compact_history(full_history, rolling_summary, keep_last_n=keep_last_n)

    duration = time.time() - start_time

    status = "completed" if agent_done else "max_turns"
    if logger.budget.budget_exceeded:
        status = "budget_exceeded"

    return LoopResult(
        final_output=final_output,
        turns_used=logger.turn,
        cost_usd=logger.budget.current_cost,
        duration_sec=duration,
        status=status,
        rolling_summary=rolling_summary,
    )
