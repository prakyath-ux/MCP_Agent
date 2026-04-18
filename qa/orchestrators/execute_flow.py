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


def _find_l1_css_selector(kb: KnowledgeBase, element_id: str) -> str:
    """Get the best-confidence CSS locator for an element. Returns ""
    if the element isn't in the KB or has no CSS locator."""
    l1 = kb.get_l1_for_element(element_id)
    if l1 is None:
        return ""
    css_locators = [loc for loc in l1.locators if loc.strategy == "css"]
    if not css_locators:
        return ""
    best = max(css_locators, key=lambda loc: loc.confidence)
    return best.value


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

        # Approaches deferred to later walls — record as BLOCKED with a
        # clear reason so the user sees exactly what's not yet supported.
        if tc.approach == TestApproach.SELECT_AND_VERIFY:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes="SELECT_AND_VERIFY handled by Wall 1.1 (dependent dropdown chaining)",
            )
        if tc.approach == TestApproach.UPLOAD_FILE:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes="UPLOAD_FILE handled by Wall 1.4 (OCR-gated replay)",
            )

        # Supported in Wall 0.1:
        if tc.approach == TestApproach.FILL_CHECK:
            return await self._fill_check(tc, ctx, test_gc)
        if tc.approach == TestApproach.VERIFY_ONLY:
            return await self._verify_only(tc, ctx, test_gc)
        if tc.approach == TestApproach.TAP_VERIFY:
            return await self._tap_verify(tc, ctx, test_gc)

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
        css = _find_l1_css_selector(ctx.knowledge, tc.element_id)
        if not css:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"no CSS locator in KB for element_id={tc.element_id}",
            )

        adapter = ctx.adapter

        # Pre-snapshot for CoVe verification below.
        pre_snap = await _raw_snapshot(adapter)

        # Python fills via React-safe native setter. If fill fails for
        # reasons we can see (selector bad, element not found), mark BLOCKED.
        fill_ok = await _python_fill(adapter, css, tc.test_value)
        if not fill_ok:
            return _blocked_or_skipped(
                tc, TestStatus.BLOCKED,
                notes=f"fill failed on CSS={css!r}",
            )

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

        # Restore target field to a valid default so the form stays
        # submittable for any subsequent navigation / test.
        default_value = ctx.defaults.get(tc.field_name, section=tc.screen_name)
        if default_value:
            await _python_fill(adapter, css, default_value)
            await asyncio.sleep(0.3)

        return _result_from_classification(
            tc, classification,
            actual=classification.get("observed", ""),
            duration_ms=int((time.time() - t0) * 1000),
            restored_to=default_value or "",
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


async def _python_fill(adapter: PlatformAdapter, css: str, value: str) -> bool:
    """React-safe native-setter fill. Returns True on 'OK'."""
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
    restored_to: str = "",
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
    if restored_to:
        notes_parts.append(f"restored_to={restored_to!r}")
    notes = " | ".join(notes_parts)

    return TestResult(
        tc_id=tc.tc_id,
        element_id=tc.element_id,
        field_name=tc.field_name,
        test_value=tc.test_value,
        expected=tc.expected_result,
        actual=actual,
        status=status,
        notes=notes,
        evidence={
            "classification": classification,
        },
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
