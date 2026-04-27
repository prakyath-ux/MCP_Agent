# qa/orchestrators/run_execute_flow.py — CLI entry for ExecuteOrchestrator.
#
# Usage:
#   python -m qa.orchestrators.run_execute_flow URL --app-name TECU --wait
#
# Loads the existing KB + optional defaults.json, generates a plan via
# Pipeline 2, then runs ExecuteOrchestrator with full precision primitives
# wired (guardrails, CoVe, atomic checkpoints, defaults-driven restore).
#
# --loop: stay in one Chrome session and test multiple screens. After each
# run, the user manually navigates to the next page/section in Chrome and
# presses Enter for another round. Mirrors form_extract --loop. Useful for
# multi-step wizards where wizard state (login, mandatory fields filled,
# OCR-validated uploads) must persist across test runs.

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
from qa.pipelines.plan import run_plan


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
    ap.add_argument(
        "--loop", action="store_true",
        help=(
            "Multi-section mode. After each test run completes, prompts "
            "the user to navigate Chrome to the next screen and press "
            "Enter for another round. Quit with 'q'. Chrome stays open "
            "across iterations so wizard state persists. Mirrors form_"
            "extract --loop. Combine with --wait for the initial setup."
        ),
    )
    ap.add_argument("--model", default="gpt-5.1", help="Model for classification")
    ap.add_argument(
        "--budget", type=float, default=1.50,
        help="Hard cost cap PER ITERATION in --loop mode (else whole run).",
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

    # Launch browser ONCE for the whole session. ExecuteOrchestrator (Path B)
    # uses stateless llm_classify calls — no tools exposed to the LLM — so
    # Wall 1.8 is already structurally enforced. Block the nav tools anyway
    # in case anything downstream spawns a tool-enabled agent.
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(
        app,
        extra_blocked_tools=["navigate_page", "go_back", "go_forward"],
    )

    iteration = 1
    completed: list[tuple[str, "TestSummary"]] = []  # (label, summary)

    try:
        while True:
            # ── Screen selection for this iteration ──────────────────
            if iteration == 1:
                # First pass: use --screens from CLI
                screens = [s.strip() for s in args.screens.split(",") if s.strip()]
            else:
                # Loop iteration: show existing KB screens, ask user to pick.
                existing = kb.screen_names()
                if existing:
                    print(f"\n  Available screens in KB ({len(existing)}):")
                    for i, n in enumerate(existing, 1):
                        print(f"    {i}. {n}")
                try:
                    user_input = input(
                        "  Screen to test (paste exact name, or empty for all, "
                        "or 'q' to quit): "
                    ).strip()
                except EOFError:
                    break
                if user_input.lower() == "q":
                    break
                screens = [user_input] if user_input else []

            label = ", ".join(screens) if screens else "all screens"

            # ── Plan: generate test cases for this iteration ─────────
            plan_out = await run_plan(PlanInput(
                knowledge=kb,
                screen_names=screens,
                element_filter=args.filter,
                max_total_cases=args.max_cases,
                model=args.model,
            ))
            test_cases: list[TestCase] = plan_out.test_cases
            if not test_cases:
                print(f"  ⚠ no test cases generated for {label!r}")
                if not args.loop:
                    return 1
                # In loop mode, prompt to try a different screen
                print("  Try another screen (or 'q' to quit).\n")
                iteration += 1
                continue
            print(f"  Plan generated {len(test_cases)} test case(s) for {label}")

            # ── Pause for navigation: required on first iter if --wait,
            # always required on subsequent iters in --loop mode (user
            # has to navigate to the new screen between runs). ────────
            should_pause = args.wait if iteration == 1 else args.loop
            if should_pause:
                print()
                print("=" * 60)
                if iteration == 1:
                    print(f"  PAUSE: navigate Chrome to the screen for {label!r}.")
                else:
                    print(f"  PAUSE: navigate Chrome to the screen for THIS iteration:")
                    print(f"         {label}")
                    print()
                    print("    Multi-step wizards: fill required fields and click")
                    print("    'Save & Continue' yourself to advance. Then return here.")
                print("  Press Enter when the page is fully visible.")
                print("=" * 60)
                try:
                    input("  >>> Press Enter to start tests... ")
                except EOFError:
                    break

            # ── Execute ──────────────────────────────────────────────
            budget = BudgetTracker(model=args.model, max_budget=args.budget)
            run_gc = per_run_scope()
            run_gc.hard_max_cost = args.budget

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
            output: ExecuteOutput = await orchestrator.run(ctx)

            # ── Per-iteration summary ────────────────────────────────
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

            completed.append((label, output.summary))

            # ── Continue? ────────────────────────────────────────────
            if not args.loop:
                break

            print()
            print("=" * 60)
            print("  Section tested. To advance:")
            print("    1. Manually navigate Chrome to the next page/section")
            print("    2. For multi-step wizards: fill any required fields,")
            print("       upload any required documents, click Save & Continue")
            print("    3. When the next screen is fully visible in Chrome,")
            print("       return here and press Enter")
            print("=" * 60)
            try:
                choice = input(
                    "  >>> Press Enter to test next screen, or 'q' + Enter to quit: "
                ).strip().lower()
            except EOFError:
                break
            if choice == "q":
                break
            iteration += 1
    finally:
        await adapter.close()

    # ── Session summary ──────────────────────────────────────────────
    if len(completed) > 1:
        print(f"\n  ══ Session summary: {len(completed)} test run(s) ══")
        total_pass = total_fail = total_blocked = 0
        for label, smry in completed:
            print(f"    {label}: PASS {smry.passed}  FAIL {smry.failed}  "
                  f"BLOCKED {smry.blocked}  ({smry.total} cases)")
            total_pass += smry.passed
            total_fail += smry.failed
            total_blocked += smry.blocked
        print(f"    ───")
        print(f"    Combined: PASS {total_pass}  FAIL {total_fail}  "
              f"BLOCKED {total_blocked}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
