# qa/orchestrators/strategies.py — uniform handler interface for the
# autonomous strategist. Each strategy is an async function with the
# same signature; the registry maps a strategy name (returned by the
# PICK_STRATEGY LLM call) to its handler.
#
# Existing per-mode flows (wizard, gated) are wrapped here as one-step
# handlers — each invocation does ONE logical iteration (one page, one
# fill+advance cycle, one full multi-section run) and returns an
# outcome the runner uses to decide whether to continue or stop.

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from qa.adapters.protocol import PlatformAdapter
from qa.config import Defaults
from qa.engine.budget import BudgetTracker
from qa.models import KnowledgeBase
from qa.models.knowledge import ScreenKnowledge


@dataclass
class StrategyContext:
    """Everything a strategy handler needs to do its work. Created once
    by the runner per page-iteration and threaded through."""
    adapter: PlatformAdapter
    kb: KnowledgeBase
    defaults: Defaults
    budget: BudgetTracker
    app_name: str
    page_url: str
    section_num: int          # 1-indexed iteration counter
    model: str = "gpt-5.1"


@dataclass
class StrategyOutcome:
    """Result of one strategy invocation. The runner reads this to
    decide whether to continue, stop, or change course.

    advance: True → page changed, runner picks a strategy for the new
        page next iteration. False → stayed on the same page (validation
        rejected, no nav button, etc.); runner stops to avoid loops.
    captured: ScreenKnowledge produced by this strategy, if any. Already
        merged into kb.screens by the handler (each strategy is
        responsible for KB persistence so it can use its own cleanup
        logic, e.g. wizard runs the field-tagger).
    success: did the strategy complete without an internal error? False
        means something genuinely broke (locator not found, MCP timeout,
        etc.) and the runner should surface the error.
    note: short human-readable description of what happened, shown in
        the audit log + console.
    cost: USD spent inside this strategy invocation (LLM calls only;
        tool calls are free at the MCP layer)."""

    success: bool
    advance: bool
    captured: ScreenKnowledge | None = None
    note: str = ""
    cost: float = 0.0
    error: str = ""


StrategyHandler = Callable[[StrategyContext], Awaitable[StrategyOutcome]]


# ─── wizard_step ──────────────────────────────────────────────────────
#
# One iteration of the existing --wizard loop, refactored out of
# form_extract.py so the runner can call it directly. Steps:
#   1. extract_form on the current page
#   2. (if section_num > 1) tag NEW vs CARRYOVER fields, drop carryovers
#   3. save the (cleaned) screen to kb
#   4. fill_page_from_defaults
#   5. required-field precheck
#   6. click_save_and_continue
#   7. wait_for_page_transition
#   8. classify_transition with the LLM
#   9. on NEW_PAGE → wait_for_content_render, return advance=True
#      on SAME_PAGE_WITH_EXPANSION → return advance=True (next iteration
#        re-extracts in place — runner counts iterations, not pages)
#      on SAME_PAGE_WITH_ERROR → return advance=False with error note

async def wizard_step(ctx: StrategyContext) -> StrategyOutcome:
    """Execute one wizard iteration. Returns advance=True if the page
    transitioned (or expanded) so the runner picks a new strategy
    next; advance=False if validation rejected the click or no nav
    button was found."""
    from qa.knowledge.store import KnowledgeStore
    from qa.orchestrators.form_extract import extract_form
    from qa.orchestrators.wizard_steps import (
        DEFAULT_NAV_LABELS,
        classify_transition,
        click_save_and_continue,
        fill_page_from_defaults,
        page_signature,
        tag_fields_new_vs_carryover,
        wait_for_content_render,
        wait_for_page_transition,
    )
    from qa.tools.web_tools import _safe_parse

    adapter = ctx.adapter
    cost_at_start = ctx.budget.current_cost

    # ── 1. Resolve a screen_name (page title → main heading → fallback)
    title_raw = await adapter.evaluate_script("() => document.title")
    parsed = _safe_parse(title_raw)
    screen_name = (str(parsed) if parsed else "").strip()
    if not screen_name or screen_name.lower() in (
        "extracted form", "react app", "vue app", "loading",
    ):
        h_raw = await adapter.evaluate_script(
            "() => { const h = document.querySelector("
            "'h1, h2, [role=heading]'); "
            "return h ? (h.textContent || '').trim() : ''; }"
        )
        h_parsed = _safe_parse(h_raw)
        h_text = (str(h_parsed) if h_parsed else "").strip()
        if h_text:
            screen_name = h_text
    if not screen_name:
        screen_name = f"Page {ctx.section_num}"

    # ── 2. Extract
    screen = await extract_form(
        adapter=adapter,
        app_name=ctx.app_name,
        screen_name=screen_name,
        budget=ctx.budget,
        page_url=ctx.page_url,
        defaults=ctx.defaults,
    )
    if not screen.l0:
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=None,
            note=f"extract returned 0 elements for {screen_name!r}",
            cost=ctx.budget.current_cost - cost_at_start,
        )

    # ── 3. Tag NEW vs CARRYOVER (skip on first page — nothing to compare)
    if ctx.section_num > 1 and ctx.kb.screens:
        prior_names = [
            el.name for s in ctx.kb.screens for el in s.l0
            if s.screen_name != screen.screen_name and el.name
        ]
        current_names = [el.name for el in screen.l0]
        tags = await tag_fields_new_vs_carryover(
            adapter, current_names, prior_names, budget=ctx.budget,
        )
        carryovers = [n for n, t in tags.items() if t == "CARRYOVER"]
        if carryovers:
            keep = {n for n, t in tags.items() if t == "NEW"}
            screen.l0 = [el for el in screen.l0 if el.name in keep]
            keep_ids = {el.element_id for el in screen.l0}
            screen.l1 = [l1 for l1 in screen.l1 if l1.element_id in keep_ids]
            print(
                f"  [strat:wizard] dropped {len(carryovers)} carryover field(s) "
                f"from {screen.screen_name!r}"
            )

    # ── 4. Persist
    store = KnowledgeStore()
    existing = ctx.kb.get_screen(screen.screen_name)
    if existing:
        ctx.kb.screens = [
            s for s in ctx.kb.screens if s.screen_name != screen.screen_name
        ]
    ctx.kb.screens.append(screen)
    store.save(ctx.kb)

    # ── 5. Fill from defaults
    filled, skipped = await fill_page_from_defaults(
        adapter, ctx.kb, ctx.defaults, screen,
    )
    print(f"  [strat:wizard] filled {len(filled)} field(s), skipped {len(skipped)}")

    # ── 5b. Mid-fill reveal: filling cascade dropdowns / radios may
    # reveal new fields that weren't in the original L0. Re-extract.
    # If new fields appeared, fill them too. Bounded to one pass —
    # avoids runaway loops on pages where every fill reveals more.
    revealed_screen = await extract_form(
        adapter=adapter,
        app_name=ctx.app_name,
        screen_name=screen.screen_name,
        budget=ctx.budget,
        page_url=ctx.page_url,
        defaults=ctx.defaults,
    )
    prior_ids = {el.element_id for el in screen.l0}
    new_l0 = [el for el in revealed_screen.l0 if el.element_id not in prior_ids]
    if new_l0:
        print(
            f"  [strat:wizard] mid-fill reveal: {len(new_l0)} new field(s) "
            f"appeared after initial fill — extending"
        )
        # Build a synthetic screen containing only the new fields so
        # fill_page_from_defaults targets only them.
        from qa.models.knowledge import ScreenKnowledge
        delta = ScreenKnowledge(
            screen_name=screen.screen_name,
            screen_url=screen.screen_url,
            l0=new_l0,
            l1=[
                l1 for l1 in revealed_screen.l1
                if l1.element_id not in prior_ids
            ],
        )
        # Merge new L0/L1 into the persisted screen so KB reflects reality.
        screen.l0 = list(screen.l0) + new_l0
        screen.l1 = list(screen.l1) + delta.l1
        # Update kb in place (we already saved screen earlier).
        from qa.knowledge.store import KnowledgeStore
        existing = ctx.kb.get_screen(screen.screen_name)
        if existing:
            ctx.kb.screens = [
                s for s in ctx.kb.screens if s.screen_name != screen.screen_name
            ]
        ctx.kb.screens.append(screen)
        KnowledgeStore().save(ctx.kb)

        delta_filled, delta_skipped = await fill_page_from_defaults(
            adapter, ctx.kb, ctx.defaults, delta,
        )
        filled.extend(delta_filled)
        skipped.extend(delta_skipped)
        print(
            f"  [strat:wizard] reveal-fill: +{len(delta_filled)} filled, "
            f"+{len(delta_skipped)} skipped"
        )

    # ── 6. Required-field precheck
    # Autofilled / read-only fields are intentionally skipped during fill
    # (see _is_autofilled in wizard_steps), so they shouldn't be flagged
    # as misses here either — the app populates them itself.
    AUTOFILL_MARKERS_PRECHECK = (
        "auto_filled", "auto-filled", "autofilled",
        "read_only", "read-only", "readonly", "masked",
    )
    filled_names = {n for n, _ in filled}
    required_misses: list[tuple[str, str]] = []
    for el in screen.l0:
        if not getattr(el, "required", False):
            continue
        if el.name in filled_names:
            continue
        # Autofilled-required fields don't count as misses — the app
        # fills them itself and inspecting them as "missing" leads to
        # false stops on pages with OCR'd values.
        behavior = (getattr(el, "behavior", "") or "").lower()
        if any(m in behavior for m in AUTOFILL_MARKERS_PRECHECK):
            continue
        note = next(
            (n for fname, n in skipped if fname == el.name),
            "not filled (no fill report)",
        )
        required_misses.append((el.name, note))
    if required_misses:
        details = "; ".join(f"{n}: {note}" for n, note in required_misses[:3])
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=screen,
            note=f"{len(required_misses)} required field(s) failed to fill: {details}",
            cost=ctx.budget.current_cost - cost_at_start,
        )

    # ── 6.5. Wait for inline validation to settle before nav-clicking.
    # Apps with debounced server-side checks (email-uniqueness,
    # username availability) show a spinner for 1-5s after fill.
    # Clicking Save & Continue while it's still validating submits
    # stale state and gets rejected. 6s cap is a comfortable margin.
    from qa.orchestrators.wizard_steps import wait_for_inline_validation_settle
    settled, signal, vwait = await wait_for_inline_validation_settle(
        adapter, timeout=6.0,
    )
    if signal != "clean":
        print(f"  [strat:wizard] inline validation {signal} after {vwait:.1f}s")

    # ── 7. Capture transition baseline + click
    before_sig = await page_signature(adapter)
    before_snap = await adapter.raw_snapshot_text()
    clicked, label = await click_save_and_continue(adapter)
    if not clicked:
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=screen,
            note="no nav button found (Save & Continue / Next / Verify)",
            cost=ctx.budget.current_cost - cost_at_start,
        )
    print(f"  [strat:wizard] clicked {label!r}")

    # ── 8. Wait for transition (deterministic) + classify (LLM)
    transitioned, signal = await wait_for_page_transition(
        adapter, before_sig, timeout=12.0,
    )
    if not transitioned:
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=screen,
            note=f"no transition detected ({signal}) — likely a silent validation error",
            cost=ctx.budget.current_cost - cost_at_start,
        )

    verdict, reasoning, error_text = await classify_transition(
        adapter, before_snap, budget=ctx.budget,
    )
    print(f"  [strat:wizard] verdict: {verdict} — {reasoning}")

    if verdict == "SAME_PAGE_WITH_ERROR":
        msg = "validation error" + (f": {error_text}" if error_text else "")
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=screen,
            note=msg,
            cost=ctx.budget.current_cost - cost_at_start,
        )

    if verdict == "SAME_PAGE_WITH_EXPANSION":
        # Same page, content expanded (e.g. inline OTP block appeared).
        # We DID advance the form's state — return advance=True so the
        # runner picks again on the next iteration. The runner is
        # responsible for breaking infinite EXPANSION loops via its
        # own circuit breaker.
        await asyncio.sleep(1.0)
        return StrategyOutcome(
            success=True,
            advance=True,
            captured=screen,
            note=f"same page expanded — {reasoning}",
            cost=ctx.budget.current_cost - cost_at_start,
        )

    # NEW_PAGE — wait for the destination's content to render before
    # returning, so the runner's next snapshot is meaningful.
    rendered, n_interactive, wait_elapsed = await wait_for_content_render(
        adapter, min_interactive_elements=3, timeout=15.0,
    )
    if rendered:
        print(
            f"  [strat:wizard] next page rendered "
            f"({n_interactive} interactive elements after {wait_elapsed:.1f}s)"
        )
    else:
        print(
            f"  [strat:wizard] ⚠ next page only {n_interactive} interactive "
            f"element(s) after {wait_elapsed:.1f}s — proceeding anyway"
        )

    return StrategyOutcome(
        success=True,
        advance=True,
        captured=screen,
        note=f"NEW_PAGE — {reasoning}",
        cost=ctx.budget.current_cost - cost_at_start,
    )


# ─── gated_step ───────────────────────────────────────────────────────
#
# Wraps GatedMultiSectionFlow.run(). One invocation processes ALL
# sections of the current gated page (1.First ID → 2.Second ID →
# 3.Address Proof for TECU, etc.). After it returns, the runner picks
# again on the next page.

async def gated_step(ctx: StrategyContext) -> StrategyOutcome:
    """Run GatedMultiSectionFlow over the current gated page. Each
    section's screen is persisted into ctx.kb by the flow itself."""
    from qa.knowledge.file_resolver import discover_test_files
    from qa.knowledge.store import KnowledgeStore
    from qa.models import ExploreInput, Platform, TargetApp
    from qa.orchestrators.base import RunContext
    from qa.orchestrators.gated_multi_section import (
        GatedMultiSectionFlow,
        SectionFailed,
    )

    cost_at_start = ctx.budget.current_cost

    available = discover_test_files(ctx.app_name)
    if not available:
        return StrategyOutcome(
            success=False,
            advance=False,
            note=f"no test files in artifacts/test_files/{ctx.app_name.lower()}/ or global/",
            error="missing_test_files",
            cost=0.0,
        )

    # Build a thin RunContext for the gated flow. We reuse ctx.kb
    # (the gated flow appends to it), so this stays compatible with
    # the runner's KB lifecycle.
    app = TargetApp(
        platform=Platform.WEB, url=ctx.page_url, app_name=ctx.app_name,
    )
    inp = ExploreInput(app=app, model=ctx.model, budget=ctx.budget.max_budget)
    gated_ctx = RunContext(
        adapter=ctx.adapter,
        inp=inp,
        knowledge=ctx.kb,
        budget=ctx.budget,
        available_files=available,
    )

    flow = GatedMultiSectionFlow()
    try:
        captured = await flow.run(gated_ctx)
    except SectionFailed as e:
        return StrategyOutcome(
            success=False,
            advance=False,
            note=f"gated section failed: {e}",
            error=str(e),
            cost=ctx.budget.current_cost - cost_at_start,
        )
    except Exception as e:
        return StrategyOutcome(
            success=False,
            advance=False,
            note=f"gated flow raised {type(e).__name__}: {e}",
            error=f"{type(e).__name__}: {e}",
            cost=ctx.budget.current_cost - cost_at_start,
        )

    # Persist the kb update one more time defensively. The flow already
    # checkpointed each section, but a second save is cheap.
    KnowledgeStore().save(ctx.kb)

    last_screen = captured[-1] if captured else None
    return StrategyOutcome(
        success=True,
        advance=True,  # gated always advances the page state forward
        captured=last_screen,
        note=f"gated flow captured {len(captured)} section(s)",
        cost=ctx.budget.current_cost - cost_at_start,
    )


# ─── terminal / blocked / unknown — no-op handlers ───────────────────
#
# These aren't really "strategies"; they're decisions the runner needs
# to honour by stopping. The runner inspects the picked strategy name
# and stops without invoking these — but we register them as no-ops
# anyway so the registry is exhaustive over the LLM enum.

async def terminal_step(ctx: StrategyContext) -> StrategyOutcome:
    return StrategyOutcome(
        success=True, advance=False,
        note="terminal page reached", cost=0.0,
    )


async def blocked_step(ctx: StrategyContext) -> StrategyOutcome:
    return StrategyOutcome(
        success=False, advance=False,
        note="page is blocked (login / captcha / error)",
        error="blocked", cost=0.0,
    )


async def unknown_step(ctx: StrategyContext) -> StrategyOutcome:
    return StrategyOutcome(
        success=False, advance=False,
        note="page pattern not recognised — extend the strategy library",
        error="unknown_pattern", cost=0.0,
    )


# ─── Registry ─────────────────────────────────────────────────────────

STRATEGY_HANDLERS: dict[str, StrategyHandler] = {
    "wizard_step": wizard_step,
    "gated_step": gated_step,
    "terminal": terminal_step,
    "blocked": blocked_step,
    "unknown": unknown_step,
}
