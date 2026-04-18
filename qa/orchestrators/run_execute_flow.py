# qa/orchestrators/run_execute_flow.py — CLI entry for ExecuteOrchestrator.
#
# Usage:
#   python -m qa.orchestrators.run_execute_flow URL --app-name TECU --wait
#
# Loads the existing KB + optional defaults.json, generates a plan via
# Pipeline 2, then runs ExecuteOrchestrator with full precision primitives
# wired (guardrails, CoVe, atomic checkpoints, defaults-driven restore).

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from qa.adapters import make_adapter
from qa.config import load_defaults
from qa.engine.budget import BudgetTracker
from qa.engine.guardrails import per_run_scope
from qa.knowledge.store import KnowledgeStore
from qa.models import (
    ExecuteOutput, Platform, PlanInput, TargetApp, TestCase,
)
from qa.orchestrators.execute_flow import (
    ExecuteOrchestrator, ExecuteRunContext, _default_results_path,
)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Path B ExecuteOrchestrator — runs one test at a time in an "
            "isolated setup/test/restore cycle. Inherits guardrails, CoVe, "
            "defaults, and atomic checkpointing from Tier 0 primitives."
        ),
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", required=True, help="App name (e.g. TECU)")
    ap.add_argument(
        "--wait", action="store_true",
        help="Pause after browser launch for manual navigation",
    )
    ap.add_argument("--model", default="gpt-5.1", help="Model for classification")
    ap.add_argument(
        "--budget", type=float, default=1.50,
        help="Hard cost cap for the whole run (matches plan.md Tier 0)",
    )
    ap.add_argument(
        "--defaults", default="",
        help="Path to defaults JSON. If omitted, uses artifacts/defaults/{app_name}.json if present.",
    )
    ap.add_argument("--max-cases", type=int, default=30, help="Plan size cap")
    ap.add_argument(
        "--screens", default="",
        help="Comma-separated screen names to scope test cases to (empty = all)",
    )
    ap.add_argument(
        "--filter", default="",
        help="Element type (dropdown, text_input, ...) or name substring",
    )
    args = ap.parse_args()

    app = TargetApp(platform=Platform.WEB, url=args.url, app_name=args.app_name)

    # Load defaults early — fail fast if path is bad
    defaults = load_defaults(args.app_name, path=args.defaults.strip() or None)
    print(f"  {defaults.summary()}")

    # Load KB
    store = KnowledgeStore()
    kb = store.load(app)
    if kb is None:
        print(f"  ERROR: no KB for app {args.app_name!r} — run form_extract first")
        return 1
    print(f"  KB: {len(kb.screens)} screen(s), "
          f"{sum(len(s.l0) for s in kb.screens)} L0 element(s)")

    # Generate plan via existing Pipeline 2 — unchanged, reuses what works
    from qa.pipelines.plan import run_plan
    screens = [s.strip() for s in args.screens.split(",") if s.strip()]
    plan_out = await run_plan(PlanInput(
        knowledge=kb,
        screen_names=screens,
        element_filter=args.filter,
        max_total_cases=args.max_cases,
        model=args.model,
    ))
    test_cases: list[TestCase] = plan_out.test_cases
    if not test_cases:
        print("  ERROR: plan pipeline returned zero test cases")
        return 1
    print(f"  Plan generated {len(test_cases)} test case(s)")

    # Launch browser
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    if args.wait:
        print()
        print("=" * 60)
        print("  PAUSE: navigate to the target screen for testing.")
        print("  Press Enter when the page is ready.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to start... ")
        except EOFError:
            pass

    # Build context with guardrails wired in
    budget = BudgetTracker(model=args.model, max_budget=args.budget)
    run_gc = per_run_scope()
    run_gc.hard_max_cost = args.budget   # honor CLI budget flag at the guardrail level

    results_path = _default_results_path(args.app_name)
    print(f"  Results will be written incrementally to {results_path}")

    ctx = ExecuteRunContext(
        adapter=adapter,
        knowledge=kb,
        test_cases=test_cases,
        defaults=defaults,
        budget=budget,
        guardrails=run_gc,
        app_name=args.app_name,
        results_path=results_path,
    )

    orchestrator = ExecuteOrchestrator(model=args.model)

    try:
        output: ExecuteOutput = await orchestrator.run(ctx)
    finally:
        await adapter.close()

    # Human-readable summary
    print(f"\n  ── Final Results ──")
    for r in output.results:
        tag = {"pass": "✓", "fail": "✗", "skip": "○", "blocked": "⊘"}.get(
            r.status.value, "?"
        )
        print(f"  {tag} {r.tc_id:5} {r.field_name:35} {r.status.value:7} "
              f"{r.notes[:70] if r.notes else ''}")
    print(f"\n  Total: {output.summary.total}  "
          f"PASS {output.summary.passed}  FAIL {output.summary.failed}  "
          f"SKIP {output.summary.skipped}  BLOCKED {output.summary.blocked}")
    print(f"  Cost: ${output.cost_usd:.4f}  Duration: {output.duration_sec:.1f}s")
    print(f"  Results file: {results_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
