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
import json
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
    # The reveal-button LLM scan is OFF by default — in TECU testing it
    # misfired twice (clicked an internal marker, then a dropdown's
    # display label). Apps with genuine "+ Add Another" buttons can
    # enable it via --reveal-scan.
    reveal_scan: bool = False


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
        # ── 6.7. LLM retry with alternate values.
        # Before giving up, give each missed required field ONE retry
        # with an LLM-suggested alternate. The LLM reads the field's
        # name + type + validation_rules, the value we tried, what's
        # actually in the field now, and any error_text the page
        # surfaces. Returns a single alternate value to try.
        from qa.orchestrators.execute_flow import (
            _fill_trying_all_locators, _find_l1_locators_sorted,
        )
        from qa.orchestrators.wizard_steps import suggest_alternate_value

        # Pull error text from the page so the LLM has context.
        err_js = (
            "() => {"
            "  const errs = [...document.querySelectorAll("
            "    '.error, [role=alert], [class*=error], [class*=Error], '"
            "    + '.helper-text, .MuiFormHelperText-root.Mui-error, '"
            "    + '[aria-invalid=true] ~ * '"
            "  )].filter(e => e.offsetParent !== null);"
            "  return JSON.stringify({"
            "    errors: errs.map(e => (e.textContent||'').trim()).filter(t => t).slice(0, 8)"
            "  });"
            "}"
        )
        try:
            err_raw = await adapter.evaluate_script(err_js)
            err_parsed = _safe_parse(err_raw) or {}
            page_errors = err_parsed.get("errors", []) if isinstance(err_parsed, dict) else []
        except Exception:
            page_errors = []

        # Build a name → L0 lookup so we can resolve element type / rules
        l0_by_name = {el.name: el for el in screen.l0}

        retried_filled: list[tuple[str, str]] = []
        retry_failed: list[tuple[str, str]] = []
        for name, original_note in required_misses:
            el = l0_by_name.get(name)
            if el is None:
                retry_failed.append((name, original_note))
                continue
            tname = el.type.value if hasattr(el.type, "value") else str(el.type)
            attempted = ctx.defaults.get(name, section=el.screen_name) or ""

            # Read the field's actual current value (helpful for the LLM)
            actual_now = ""
            locators = _find_l1_locators_sorted(ctx.kb, el.element_id)
            for strategy, sel in locators:
                if strategy != "css" or not sel:
                    continue
                try:
                    val_js = (
                        "() => {"
                        f"  const e = document.querySelector({json.dumps(sel)});"
                        "  return JSON.stringify({v: e ? (e.value || '') : ''});"
                        "}"
                    )
                    val_raw = await adapter.evaluate_script(val_js)
                    val_parsed = _safe_parse(val_raw) or {}
                    if isinstance(val_parsed, dict):
                        actual_now = str(val_parsed.get("v", ""))
                        break
                except Exception:
                    pass

            err_text = " | ".join(page_errors)
            validation_rules = getattr(el, "validation_rules", "") or ""

            alt_value, alt_reason = await suggest_alternate_value(
                field_name=name,
                field_type=tname,
                attempted_value=attempted,
                actual_value=actual_now,
                error_text=err_text,
                validation_rules=validation_rules,
                budget=ctx.budget,
            )
            if not alt_value:
                retry_failed.append((name, f"{original_note}; LLM had no alternate ({alt_reason})"))
                continue

            print(
                f"  [strat:wizard] retry {name!r}: trying {alt_value!r} "
                f"({alt_reason[:80]})"
            )

            # Try the alternate. Only text-style fills here — dropdown
            # alternates would need re-enumeration which is rare for
            # required-field rejections.
            if tname not in ("text_input", "email", "phone", "url", "tel"):
                retry_failed.append((name, f"{original_note}; alt-retry only handles text-style fields, got {tname}"))
                continue
            ok, used_strategy, used_sel = await _fill_trying_all_locators(
                ctx.adapter, locators, alt_value,
            )
            if ok:
                retried_filled.append((name, f"alt={alt_value!r} ({used_strategy})"))
            else:
                retry_failed.append((name, f"{original_note}; alt {alt_value!r} also rejected"))

        if retried_filled:
            print(
                f"  [strat:wizard] retry recovered {len(retried_filled)} "
                f"required field(s):"
            )
            for n, note in retried_filled:
                print(f"  [strat:wizard]   ✓ {n}: {note}")
            filled.extend(retried_filled)

        # Recompute misses after retry
        retried_names = {n for n, _ in retried_filled}
        still_missing = [
            (n, note) for n, note in required_misses
            if n not in retried_names
        ]
        if still_missing:
            details = "; ".join(f"{n}: {note}" for n, note in still_missing[:3])
            return StrategyOutcome(
                success=True,
                advance=False,
                captured=screen,
                note=f"{len(still_missing)} required field(s) failed even after retry: {details}",
                cost=ctx.budget.current_cost - cost_at_start,
            )

    # ── 6.4. Reveal-on-click button scan (OPT-IN via --reveal-scan).
    # Disabled by default: in TECU testing it misclassified the Branch
    # dropdown's display text as a reveal button and broke Save & Continue
    # by re-opening the popup mid-submission. Apps with genuine "+ Add
    # Another" / "Show Advanced" buttons can opt in.
    from qa.orchestrators.wizard_steps import (
        find_and_click_reveal_buttons,
        wait_for_inline_validation_settle,
    )
    if ctx.reveal_scan:
        # Pass known field names so the helper rejects any LLM-suggested
        # label that just matches an existing L0 element on this page.
        known_names: set[str] = set()
        for el in screen.l0:
            if el.name:
                known_names.add(el.name)
            # Dropdown options also surface as button labels after
            # selection ("100 - TECU - MARABELLA BRANCH"). Add them
            # so the reveal scan can't pick them.
            for opt in (getattr(el, "options", None) or []):
                if isinstance(opt, str) and opt:
                    known_names.add(opt)
        reveal_clicked, reveal_skipped = await find_and_click_reveal_buttons(
            adapter, budget=ctx.budget, max_clicks=4,
            known_field_names=known_names,
        )
    else:
        reveal_clicked, reveal_skipped = ([], [])
    if reveal_clicked:
        print(
            f"  [strat:wizard] reveal scan clicked {len(reveal_clicked)} "
            f"button(s): {reveal_clicked}"
        )
        await asyncio.sleep(0.8)
        # Reveal clicks may have surfaced new fields — extend KB + fill them
        revealed2 = await extract_form(
            adapter=adapter,
            app_name=ctx.app_name,
            screen_name=screen.screen_name,
            budget=ctx.budget,
            page_url=ctx.page_url,
            defaults=ctx.defaults,
        )
        prior_ids2 = {el.element_id for el in screen.l0}
        new_l02 = [el for el in revealed2.l0 if el.element_id not in prior_ids2]
        if new_l02:
            print(
                f"  [strat:wizard] reveal-fill: {len(new_l02)} new field(s) "
                f"appeared after reveal-button clicks"
            )
            from qa.models.knowledge import ScreenKnowledge
            delta2 = ScreenKnowledge(
                screen_name=screen.screen_name,
                screen_url=screen.screen_url,
                l0=new_l02,
                l1=[
                    l1 for l1 in revealed2.l1
                    if l1.element_id not in prior_ids2
                ],
            )
            screen.l0 = list(screen.l0) + new_l02
            screen.l1 = list(screen.l1) + delta2.l1
            from qa.knowledge.store import KnowledgeStore
            existing2 = ctx.kb.get_screen(screen.screen_name)
            if existing2:
                ctx.kb.screens = [
                    s for s in ctx.kb.screens
                    if s.screen_name != screen.screen_name
                ]
            ctx.kb.screens.append(screen)
            KnowledgeStore().save(ctx.kb)
            d2_filled, d2_skipped = await fill_page_from_defaults(
                adapter, ctx.kb, ctx.defaults, delta2,
            )
            filled.extend(d2_filled)
            skipped.extend(d2_skipped)

    # ── 6.5. Wait for inline validation to settle before nav-clicking.
    # Apps with debounced server-side checks (email-uniqueness,
    # username availability) show a spinner for 1-5s after fill.
    # Clicking Save & Continue while it's still validating submits
    # stale state and gets rejected. 6s cap is a comfortable margin.
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

    # Wait for any post-click loading state to clear before asking the
    # LLM to classify. Otherwise the classifier sees a progress bar /
    # spinner and hallucinates SAME_PAGE_WITH_ERROR (it correctly notes
    # 'likely error/processing' — both are visually identical mid-flight).
    # First call: clear inline-validation indicators (aria-busy, spinners).
    # Second: poll for real interactive content (3+ inputs/buttons).
    await wait_for_inline_validation_settle(adapter, timeout=8.0)
    rendered_post, n_post, post_wait = await wait_for_content_render(
        adapter, min_interactive_elements=3, timeout=10.0,
    )
    if rendered_post:
        print(
            f"  [strat:wizard] post-transition page rendered "
            f"({n_post} interactive elements after {post_wait:.1f}s)"
        )
    else:
        print(
            f"  [strat:wizard] ⚠ post-transition only {n_post} interactive "
            f"element(s) after {post_wait:.1f}s — classifying anyway"
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

    # ── Click Save & Continue to leave the gated page ────────────────
    # Without this the next strategist iteration re-picks gated_step
    # (section tabs still visible) and gated_step re-runs against an
    # already-filled DOM, crashing on missing inputs that are now
    # thumbnails. Click the page-level nav button to advance to the
    # next wizard step in the SAME iteration.
    from qa.orchestrators.wizard_steps import (
        click_save_and_continue,
        page_signature,
        wait_for_page_transition,
        wait_for_inline_validation_settle,
        wait_for_content_render,
        classify_transition,
    )

    # Some apps surface a "verify all fields" warning banner after the
    # last upload completes — give it a moment to settle so the nav
    # click doesn't race a banner spinner.
    await wait_for_inline_validation_settle(ctx.adapter, timeout=4.0)

    before_sig = await page_signature(ctx.adapter)
    before_snap = await ctx.adapter.raw_snapshot_text()
    clicked, label = await click_save_and_continue(ctx.adapter)
    if not clicked:
        # No nav button after gated completion — unusual but we still
        # captured the sections, surface as a soft stop.
        last_screen = captured[-1] if captured else None
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=last_screen,
            note=(
                f"gated captured {len(captured)} section(s) but no Save & "
                f"Continue button found — page may need manual review"
            ),
            cost=ctx.budget.current_cost - cost_at_start,
        )
    print(f"  [strat:gated] clicked {label!r} after sections — advancing")

    transitioned, signal = await wait_for_page_transition(
        ctx.adapter, before_sig, timeout=15.0,
    )
    if not transitioned:
        last_screen = captured[-1] if captured else None
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=last_screen,
            note=(
                f"gated captured {len(captured)} section(s); Save & Continue "
                f"clicked but no transition detected ({signal}) — likely a "
                f"validation banner blocked submission"
            ),
            cost=ctx.budget.current_cost - cost_at_start,
        )

    # Settle the destination page so the next iteration's pick_strategy
    # doesn't see a loading state.
    await wait_for_inline_validation_settle(ctx.adapter, timeout=8.0)
    await wait_for_content_render(
        ctx.adapter, min_interactive_elements=3, timeout=12.0,
    )
    verdict, reasoning, error_text = await classify_transition(
        ctx.adapter, before_snap, budget=ctx.budget,
    )
    print(f"  [strat:gated] post-advance verdict: {verdict} — {reasoning}")

    last_screen = captured[-1] if captured else None
    if verdict == "SAME_PAGE_WITH_ERROR":
        msg = "validation error after Save & Continue"
        if error_text:
            msg += f": {error_text}"
        return StrategyOutcome(
            success=True,
            advance=False,
            captured=last_screen,
            note=msg,
            cost=ctx.budget.current_cost - cost_at_start,
        )

    # NEW_PAGE or SAME_PAGE_WITH_EXPANSION — both treated as advance
    return StrategyOutcome(
        success=True,
        advance=True,
        captured=last_screen,
        note=f"gated captured {len(captured)} section(s); advanced via {label!r}",
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
