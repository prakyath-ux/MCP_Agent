# qa/orchestrators/agent_run.py — Phase 2: autonomous strategist
# orchestrator. One unified entry point that picks a strategy per page
# via the PICK_STRATEGY LLM call and dispatches to the registered
# handler. Replaces the manual --wizard / --gated / --loop choice with
# a single command.
#
# Usage:
#   python -m qa.orchestrators.agent_run URL --app-name TECU [--wait]
#
# The runner stops when the strategist returns terminal / blocked /
# unknown, when an iteration fails, when no advance happens (stuck on
# a page), or when --max-iterations is reached. Every decision +
# outcome lands in artifacts/results/{app}_agent_{ts}.json so the
# whole run is auditable.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from qa.adapters import make_adapter
from qa.config import load_defaults
from qa.config.defaults import randomize_pii
from qa.engine.budget import BudgetTracker
from qa.knowledge.store import KnowledgeStore
from qa.models import KnowledgeBase, Platform, TargetApp
from qa.orchestrators.llm_subtask import llm_classify
from qa.orchestrators.strategies import (
    STRATEGY_HANDLERS,
    StrategyContext,
    StrategyOutcome,
)
from qa.orchestrators.sub_prompts import (
    PICK_STRATEGY_PROMPT,
    PICK_STRATEGY_SCHEMA,
)


@dataclass
class AuditEntry:
    """One iteration's record. Saved to disk per-iteration so a crash
    leaves a usable partial trace."""
    iteration: int
    timestamp: str
    strategy: str
    confidence: str
    reasoning: str
    success: bool
    advance: bool
    note: str
    cost: float
    duration_sec: float
    error: str = ""


@dataclass
class AgentRunReport:
    app_name: str
    url: str
    started_at: str
    finished_at: str = ""
    iterations: list[AuditEntry] = field(default_factory=list)
    total_cost: float = 0.0
    final_status: str = "running"
    final_reason: str = ""


def _audit_path(app_name: str) -> Path:
    """Where the audit log lives. Mirrors execute_flow's results layout."""
    import re
    safe = re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("artifacts/results")
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{safe}_agent_{ts}.json"


def _save_audit(path: Path, report: AgentRunReport) -> None:
    """Atomic write. Each iteration triggers a save so a crash
    preserves the trace up to the failure point."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(report), indent=2))
    tmp.replace(path)


async def pick_strategy(
    adapter,
    budget: BudgetTracker,
    model: str,
    replan_hint: str = "",
) -> tuple[str, str, str]:
    """Run the PICK_STRATEGY LLM call. Returns (name, confidence, reason).
    On any failure (LLM down, parse error), returns ('unknown', 'low',
    '<error>') so the runner stops cleanly rather than acting on stale
    or hallucinated picks.

    replan_hint: optional context note from the previous strategy. When
    a strategy bows out via request_replan it can describe what just
    changed on the page (modal dismissed, sections complete, etc.) so
    the strategist doesn't blindly re-pick the same recipe. Empty on
    the first iteration of a page.
    """
    try:
        snap = await adapter.raw_snapshot_text()
    except Exception as e:
        return ("unknown", "low", f"snapshot failed: {type(e).__name__}: {e}")

    user = f"PAGE_SNAPSHOT:\n{(snap or '')[:8000]}\n"
    if replan_hint:
        user += (
            f"\nCONTEXT_FROM_PREVIOUS_STRATEGY:\n{replan_hint}\n"
            f"Pick the strategy for the CURRENT page state, not what "
            f"the page looked like before. If the previous strategy "
            f"already captured all sections / filled all fields, route "
            f"to wizard_step to handle the final Save & Continue submit "
            f"rather than re-running the same recipe.\n"
        )
    try:
        result = await llm_classify(
            PICK_STRATEGY_PROMPT,
            user,
            PICK_STRATEGY_SCHEMA,
            model=model,
            budget=budget,
            label="pick_strategy",
        )
    except Exception as e:
        return ("unknown", "low", f"strategist call failed: {type(e).__name__}: {e}")

    name = (result.get("strategy") or "unknown").strip()
    conf = (result.get("confidence") or "low").strip()
    reason = (result.get("reasoning") or "").strip()
    if name not in STRATEGY_HANDLERS:
        return ("unknown", "low", f"strategist returned unknown name: {name!r}")
    return (name, conf, reason)


async def run_agent(
    url: str,
    app_name: str,
    *,
    wait_for_manual_nav: bool,
    max_iterations: int,
    budget_cap: float,
    model: str,
    randomize: bool,
    defaults_path: str | None,
    reveal_scan: bool = False,
) -> int:
    """Top-level autonomous loop. Returns process exit code."""

    app = TargetApp(platform=Platform.WEB, url=url, app_name=app_name)

    defaults = load_defaults(app_name, path=defaults_path)
    print(f"  {defaults.summary()}")

    if randomize:
        applied = randomize_pii(defaults)
        print()
        print("=" * 60)
        if applied:
            print(f"  ★ RANDOMIZED {len(applied)} PII default(s) for this run:")
            for k, v in applied.items():
                print(f"      {k!r:20} → {v!r}")
        else:
            print("  ★ randomize requested but no PII keys present in defaults")
        print("=" * 60)

    store = KnowledgeStore()
    kb = store.load(app) or KnowledgeBase(app=app)
    if kb.screens:
        print(
            f"  Loaded KB with {len(kb.screens)} screen(s): "
            f"{[s.screen_name for s in kb.screens]}"
        )

    audit_path = _audit_path(app_name)
    report = AgentRunReport(
        app_name=app_name,
        url=url,
        started_at=datetime.now().isoformat(),
    )
    print(f"  Audit log: {audit_path}")

    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    if wait_for_manual_nav:
        print()
        print("=" * 60)
        print("  PAUSE: navigate to the page where the agent should start.")
        print("  Press Enter when the page is fully visible.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to start the agent... ")
        except EOFError:
            pass

    budget = BudgetTracker(model=model, max_budget=budget_cap)

    # Track consecutive same-page-expansion outcomes to break out of
    # the inline-OTP-style loops we already saw on TECU.
    expansion_streak = 0
    EXPANSION_LIMIT = 3
    # Track consecutive request_replan outcomes (strategy bowed out on
    # a changed page state and asked the strategist to re-pick). Cap is
    # small because each replan costs an LLM call and must produce a
    # different next move; if the same page keeps requesting replans we
    # are looping rather than progressing.
    replan_streak = 0
    REPLAN_LIMIT = 2
    # Note from the previous strategy that requested a replan. Passed
    # to pick_strategy so the strategist understands what just happened
    # on the page and doesn't re-pick the strategy that just bowed out.
    # Cleared after each non-replan iteration.
    replan_hint = ""
    exit_code = 0
    # Track screens captured in THIS run (not historical KB) so the
    # wizard's carryover tagger doesn't poison the per-page L0 with
    # leftover screens from prior sessions.
    in_run_screens: list[str] = []

    try:
        for iteration in range(1, max_iterations + 1):
            print()
            print("=" * 60)
            print(f"  [agent] iteration {iteration}/{max_iterations}")
            print("=" * 60)

            # ── 0. Wait for the page to actually render before picking
            # a strategy. Apps with loading spinners / progress bars
            # between transitions confuse the strategist — it saw an
            # in-flight loading state on TECU after Verify OTP and
            # returned 'unknown'. Bound to 12s; falls through to pick
            # anyway if nothing rendered (so genuinely-empty pages
            # still surface as 'unknown').
            from qa.orchestrators.wizard_steps import wait_for_content_render
            rendered, n_int, wait_el = await wait_for_content_render(
                adapter, min_interactive_elements=3, timeout=12.0,
            )
            if not rendered:
                print(
                    f"  [agent] ⚠ page only had {n_int} interactive element(s) "
                    f"after {wait_el:.1f}s — picking strategy anyway"
                )

            # ── 1. Pick a strategy
            name, conf, reason = await pick_strategy(
                adapter, budget, model, replan_hint=replan_hint,
            )
            # Hint is one-shot: it described the prior strategy's exit,
            # not a permanent fact about the page. Clear it now; if THIS
            # iteration also requests a replan, the loop sets a fresh one.
            replan_hint = ""
            print(f"  [agent] strategy={name} confidence={conf}")
            print(f"  [agent] reason: {reason}")

            entry = AuditEntry(
                iteration=iteration,
                timestamp=datetime.now().isoformat(),
                strategy=name,
                confidence=conf,
                reasoning=reason,
                success=False,
                advance=False,
                note="",
                cost=0.0,
                duration_sec=0.0,
            )

            # ── 2. Terminal / blocked / unknown short-circuit
            if name in ("terminal", "blocked", "unknown"):
                entry.success = (name == "terminal")
                entry.note = f"strategist chose {name}"
                report.iterations.append(entry)
                report.final_status = name
                report.final_reason = reason
                _save_audit(audit_path, report)
                print(f"  [agent] stopping — {name}: {reason}")
                break

            # ── 3. Dispatch
            handler = STRATEGY_HANDLERS[name]
            ctx = StrategyContext(
                adapter=adapter,
                kb=kb,
                defaults=defaults,
                budget=budget,
                app_name=app_name,
                page_url=url,
                section_num=iteration,
                model=model,
                reveal_scan=reveal_scan,
                in_run_screens=list(in_run_screens),
            )

            t0 = time.time()
            try:
                outcome: StrategyOutcome = await handler(ctx)
            except Exception as e:
                outcome = StrategyOutcome(
                    success=False, advance=False,
                    note=f"handler raised {type(e).__name__}: {e}",
                    error=f"{type(e).__name__}: {e}",
                    cost=0.0,
                )
            duration = time.time() - t0

            entry.success = outcome.success
            entry.advance = outcome.advance
            entry.note = outcome.note
            entry.cost = outcome.cost
            entry.error = outcome.error
            entry.duration_sec = round(duration, 2)
            report.iterations.append(entry)
            report.total_cost = budget.current_cost
            _save_audit(audit_path, report)

            print(
                f"  [agent] outcome: success={outcome.success} "
                f"advance={outcome.advance} cost=${outcome.cost:.4f} "
                f"({duration:.1f}s)"
            )
            print(f"  [agent] note: {outcome.note}")

            # Record screen captured this iteration so the wizard's
            # carryover tagger has the right comparison set on later
            # iterations. Even if advance=False, the screen was
            # captured (and is in the KB), so include it.
            if outcome.captured is not None:
                cap_name = outcome.captured.screen_name
                if cap_name and cap_name not in in_run_screens:
                    in_run_screens.append(cap_name)

            # ── 4. Continue / stop logic
            if not outcome.success:
                report.final_status = "error"
                report.final_reason = outcome.error or outcome.note
                _save_audit(audit_path, report)
                print(f"  [agent] stopping — handler failed: {outcome.error or outcome.note}")
                exit_code = 1
                break

            if not outcome.advance:
                # Soft handoff: the strategy didn't advance the wizard but
                # explicitly asked the strategist to re-evaluate (typically
                # because it changed page state — dismissed a modal,
                # revealed a new form — and the next move belongs to a
                # different strategy). Continue the loop instead of
                # stopping, but cap consecutive replans so a buggy
                # strategy can't burn the budget in place.
                if outcome.request_replan and replan_streak < REPLAN_LIMIT:
                    replan_streak += 1
                    replan_hint = (
                        f"Previous strategy '{name}' bowed out without "
                        f"advancing. Note: {outcome.note}"
                    )
                    print(
                        f"  [agent] strategy requested replan "
                        f"({replan_streak}/{REPLAN_LIMIT}) — "
                        f"re-picking on changed page state"
                    )
                    continue

                report.final_status = "stuck"
                if outcome.request_replan:
                    report.final_reason = (
                        f"replan limit ({REPLAN_LIMIT}) hit: {outcome.note}"
                    )
                else:
                    report.final_reason = outcome.note
                _save_audit(audit_path, report)
                print(f"  [agent] stopping — no advance: {report.final_reason}")
                break

            # Strategy advanced — reset the replan counter.
            replan_streak = 0

            # Track inline expansion loops (e.g. OTP shown then hidden
            # repeatedly on a duplicate-user form). Wizard's classifier
            # catches NEW_PAGE vs EXPANSION; the agent runner enforces
            # the budget for "how many expansions in a row before we
            # decide we're stuck".
            if "same page expanded" in outcome.note.lower():
                expansion_streak += 1
                if expansion_streak > EXPANSION_LIMIT:
                    report.final_status = "expansion_loop"
                    report.final_reason = (
                        f"{expansion_streak} consecutive same-page expansions"
                    )
                    _save_audit(audit_path, report)
                    print(
                        f"  [agent] stopping — {expansion_streak} consecutive "
                        f"expansions, app likely re-triggering an inline step"
                    )
                    break
            else:
                expansion_streak = 0

            # Budget cap
            if budget.current_cost >= budget_cap:
                report.final_status = "budget_exceeded"
                report.final_reason = f"hit budget cap ${budget_cap:.2f}"
                _save_audit(audit_path, report)
                print(f"  [agent] stopping — budget cap ${budget_cap:.2f} reached")
                break

        else:
            report.final_status = "max_iterations"
            report.final_reason = f"reached --max-iterations={max_iterations}"
            _save_audit(audit_path, report)
            print(f"  [agent] stopping — reached --max-iterations={max_iterations}")

    finally:
        await adapter.close()
        report.finished_at = datetime.now().isoformat()
        report.total_cost = budget.current_cost
        _save_audit(audit_path, report)

    # ── Final summary
    print()
    print("=" * 60)
    print(f"  Final status: {report.final_status}")
    if report.final_reason:
        print(f"  Reason: {report.final_reason}")
    print(f"  Iterations: {len(report.iterations)}")
    print(f"  Total cost: ${report.total_cost:.4f}")
    print(f"  Audit log: {audit_path}")

    if kb.screens:
        print(f"\n  KB ({len(kb.screens)} screen(s)):")
        for s in kb.screens:
            print(f"    - {s.screen_name}: {len(s.l0)} elements")

    return exit_code


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Phase 2 autonomous orchestrator. The agent picks a "
            "strategy per page (wizard / gated / terminal / blocked / "
            "unknown) and dispatches it. One command for the whole app."
        ),
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", required=True, help="App name (e.g. TECU)")
    ap.add_argument(
        "--wait", action="store_true",
        help="Pause after browser launch for manual navigation / login.",
    )
    ap.add_argument(
        "--max-iterations", type=int, default=12,
        help="Hard cap on agent iterations (default 12, covers a 6-page "
             "wizard + same-page expansions comfortably).",
    )
    ap.add_argument(
        "--budget", type=float, default=2.0,
        help="USD budget cap across all LLM calls (default $2.00).",
    )
    ap.add_argument(
        "--model", default="gpt-5.1",
        help="Model used for strategist + sub-tasks.",
    )
    rand_grp = ap.add_mutually_exclusive_group()
    rand_grp.add_argument(
        "--randomize", dest="randomize", action="store_true", default=None,
        help="Force PII randomization (default ON for autonomous runs).",
    )
    rand_grp.add_argument(
        "--no-randomize", dest="randomize", action="store_false",
        help="Use static PII defaults (reproducibility / debugging).",
    )
    ap.add_argument(
        "--reveal-scan", action="store_true",
        help=(
            "Enable the LLM 'reveal-on-click button' scan after fill. OFF by "
            "default — in TECU testing it misclassified dropdown display "
            "labels as reveal buttons and corrupted Save & Continue. Turn "
            "on for apps with genuine '+ Add Another' / 'Show Advanced' "
            "buttons that the wizard would otherwise skip."
        ),
    )
    ap.add_argument(
        "--defaults", default="",
        help="Path to defaults JSON. Defaults to artifacts/defaults/<app>.json.",
    )
    args = ap.parse_args()

    randomize = bool(args.randomize) if args.randomize is not None else True
    return await run_agent(
        url=args.url,
        app_name=args.app_name,
        wait_for_manual_nav=args.wait,
        max_iterations=args.max_iterations,
        budget_cap=args.budget,
        model=args.model,
        randomize=randomize,
        defaults_path=args.defaults.strip() or None,
        reveal_scan=args.reveal_scan,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
