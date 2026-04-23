# qa/orchestrators/execute_flow.py — Path B ExecuteOrchestrator.
#
# Separates the two opposing goals that killed the single-LLM execute:
#   1. TEST the field (try invalid values, expect errors)
#   2. LEAVE form valid (so navigation to next page works)
#
# Cycle per test case:
#   ┌─ SETUP (Python, deterministic) ────────────────────────────────┐
#   │ Fill prerequisite fields from defaults. Ensure target element │
#   │ is reachable. No LLM judgement here.                           │
#   └────────────────────────────────────────────────────────────────┘
#             ↓
#   ┌─ TEST (Python acts, LLM observes) ─────────────────────────────┐
#   │ Python does the test action (fill / click / select).          │
#   │ LLM sub-task classifies PASS / FAIL / BLOCKED from snapshots. │
#   │ CoVe verifies the LLM's error_text claim.                     │
#   └────────────────────────────────────────────────────────────────┘
#             ↓
#   ┌─ RESTORE (Python, deterministic) ──────────────────────────────┐
#   │ Fill target field with the KB default value. Form is now      │
#   │ submittable. Next test starts from a known-good state.        │
#   └────────────────────────────────────────────────────────────────┘
#
# Each test case is INDEPENDENT. Crash after test N → results 1..N-1
# are persisted atomically, run can resume from test N.
#
# All precision primitives inherited from Tier 0 foundation:
#   • GuardrailContext (per-run wraps per-test; hard caps enforced)
#   • CoVe verification on LLM observations
#   • Defaults (qa.config) for prerequisite + restore values
#   • Atomic per-test result writes (KnowledgeStore-style temp+rename)
#
# Approaches handled in this wall:
#   • FILL_CHECK — fill invalid, observe error
#   • VERIFY_ONLY — check element exists with expected state
#   • TAP_VERIFY — click element, observe response
#   • SKIP — record as SKIP immediately
#
# Approaches deferred to follow-up walls:
#   • SELECT_AND_VERIFY — Wall 1.1 (dependent dropdown chaining)
#   • UPLOAD_FILE — Wall 1.4 (OCR-gated replay)

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path

from qa.adapters.protocol import PlatformAdapter
from qa.config import Defaults
from qa.engine.budget import BudgetTracker
from qa.engine.guardrails import (
    GuardrailContext, GuardrailExit,
    per_run_scope, per_test_scope,
)
from qa.models import (
    ExecuteOutput, KnowledgeBase, TestCase, TestResult, TestStatus,
    TestApproach, TestSummary,
)
from qa.orchestrators.llm_subtask import llm_classify
from qa.tools.web_tools import _safe_parse as _parse_mcp_result
from qa.orchestrators.sub_prompts import (
    CLASSIFY_TEST_RESULT_PROMPT,
    CLASSIFY_TEST_RESULT_SCHEMA,
)
from qa.orchestrators.verify import verify_string_fields


RESULTS_DIR = Path("artifacts/results")


# ──────────────────────────────────────────────────────────────────────
# Context + run input — deliberately narrower than the existing
# ExecuteInput to keep the orchestrator's dependencies explicit.
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ExecuteRunContext:
    """Everything ExecuteOrchestrator needs for one run.

    Caller (CLI or chat agent) prepares this and passes it in. The
    orchestrator itself is stateless — all per-run data lives here.
    """
    adapter: PlatformAdapter
    knowledge: KnowledgeBase
    test_cases: list[TestCase]
    defaults: Defaults
    budget: BudgetTracker
    guardrails: GuardrailContext           # usually per_run_scope()
    app_name: str = ""
    results_path: Path | None = None       # auto-derived if None


# Wall 1.3 — outcome of the per-test restore step. Surfaced in result
# notes so broken restores are visible instead of silently leaving a
# page's worth of tests running against an invalid form.

@dataclass
class RestoreOutcome:
    status: str  # "restored" | "no_default" | "fill_failed"
    value: str = ""
    warning: str = ""


# ──────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────

def _safe_app_name(app_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")


def _atomic_write_text(path: Path, content: str) -> None:
    """Same pattern as KnowledgeStore — temp + os.replace for crash safety."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _default_results_path(app_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR / f"{_safe_app_name(app_name)}_flow_{ts}.json"


def _save_results_checkpoint(
    results_path: Path,
    results: list[TestResult],
    summary: TestSummary,
    app_name: str,
    model: str,
    cost_usd: float,
    duration_sec: float,
) -> None:
    """Atomic-write the current results list. Safe to call after each
    test case. No archiving — just overwrite the same file each time."""
    payload = {
        "app_name": app_name,
        "model": model,
        "updated_at": datetime.now().isoformat(),
        "cost_usd": round(cost_usd, 4),
        "duration_sec": round(duration_sec, 1),
        "summary": summary.model_dump(),
        "results": [r.model_dump() for r in results],
    }
    _atomic_write_text(results_path, json.dumps(payload, indent=2))


def _find_l1_locator(kb: KnowledgeBase, element_id: str) -> tuple[str, str]:
    """Resolve the best locator for an element. Returns (strategy, value).
    Preference: css (highest confidence) → xpath (highest confidence) →
    ('', ''). Execute falls back to document.evaluate() for xpath when the
    css slot is empty — some DOM patterns (e.g. <p>-labelled Tailwind
    cards) have no stable CSS anchor because CSS can't match text."""
    l1 = kb.get_l1_for_element(element_id)
    if l1 is None:
        return ("", "")
    css_locators = [loc for loc in l1.locators if loc.strategy == "css"]
    if css_locators:
        best = max(css_locators, key=lambda loc: loc.confidence)
        return ("css", best.value)
    xpath_locators = [loc for loc in l1.locators if loc.strategy == "xpath"]
    if xpath_locators:
        best = max(xpath_locators, key=lambda loc: loc.confidence)
        return ("xpath", best.value)
    return ("", "")


def _find_l1_css_selector(kb: KnowledgeBase, element_id: str) -> str:
    """Legacy CSS-only accessor — retained for callers that still treat
    locators as strings. Prefer `_find_l1_locator` for new code."""
    strategy, value = _find_l1_locator(kb, element_id)
    return value if strategy == "css" else ""


# ──────────────────────────────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────────────────────────────

class ExecuteOrchestrator:
    """Path B executor. One test case at a time, isolated cycles."""

    pattern_name = "execute_flow"

    def __init__(self, model: str = "gpt-5.1") -> None:
        self.model = model

    async def run(self, ctx: ExecuteRunContext) -> ExecuteOutput:
        """Run every test case in ctx. Per-test failures are isolated.
        Guardrail exits halt the run cleanly with partial results saved."""
        run_start = time.time()
        results_path = ctx.results_path or _default_results_path(ctx.app_name)
        results: list[TestResult] = []
        summary = TestSummary(total=len(ctx.test_cases))

        print(f"\n  [execute_flow] ══ ExecuteOrchestrator starting ══")
        print(f"  [execute_flow] {len(ctx.test_cases)} test case(s), "
              f"writing results to {results_path}")

        for i, tc in enumerate(ctx.test_cases):
            print(f"\n  [execute_flow] ── Test {i + 1}/{len(ctx.test_cases)}: "
                  f"{tc.tc_id} {tc.field_name!r} ({tc.approach.value}) ──")
            test_gc = per_test_scope(parent=ctx.guardrails)

            try:
                result = await self._run_test_cycle(tc, ctx, test_gc)
            except GuardrailExit as e:
                print(f"  [execute_flow] guardrail exit on {tc.tc_id}: {e}")
                result = TestResult(
                    tc_id=tc.tc_id,
                    element_id=tc.element_id,
                    field_name=tc.field_name,
                    test_value=tc.test_value,
                    expected=tc.expected_result,
                    actual="",
                    status=TestStatus.BLOCKED,
                    notes=f"guardrail:{e.reason}",
                )

            results.append(result)
            _update_summary(summary, result.status)

            # Atomic checkpoint after every test — never lose more than
            # the test we just ran to a crash.
            _save_results_checkpoint(
                results_path, results, summary,
                app_name=ctx.app_name, model=self.model,
                cost_usd=ctx.budget.current_cost,
                duration_sec=time.time() - run_start,
            )

            # Stop the run if the whole-run guardrail is exhausted —
            # no point burning more budget on remaining tests.
            try:
                ctx.guardrails.check()
            except GuardrailExit as e:
                print(f"  [execute_flow] run-level guardrail hit: {e}")
                print(f"  [execute_flow] stopping early, {len(results)}/{len(ctx.test_cases)} tests completed")
                break

        # G4 — Final audit pass. Any SELECT_AND_VERIFY that ended up
        # BLOCKED because its cascade parents couldn't be resolved
        # against the KB (now rare after G1) OR because a specific
        # parent default didn't unlock the child on first try gets one
        # more attempt: walk alternative parent option values and retry.
        # Bounded: up to 2 alternates per failed test, bails on first
        # success.
        retried = await self._final_audit_retry(ctx, results, results_path, run_start, summary)
        if retried:
            print(f"  [execute_flow] final audit recovered {retried} test(s)")

        duration = time.time() - run_start
        print(f"\n  [execute_flow] ══ Complete: "
              f"{summary.passed} PASS / {summary.failed} FAIL / "
              f"{summary.skipped} SKIP / {summary.blocked} BLOCKED "
              f"in {duration:.1f}s, ${ctx.budget.current_cost:.4f} ══")

        return ExecuteOutput(
            results=results,
            summary=summary,
            model=self.model,
            cost_usd=ctx.budget.current_cost,
            duration_sec=duration,
        )

    # ── G4: Final audit + parent-alternation retry ───────────────────
    async def _final_audit_retry(
        self,
        ctx: ExecuteRunContext,
        results: list[TestResult],
        results_path: Path,
        run_start: float,
        summary: TestSummary,
    ) -> int:
        """For each BLOCKED SELECT_AND_VERIFY test, try alternative
        parent option values and retry the test. Returns the number of
        tests recovered (BLOCKED → PASS/FAIL)."""
        recovered = 0
        kb = ctx.knowledge
        defaults = ctx.defaults

        for idx, result in enumerate(results):
            if result.status != TestStatus.BLOCKED:
                continue
            # Only retry cascade-dep-failed select_verify — restores and
            # classification-BLOCKEDs aren't fixable by parent alt.
            notes = result.notes or ""
            if "dep_failed" not in notes and "not in KB" not in notes:
                continue

            # Find the original test case in ctx.test_cases.
            tc = next((t for t in ctx.test_cases if t.tc_id == result.tc_id), None)
            if tc is None or tc.approach != TestApproach.SELECT_AND_VERIFY:
                continue

            # Find the first parent of this test's dependency chain.
            l0 = kb.get_l0_for_element(tc.element_id)
            deps = list(l0.depends_on) if (l0 and l0.depends_on) else []
            if not deps:
                deps = defaults.get_dependencies(tc.element_id)
            if not deps:
                continue

            first_parent_id = deps[0]
            parent_l0 = kb.get_l0_for_element(first_parent_id)
            if parent_l0 is None or not parent_l0.options:
                continue

            # Current default for this parent (what we tried first).
            current_default = defaults.get(parent_l0.name) or ""
            # Build alternate option list: drop the current_default, take up to 2.
            alt_options = [
                o for o in parent_l0.options
                if current_default.strip().lower() not in o.lower()
            ][:2]
            if not alt_options:
                continue

            print(f"\n  [execute_flow] audit: retrying {tc.tc_id} {tc.field_name!r} "
                  f"with alternate parent {parent_l0.name!r} values: {alt_options}")

            for alt_val in alt_options:
                # Temporarily mutate the defaults lookup to return alt_val
                # for this parent, then re-run the test cycle.
                orig_get = defaults.get
                def patched_get(label, section="", _alt=alt_val, _name=parent_l0.name):
                    if label == _name:
                        return _alt
                    return orig_get(label, section)
                defaults.get = patched_get  # type: ignore[method-assign]

                try:
                    test_gc = per_test_scope(parent=ctx.guardrails)
                    new_result = await self._run_test_cycle(tc, ctx, test_gc)
                except GuardrailExit:
                    break
                finally:
                    defaults.get = orig_get  # type: ignore[method-assign]

                if new_result.status != TestStatus.BLOCKED:
                    # Recovered! Replace the old BLOCKED result.
                    old_status = result.status
                    results[idx] = new_result
                    results[idx].notes = (new_result.notes or "") + f" | audit_parent={parent_l0.name!r}={alt_val!r}"
                    # Update summary: remove old BLOCKED, add new status.
                    summary.blocked -= 1
                    if new_result.status == TestStatus.PASS:
                        summary.passed += 1
                    elif new_result.status == TestStatus.FAIL:
                        summary.failed += 1
                    else:
                        summary.skipped += 1
                    recovered += 1
                    print(f"    ✓ recovered: {tc.tc_id} BLOCKED → "
                          f"{new_result.status.value.upper()} via "
                          f"{parent_l0.name!r}={alt_val!r}")
                    _save_results_checkpoint(
                        results_path, results, summary,
                        app_name=ctx.app_name, model=self.model,
                        cost_usd=ctx.budget.current_cost,
                        duration_sec=time.time() - run_start,
                    )
                    break
                else:
                    print(f"    ⚠ alt {alt_val!r} still BLOCKED — trying next")

        return recovered

    # ── Per-test dispatch ──────────────────────────────────────────

    async def _run_test_cycle(
        self,
        tc: TestCase,
        ctx: ExecuteRunContext,
        test_gc: GuardrailContext,
    ) -> TestResult:
        """Run one test through the setup→test→restore cycle.
        Dispatches to the approach-specific handler."""
        # SKIP short-circuits: record immediately, no browser touch.
        if tc.approach == TestApproach.SKIP:
            return _blocked_or_skipped(tc, TestStatus.SKIP, notes="marked SKIP by plan")

        # Defensive: ensure no residual popup from a prior test is still
        # visible. Idempotent — no-op if nothing is open.
        await _close_popup(ctx.adapter)

        # Supported approaches:
        if tc.approach == TestApproach.FILL_CHECK:
            return await self._fill_check(tc, ctx, test_gc)
        if tc.approach == TestApproach.VERIFY_ONLY:
            return await self._verify_only(tc, ctx, test_gc)
        if tc.approach == TestApproach.TAP_VERIFY:
            return await self._tap_verify(tc, ctx, test_gc)
        if tc.approach == TestApproach.SELECT_AND_VERIFY:
            return await self._select_and_verify(tc, ctx, test_gc)
        if tc.approach == TestApproach.UPLOAD_FILE:
            return await self._upload_file(tc, ctx, test_gc)

        return _blocked_or_skipped(
            tc, TestStatus.BLOCKED,
            notes=f"unknown approach {tc.approach.value}",
        )

    # ── FILL_CHECK ────────────────────────────────────────────────
    #
    # 1. setup: (no prerequisites for simple fields in this wall)
    # 2. test: fill target with test_value via CSS selector
    # 3. observe: take post-fill snapshot
    # 4. classify: LLM sub-task → PASS/FAIL + error_text
    # 5. verify: CoVe check error_text is actually in snapshot
    # 6. restore: fill target with defaults.get(field_name)

    async def _fill_check(
        self,
        tc: TestCase,
        ctx: ExecuteRunContext,
        test_gc: GuardrailContext,
    ) -> TestResult:
        t0 = time.time()
        # Try all locators in confidence order, falling through to the next
        # if the current one fails. SPAs like Salesforce Lightning regenerate
        # id attributes between extract and execute, so a stale id-based CSS
        # is common — the label-based XPath fallbacks generated during
        # extract save us from BLOCKED.
        locators = _find_l1_locators_sorted(ctx.knowledge, tc.element_id)
        if not locators:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"no locator in KB for element_id={tc.element_id}",
            )

        adapter = ctx.adapter

        # Pre-snapshot for CoVe verification below.
        pre_snap = await _raw_snapshot(adapter)

        # Python fills via React-safe native setter. If all locators fail,
        # mark BLOCKED with a note of what was tried.
        fill_ok, strat_used, sel_used = await _fill_trying_all_locators(
            adapter, locators, tc.test_value
        )
        if not fill_ok:
            tried = ", ".join(f"{s}={v!r}" for s, v in locators[:3])
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"fill failed on all {len(locators)} locator(s); tried: {tried}",
            )
        # Keep a note of which locator actually worked — useful for
        # debugging SPAs where the id-based CSS went stale.
        css = sel_used  # variable retained below for downstream references

        # Small settle for validation to render.
        await asyncio.sleep(0.6)
        post_snap = await _raw_snapshot(adapter)

        # LLM classifies the outcome — one narrow call, not an agent loop.
        classification = await _classify_test_result(
            tc=tc,
            pre_snapshot=pre_snap,
            post_snapshot=post_snap,
            budget=ctx.budget,
            guardrails=test_gc,
            model=self.model,
        )

        # CoVe: if the LLM claimed an error_text, verify it literally
        # appears in the post-snapshot. Unsupported claim → flag but
        # don't overwrite the classification (LLM may be right).
        err = (classification.get("error_text") or "").strip()
        if err:
            verify = verify_string_fields(
                classification,
                ["error_text"],
                post_snap,
                label=f"{tc.tc_id}_err",
                on_unsupported="flag",
                log=False,
            )
            if verify.dropped:
                classification["_verify_warning"] = (
                    f"LLM claimed error_text={err!r} not found in snapshot"
                )

        # Wall 1.3 — Python enforces valid state post-test. Restore the
        # target field to a KB default so the form stays submittable for
        # any subsequent navigation / test. Outcome is tracked so missing
        # defaults and silent fill failures show up in the report.
        restore = await _restore_field_to_default(
            adapter, css, tc, ctx.defaults,
        )

        return _result_from_classification(
            tc, classification,
            actual=classification.get("observed", ""),
            duration_ms=int((time.time() - t0) * 1000),
            restore=restore,
        )

    # ── VERIFY_ONLY ───────────────────────────────────────────────
    # Just check element exists + looks reasonable. No fill, no restore.

    async def _verify_only(
        self,
        tc: TestCase,
        ctx: ExecuteRunContext,
        test_gc: GuardrailContext,
    ) -> TestResult:
        t0 = time.time()
        css = _find_l1_css_selector(ctx.knowledge, tc.element_id)
        if not css:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"no CSS locator for element_id={tc.element_id}",
            )
        snap = await _raw_snapshot(ctx.adapter)
        # Cheap check: does the field name appear in the snapshot?
        # Good enough for MVP — sophisticated existence checks are a
        # follow-up if real needs emerge.
        status = (
            TestStatus.PASS
            if tc.field_name.lower() in (snap or "").lower()
            else TestStatus.FAIL
        )
        return TestResult(
            tc_id=tc.tc_id,
            element_id=tc.element_id,
            field_name=tc.field_name,
            test_value=tc.test_value,
            expected=tc.expected_result,
            actual=f"field {'visible' if status == TestStatus.PASS else 'not found'} in snapshot",
            status=status,
            notes="verify_only (snapshot text search)",
            duration_ms=int((time.time() - t0) * 1000),
        )

    # ── TAP_VERIFY ────────────────────────────────────────────────
    # Click element, observe response. Uses same classification path
    # as FILL_CHECK but without filling first.

    async def _tap_verify(
        self,
        tc: TestCase,
        ctx: ExecuteRunContext,
        test_gc: GuardrailContext,
    ) -> TestResult:
        t0 = time.time()
        css = _find_l1_css_selector(ctx.knowledge, tc.element_id)
        if not css:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"no CSS locator for element_id={tc.element_id}",
            )
        adapter = ctx.adapter
        pre_snap = await _raw_snapshot(adapter)

        # Click via JS (we already know this pattern works for React/MUI
        # buttons — native-click is reliable for elements we've targeted
        # by CSS selector).
        tap_js = (
            "() => {"
            f"  const el = document.querySelector({json.dumps(css)});"
            "  if (!el) return 'NOT_FOUND';"
            "  el.click();"
            "  return 'OK';"
            "}"
        )
        result = await adapter.evaluate_script(tap_js)
        if "NOT_FOUND" in (result or ""):
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"tap target not found by CSS={css!r}",
            )
        await asyncio.sleep(0.5)
        post_snap = await _raw_snapshot(adapter)

        classification = await _classify_test_result(
            tc=tc,
            pre_snapshot=pre_snap,
            post_snapshot=post_snap,
            budget=ctx.budget,
            guardrails=test_gc,
            model=self.model,
        )
        return _result_from_classification(
            tc, classification,
            actual=classification.get("observed", ""),
            duration_ms=int((time.time() - t0) * 1000),
        )

    # ── SELECT_AND_VERIFY ─────────────────────────────────────────
    #
    # Wall 1.1 — dependent dropdown chaining. Flow:
    #   1. Resolve parent deps (L0.depends_on ∪ defaults sidecar)
    #   2. For each parent: fill/select to KB default
    #   3. Click child dropdown → enumerate options → click target
    #   4. Verify: post-snapshot contains the chosen option text
    #
    # Deterministic: no LLM call in the happy path. PASS iff
    # (select returned SELECTED) AND (option text appears in post-snap).

    async def _select_and_verify(
        self,
        tc: TestCase,
        ctx: ExecuteRunContext,
        test_gc: GuardrailContext,
    ) -> TestResult:
        t0 = time.time()
        setup_notes: list[str] = []

        # 1) Dependencies — L0.depends_on first, defaults sidecar as fallback.
        l0 = ctx.knowledge.get_l0_for_element(tc.element_id)
        deps = list(l0.depends_on) if (l0 and l0.depends_on) else []
        if not deps:
            deps = ctx.defaults.get_dependencies(tc.element_id)

        for parent_id in deps:
            ok, note = await _set_parent_value(
                ctx.adapter, ctx.knowledge, ctx.defaults, parent_id,
            )
            setup_notes.append(note)
            if not ok:
                print(f"    [select] ⚠ setup failed: {note}")
                return TestResult(
                    tc_id=tc.tc_id,
                    element_id=tc.element_id,
                    field_name=tc.field_name,
                    test_value=tc.test_value,
                    expected=tc.expected_result,
                    actual="",
                    status=TestStatus.BLOCKED,
                    notes=" | ".join(["dep_failed", *setup_notes]),
                    duration_ms=int((time.time() - t0) * 1000),
                )
            await asyncio.sleep(0.4)

        # 2) Child dropdown — try all locators in confidence order until one
        # finds the trigger. Same fall-through pattern as _fill_check so
        # SPAs with regenerated id attributes (Salesforce Lightning, Odoo)
        # reach the label-based XPath fallback instead of BLOCKING.
        locators = _find_l1_locators_sorted(ctx.knowledge, tc.element_id)
        if not locators:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"no locator in KB for element_id={tc.element_id}",
            )

        strategy, locator_value = "", ""
        status, options = "ELEMENT_NOT_FOUND", []
        for s, v in locators:
            status, options = await _select_option(
                ctx.adapter, s, v, tc.test_value,
            )
            # ELEMENT_NOT_FOUND is the only status that means "wrong
            # locator" — keep trying. Any other status means we reached
            # the element (and success/failure is about the option pick).
            if status != "ELEMENT_NOT_FOUND":
                strategy, locator_value = s, v
                break
        if not strategy:
            # All locators returned ELEMENT_NOT_FOUND. Treat last attempt's
            # locator as the one we report.
            strategy, locator_value = locators[-1]

        print(f"    [select] {tc.tc_id} {tc.test_value!r} → {status} "
              f"(via {strategy}, saw {len(options)} options)")

        # 3) CoVe-style deterministic verify — selection should be visible
        # in the post-snapshot (MUI renders the chosen label in the trigger).
        post_snap = await _raw_snapshot(ctx.adapter)
        selection_visible = (tc.test_value or "").lower() in (post_snap or "").lower()

        # 4) Decide outcome.
        test_status: TestStatus
        actual: str
        notes_parts: list[str] = []
        if setup_notes:
            notes_parts.append("setup=" + "; ".join(setup_notes))
        notes_parts.append(f"options_seen={len(options)}")

        if status == "SELECTED" and selection_visible:
            test_status = TestStatus.PASS
            actual = f"selected {tc.test_value!r}; trigger shows selected label"
        elif status == "SELECTED" and not selection_visible:
            # Selection clicked but the UI didn't commit it — likely a
            # framework event-binding gap we need to know about.
            test_status = TestStatus.FAIL
            actual = f"option clicked but {tc.test_value!r} not in post-snapshot"
            notes_parts.append("cove=selection_not_in_snapshot")
        elif status == "OPTION_NOT_FOUND":
            test_status = TestStatus.FAIL
            actual = f"option {tc.test_value!r} not in dropdown"
            notes_parts.append(f"options_sample={options[:5]}")
        elif status == "ELEMENT_NOT_FOUND":
            test_status = TestStatus.BLOCKED
            actual = f"dropdown trigger not found via {strategy}={locator_value!r}"
        else:
            test_status = TestStatus.BLOCKED
            actual = f"select failed: {status}"

        return TestResult(
            tc_id=tc.tc_id,
            element_id=tc.element_id,
            field_name=tc.field_name,
            test_value=tc.test_value,
            expected=tc.expected_result,
            actual=actual,
            status=test_status,
            notes=" | ".join(notes_parts),
            evidence={
                "select_status": status,
                "options_seen": options,
                "dependencies_applied": deps,
                "selection_visible_in_snapshot": selection_visible,
            },
            duration_ms=int((time.time() - t0) * 1000),
        )

    # ── UPLOAD_FILE (Wall 1.4) ─────────────────────────────────────
    #
    # Full OCR-gated upload flow: attach the file, click the confirm
    # button if present, poll for OCR completion, classify PASS/FAIL.
    # Reuses the existing `_upload_file_for_field_impl` (web_tools) which
    # already handles trigger click → modal cascade → hidden-input exposure
    # → MCP upload → wait-for-verification.
    #
    # Status mapping from the impl's JSON response:
    #   PASS      → TestStatus.PASS   (file uploaded + OCR success signal)
    #   FAIL      → TestStatus.FAIL   (uploaded but no success signal)
    #   ERROR     → TestStatus.BLOCKED (could not start upload — missing
    #               file, L0 mismatch, trigger not found)
    #   anything else → TestStatus.BLOCKED (defensive)
    #
    # test_value optionally carries an explicit file name; if empty, the
    # impl's token-overlap resolver picks the best match from the app's
    # test_files folder.

    async def _upload_file(
        self,
        tc: TestCase,
        ctx: ExecuteRunContext,
        test_gc: GuardrailContext,
    ) -> TestResult:
        from qa.tools.web_tools import _upload_file_for_field_impl

        t0 = time.time()
        try:
            raw = await _upload_file_for_field_impl(
                field_name=tc.field_name,
                file_name=tc.test_value or "",
                wait_for_ocr=True,
            )
        except Exception as e:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"upload impl raised: {type(e).__name__}: {e}",
            )

        parsed: dict = {}
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, TypeError):
            parsed = {}

        impl_status = str(parsed.get("status", "")).upper()

        if impl_status == "PASS":
            test_status = TestStatus.PASS
            actual = (
                f"uploaded {parsed.get('file_uploaded', '')!r}; "
                f"signal={parsed.get('success_signal', '')!r}"
            )
        elif impl_status == "FAIL":
            test_status = TestStatus.FAIL
            actual = (
                f"uploaded but no success signal "
                f"({parsed.get('success_signal', 'none')})"
            )
        elif impl_status == "ERROR":
            test_status = TestStatus.BLOCKED
            actual = f"upload error: {parsed.get('reason', 'unknown')}"
        else:
            test_status = TestStatus.BLOCKED
            actual = f"unexpected impl status={impl_status!r}"

        notes = f"upload_file impl_status={impl_status or 'none'}"

        return TestResult(
            tc_id=tc.tc_id,
            element_id=tc.element_id,
            field_name=tc.field_name,
            test_value=tc.test_value,
            expected=tc.expected_result,
            actual=actual,
            status=test_status,
            notes=notes,
            evidence={
                "impl_status": impl_status,
                "file_uploaded": parsed.get("file_uploaded", ""),
                "success_signal": parsed.get("success_signal", ""),
                "reason": parsed.get("reason", ""),
            },
            duration_ms=int((time.time() - t0) * 1000),
        )


# ──────────────────────────────────────────────────────────────────────
# Helpers — module level so they're straightforward to unit-test.
# ──────────────────────────────────────────────────────────────────────

async def _raw_snapshot(adapter: PlatformAdapter) -> str:
    """Unfiltered snapshot via the MCP server directly. We want the full
    a11y tree (with options, error text, etc.) for classification."""
    server = adapter.get_mcp_server()
    result = await server.call_tool("take_snapshot", {})
    if result.content:
        return result.content[0].text or ""
    return ""


def _js_resolve_expr(strategy: str, value: str) -> str:
    """Return a JS expression (as a string) that resolves to the target
    element for the given locator strategy — `document.querySelector` for
    CSS, `document.evaluate` for XPath. Accepts either strategy so the
    rest of _select_option's JS blocks are strategy-agnostic."""
    if strategy == "xpath":
        xp = json.dumps(value)
        return (
            f"document.evaluate({xp}, document, null, "
            f"XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue"
        )
    # Default: CSS (works for css strategy AND for legacy callers that
    # only have a CSS string handy).
    return f"document.querySelector({json.dumps(value)})"


async def _select_option(
    adapter: PlatformAdapter,
    strategy_or_css: str,
    value_or_option: str,
    option_text: str | None = None,
) -> tuple[str, list[str]]:
    """Wall 1.1 — deterministic dropdown select.

    Two call shapes supported for backward compat:
      • 3-arg (new): _select_option(adapter, strategy, value, option_text)
        where strategy is "css" or "xpath".
      • 2-arg (legacy): _select_option(adapter, css, option_text) — CSS-only.

    Handles both native <select> and MUI-style custom comboboxes. Returns
    (status, options_seen) where status is one of:
        • "SELECTED"          — option clicked, selection committed
        • "OPTION_NOT_FOUND"  — dropdown opened but target option not present
        • "ELEMENT_NOT_FOUND" — trigger locator matched nothing
        • "OPEN_FAILED"       — click on trigger yielded no popup / no options
    """
    # Resolve the two calling conventions into (strategy, value, option_text)
    if option_text is None:
        # Legacy 2-arg: (adapter, css, option_text)
        strategy = "css"
        value = strategy_or_css
        option_text = value_or_option
    else:
        strategy = strategy_or_css
        value = value_or_option
    if strategy not in ("css", "xpath"):
        strategy = "css"

    resolve_expr = _js_resolve_expr(strategy, value)

    # Detect native <select> vs custom combobox.
    detect_fn = (
        "() => {"
        f"  const el = {resolve_expr};"
        "  if (!el) return JSON.stringify({kind: 'none'});"
        "  if (el.tagName === 'SELECT') {"
        "    const opts = [...el.options].map(o => o.textContent.trim()).filter(t => t);"
        "    return JSON.stringify({kind: 'native', options: opts});"
        "  }"
        "  return JSON.stringify({kind: 'custom'});"
        "}"
    )
    raw = await adapter.evaluate_script(detect_fn)
    # Use the same Chrome-envelope-aware parser as the rest of the
    # extract/tools side. _strip_json_wrapper only understood plain
    # `result:`/`Function call returned:` prefixes — Chrome DevTools MCP
    # actually returns `Script ran on page and returned:\n```json\n...`
    # which caused every dropdown to fail ELEMENT_NOT_FOUND.
    detect = _parse_mcp_result(raw)
    if not isinstance(detect, dict):
        return ("ELEMENT_NOT_FOUND", [])
    if detect.get("kind") == "none":
        return ("ELEMENT_NOT_FOUND", [])

    if detect.get("kind") == "native":
        options = detect.get("options") or []
        select_fn = (
            "() => {"
            f"  const el = {resolve_expr};"
            f"  const needle = {json.dumps(option_text.lower())};"
            "  const target = [...el.options].find(o => "
            "    o.textContent.trim().toLowerCase().includes(needle));"
            "  if (!target) return 'OPTION_NOT_FOUND';"
            "  el.value = target.value;"
            "  el.dispatchEvent(new Event('change', {bubbles: true}));"
            "  return 'SELECTED';"
            "}"
        )
        result = (await adapter.evaluate_script(select_fn) or "").strip()
        if "OPTION_NOT_FOUND" in result:
            return ("OPTION_NOT_FOUND", options)
        if "SELECTED" in result:
            return ("SELECTED", options)
        return ("OPEN_FAILED", options)

    # Custom combobox path — click trigger, wait, read options, click target.
    open_fn = (
        "() => {"
        f"  const el = {resolve_expr};"
        "  if (!el) return 'ELEMENT_NOT_FOUND';"
        "  el.click();"
        "  return 'OPENED';"
        "}"
    )
    opened = (await adapter.evaluate_script(open_fn) or "").strip()
    if "ELEMENT_NOT_FOUND" in opened:
        return ("ELEMENT_NOT_FOUND", [])

    await asyncio.sleep(0.5)

    # Option-element selector list covering frameworks we've seen. This
    # is a stop-gap per-framework match list; the proper fix is to have
    # Extract capture the option selector per app and persist it to L1
    # (tracked as follow-up wall). Selectors ordered from most-specific
    # to most-generic; first match wins.
    options_sel = (
        # TECU / Tailwind custom dropdowns
        '[data-testid="dropdown-menu"] li[data-testid^="option-"], '
        'li[data-testid^="option-"], '
        # ARIA standard
        '[role=option], [role=menuitem], li[role=listitem], '
        # MUI
        '.MuiMenuItem-root, '
        # Bootstrap
        '.dropdown-item, '
        # Generic fallback: visible <li> inside any popup container
        '[role=listbox] li, [data-testid*="menu"] li, [data-testid*="dropdown"] li'
    )
    options_sel_js = json.dumps(options_sel)

    read_fn = (
        "() => {"
        f"  const opts = [...document.querySelectorAll({options_sel_js})]"
        "    .filter(e => e.offsetParent !== null);"
        "  return JSON.stringify(opts.map(o => o.textContent.trim()).filter(t => t));"
        "}"
    )
    raw_opts = await adapter.evaluate_script(read_fn)
    parsed = _parse_mcp_result(raw_opts)
    options = parsed if isinstance(parsed, list) else []
    if not options:
        await _close_popup(adapter)
        return ("OPEN_FAILED", [])

    click_fn = (
        "() => {"
        f"  const needle = {json.dumps(option_text.lower())};"
        f"  const match = [...document.querySelectorAll({options_sel_js})]"
        "    .filter(e => e.offsetParent !== null)"
        "    .find(e => e.textContent.trim().toLowerCase().includes(needle));"
        "  if (!match) return 'OPTION_NOT_FOUND';"
        "  match.click();"
        "  return 'SELECTED';"
        "}"
    )
    result = (await adapter.evaluate_script(click_fn) or "").strip()
    if "OPTION_NOT_FOUND" in result:
        await _close_popup(adapter)
        return ("OPTION_NOT_FOUND", options)
    await asyncio.sleep(0.3)
    return ("SELECTED", options)


async def _close_popup(adapter: PlatformAdapter) -> None:
    """Close any open dropdown popup — Escape keydown first (works for
    MUI, Radix, most ARIA listboxes), then backdrop-click fallback for
    custom popups that don't handle Escape. Safe to call even when no
    popup is open."""
    close_js = (
        "() => {"
        "  document.dispatchEvent(new KeyboardEvent('keydown', "
        "    {key: 'Escape', code: 'Escape', keyCode: 27, which: 27, "
        "     bubbles: true, cancelable: true}));"
        "  document.dispatchEvent(new KeyboardEvent('keyup', "
        "    {key: 'Escape', code: 'Escape', keyCode: 27, which: 27, "
        "     bubbles: true, cancelable: true}));"
        # Click a neutral point outside any visible popup container so
        # backdrop-triggered close paths also fire. Guard against clicking
        # on an element inside the popup itself.
        "  const menus = [...document.querySelectorAll("
        "    '[data-testid=\"dropdown-menu\"], [role=listbox], "
        ".MuiPopover-root, [data-testid*=\"menu\"]')];"
        "  const outside = document.elementFromPoint(5, 5);"
        "  if (outside && !menus.some(m => m.contains(outside) || m === outside)) {"
        "    outside.click();"
        "  }"
        "  return 'CLOSED';"
        "}"
    )
    try:
        await adapter.evaluate_script(close_js)
    except Exception:
        pass
    await asyncio.sleep(0.2)


def _strip_json_wrapper(raw: str | None) -> str:
    """Chrome DevTools MCP sometimes wraps evaluate_script results in a
    thin envelope like `"result": "..."` — accept raw JSON payloads or
    passthrough strings."""
    if not raw:
        return "{}"
    s = raw.strip()
    # Strip a leading `Function call returned: ` / `result:` if present.
    for prefix in ("result:", "Function call returned:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    return s


async def _set_parent_value(
    adapter: PlatformAdapter,
    kb: KnowledgeBase,
    defaults: Defaults,
    parent_element_id: str,
) -> tuple[bool, str]:
    """Wall 1.1 — fill one dependency parent so the child becomes testable.

    Dispatches by parent type: text_input → native-setter fill,
    dropdown → _select_option. Returns (ok, note).
    """
    parent_l0 = kb.get_l0_for_element(parent_element_id)
    if parent_l0 is None:
        return (False, f"parent {parent_element_id} not in KB")

    value = defaults.get(parent_l0.name, section=parent_l0.screen_name)
    if not value:
        return (
            False,
            f"no default for parent {parent_l0.name!r} "
            f"(add to artifacts/defaults/<app>.json)",
        )

    locators = _find_l1_locators_sorted(kb, parent_element_id)
    if not locators:
        return (False, f"no locator in KB for parent {parent_element_id}")

    parent_type = parent_l0.type.value if hasattr(parent_l0.type, "value") else str(parent_l0.type)
    if parent_type == "dropdown":
        # Try each locator in order. Fall through only on ELEMENT_NOT_FOUND —
        # any other status means we reached the element, success/failure is
        # about the option pick.
        last_status = "ELEMENT_NOT_FOUND"
        for s, v in locators:
            status, _ = await _select_option(adapter, s, v, value)
            last_status = status
            if status == "SELECTED":
                return (True, f"parent {parent_l0.name!r}={value!r} selected")
            if status != "ELEMENT_NOT_FOUND":
                break
        return (False, f"parent select {last_status} on {parent_l0.name!r}")

    # Text-input style parents (and any other fill-compatible type). Uses
    # the same fall-through helper as _fill_check — CSS primary, XPath
    # fallback, so SPA id-regeneration doesn't block cascade setup.
    ok, used_strategy, used_locator = await _fill_trying_all_locators(
        adapter, locators, value
    )
    if ok:
        return (True, f"parent {parent_l0.name!r}={value!r} filled")
    return (False, f"parent fill failed on all {len(locators)} locator(s)")


async def _restore_field_to_default(
    adapter: PlatformAdapter,
    css: str,
    tc: TestCase,
    defaults: Defaults,
) -> RestoreOutcome:
    """Wall 1.3: re-fill target field with its KB default so the form stays
    submittable for the next test. Returns an explicit outcome — missing
    defaults and fill failures are surfaced, not silently ignored."""
    default_value = defaults.get(tc.field_name, section=tc.screen_name)
    if not default_value:
        warn = f"no default set for {tc.field_name!r} (add to artifacts/defaults/<app>.json)"
        print(f"    [restore] ⚠ {warn}")
        return RestoreOutcome(status="no_default", warning=warn)

    fill_ok = await _python_fill(adapter, css, default_value)
    if not fill_ok:
        warn = f"native-setter fill returned non-OK on CSS={css!r}"
        print(f"    [restore] ⚠ {warn}")
        return RestoreOutcome(
            status="fill_failed", value=default_value, warning=warn,
        )

    await asyncio.sleep(0.3)
    print(f"    [restore] ✓ {tc.field_name!r} restored to {default_value!r}")
    return RestoreOutcome(status="restored", value=default_value)


async def _python_fill(adapter: PlatformAdapter, css: str, value: str) -> bool:
    """React-safe native-setter fill via CSS selector. Returns True on 'OK'."""
    fn = (
        "() => {"
        f"  const el = document.querySelector({json.dumps(css)});"
        "  if (!el) return 'ELEMENT_NOT_FOUND';"
        "  const proto = el.tagName === 'TEXTAREA' "
        "    ? HTMLTextAreaElement.prototype "
        "    : HTMLInputElement.prototype;"
        "  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
        "  el.focus();"
        f"  setter.call(el, {json.dumps(value)});"
        "  el.dispatchEvent(new Event('input', {bubbles: true}));"
        "  el.dispatchEvent(new Event('change', {bubbles: true}));"
        "  el.blur();"
        "  return 'OK';"
        "}"
    )
    try:
        result = await adapter.evaluate_script(fn)
    except Exception:
        return False
    return "OK" in (result or "")


async def _python_fill_xpath(adapter: PlatformAdapter, xpath: str, value: str) -> bool:
    """Same React-safe native-setter fill, but resolves the element via XPath.
    Used for label-based locators that can't be expressed in CSS."""
    fn = (
        "() => {"
        f"  const result = document.evaluate({json.dumps(xpath)}, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);"
        "  const el = result.singleNodeValue;"
        "  if (!el) return 'ELEMENT_NOT_FOUND';"
        "  const proto = el.tagName === 'TEXTAREA' "
        "    ? HTMLTextAreaElement.prototype "
        "    : HTMLInputElement.prototype;"
        "  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
        "  el.focus();"
        f"  setter.call(el, {json.dumps(value)});"
        "  el.dispatchEvent(new Event('input', {bubbles: true}));"
        "  el.dispatchEvent(new Event('change', {bubbles: true}));"
        "  el.blur();"
        "  return 'OK';"
        "}"
    )
    try:
        result = await adapter.evaluate_script(fn)
    except Exception:
        return False
    return "OK" in (result or "")


async def _fill_trying_all_locators(
    adapter: PlatformAdapter,
    locators: list[tuple[str, str]],
    value: str,
) -> tuple[bool, str, str]:
    """Try each (strategy, value) locator in order until one succeeds.
    Returns (ok, strategy_used, selector_used). Used to gracefully fall
    through from stale id-based CSS to label-based XPath when SPAs
    regenerate IDs between extract and execute."""
    for strategy, sel in locators:
        if not sel:
            continue
        if strategy == "css":
            ok = await _python_fill(adapter, sel, value)
        elif strategy == "xpath":
            ok = await _python_fill_xpath(adapter, sel, value)
        else:
            continue
        if ok:
            return (True, strategy, sel)
    return (False, "", "")


def _find_l1_locators_sorted(
    kb: KnowledgeBase, element_id: str
) -> list[tuple[str, str]]:
    """Return ALL L1 locators for an element, sorted by confidence descending.
    Callers can iterate and try each when the primary locator fails (SPA
    id regeneration, stale selectors). Tries CSS before XPath at equal
    confidence — CSS is cheaper to evaluate."""
    l1 = kb.get_l1_for_element(element_id)
    if l1 is None:
        return []
    ordered = sorted(
        l1.locators,
        key=lambda loc: (-loc.confidence, 0 if loc.strategy == "css" else 1),
    )
    return [(loc.strategy, loc.value) for loc in ordered if loc.strategy in ("css", "xpath")]


async def _classify_test_result(
    tc: TestCase,
    pre_snapshot: str,
    post_snapshot: str,
    budget: BudgetTracker,
    guardrails: GuardrailContext,
    model: str,
) -> dict:
    """One narrow LLM call — observes test outcome from snapshots.
    Returns the parsed classification dict (status + observed + error_text)."""
    user = (
        f"field_name: {tc.field_name}\n"
        f"approach: {tc.approach.value}\n"
        f"test_value: {tc.test_value!r}\n"
        f"expected_result: {tc.expected_result!r}\n\n"
        f"PRE_SNAPSHOT:\n{(pre_snapshot or '')[:6000]}\n\n"
        f"POST_SNAPSHOT:\n{(post_snapshot or '')[:6000]}\n"
    )
    return await llm_classify(
        CLASSIFY_TEST_RESULT_PROMPT,
        user,
        CLASSIFY_TEST_RESULT_SCHEMA,
        model=model,
        budget=budget,
        label=f"test_{tc.tc_id}",
        guardrails=guardrails,
    )


def _result_from_classification(
    tc: TestCase,
    classification: dict,
    *,
    actual: str = "",
    duration_ms: int = 0,
    restore: RestoreOutcome | None = None,
) -> TestResult:
    status_str = (classification.get("status") or "blocked").lower()
    try:
        status = TestStatus(status_str)
    except ValueError:
        status = TestStatus.BLOCKED

    notes_parts = []
    if classification.get("confidence") is not None:
        notes_parts.append(f"conf={classification['confidence']:.2f}")
    if classification.get("error_text"):
        notes_parts.append(f"err={classification['error_text']!r}")
    if classification.get("_verify_warning"):
        notes_parts.append(f"cove_warn={classification['_verify_warning']}")
    if restore is not None:
        if restore.status == "restored":
            notes_parts.append(f"restored_to={restore.value!r}")
        elif restore.status == "no_default":
            notes_parts.append(f"restore=no_default")
        elif restore.status == "fill_failed":
            notes_parts.append(f"restore=fill_failed")
    notes = " | ".join(notes_parts)

    evidence: dict = {"classification": classification}
    if restore is not None:
        evidence["restore"] = {
            "status": restore.status,
            "value": restore.value,
            "warning": restore.warning,
        }

    return TestResult(
        tc_id=tc.tc_id,
        element_id=tc.element_id,
        field_name=tc.field_name,
        test_value=tc.test_value,
        expected=tc.expected_result,
        actual=actual,
        status=status,
        notes=notes,
        evidence=evidence,
        duration_ms=duration_ms,
    )


def _blocked_or_skipped(
    tc: TestCase, status: TestStatus, *, notes: str = "",
) -> TestResult:
    return TestResult(
        tc_id=tc.tc_id,
        element_id=tc.element_id,
        field_name=tc.field_name,
        test_value=tc.test_value,
        expected=tc.expected_result,
        actual="",
        status=status,
        notes=notes,
    )


def _update_summary(summary: TestSummary, status: TestStatus) -> None:
    if status == TestStatus.PASS:
        summary.passed += 1
    elif status == TestStatus.FAIL:
        summary.failed += 1
    elif status == TestStatus.SKIP:
        summary.skipped += 1
    else:
        summary.blocked += 1
