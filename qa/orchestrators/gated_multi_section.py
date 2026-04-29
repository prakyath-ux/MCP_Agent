# qa/orchestrators/gated_multi_section.py — Python-driven orchestrator for
# gated multi-section upload flows (KYC-style: tab → dropdown → upload → OCR).
#
# Phase 2: detects all sections at start, then loops through each one —
# clicking the tab, running the full dropdown+upload+OCR+extract flow, and
# checkpointing the KB after each success. Stop-on-first-failure preserves
# prior captured sections.

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from qa.adapters.protocol import PlatformAdapter
from qa.knowledge.file_resolver import _score_file
from qa.knowledge.store import KnowledgeStore
from qa.models import (
    L0Element,
    L1Element,
    ScreenKnowledge,
)
from qa.models.common import ElementType, make_element_id

from qa.engine.guardrails import (
    GuardrailContext, GuardrailExit,
    per_dropdown_scope, per_page_scope, per_verify_scope,
)
from qa.orchestrators.base import RunContext
from qa.orchestrators.llm_subtask import llm_classify
from qa.orchestrators.sub_prompts import (
    DETECT_ALL_SECTIONS_PROMPT,
    DETECT_ALL_SECTIONS_SCHEMA,
    FIND_ACTIVE_SECTION_PROMPT,
    FIND_ACTIVE_SECTION_SCHEMA,
    EXTRACT_DROPDOWN_OPTIONS_PROMPT,
    EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
    CLASSIFY_POST_ATTACH_STATE_PROMPT,
    CLASSIFY_POST_ATTACH_STATE_SCHEMA,
    EXTRACT_POST_OCR_FIELDS_PROMPT,
    EXTRACT_POST_OCR_FIELDS_SCHEMA,
)
from qa.orchestrators.verify import verify_list_field


class SectionFailed(Exception):
    """Raised when a section can't be completed. Current section is recorded
    as failed; orchestrator stops (per design decision: sections are dependent)."""


class GatedMultiSectionFlow:
    """Orchestrator for gated multi-section document-upload flows.

    Phase 2: detects all sections from the initial snapshot, then loops
    through each one (click tab → run section flow → checkpoint KB).
    On any SectionFailed, prior sections are preserved and we stop.
    """
    pattern_name = "gated_multi_section"

    async def run(self, ctx: RunContext) -> list[ScreenKnowledge]:
        adapter = ctx.adapter
        app_name = ctx.inp.app.app_name

        # Page-level guardrail — if caller didn't supply one, spawn fresh.
        # Threaded into each LLM sub-task for cost + time bounding.
        page_gc = ctx.guardrails if ctx.guardrails is not None else per_page_scope()

        # Wire up web compound tools so we can call the upload impl directly.
        from qa.tools.web_tools import set_server, set_kb
        set_server(adapter.get_mcp_server())
        set_kb(ctx.knowledge, app_name)

        print("\n  [orch] ══ Phase 2: full multi-section loop ══")

        # ── Step 0: detect all section tabs on the page ──────────────────
        print("  [orch] 0    LLM: detect all section tabs")
        initial_snap = await adapter.raw_snapshot_text()
        detect_gc = page_gc.child(
            "detect_sections", hard_max_calls=2, hard_max_cost=0.02,
        )
        try:
            detected = await llm_classify(
                DETECT_ALL_SECTIONS_PROMPT,
                initial_snap,
                DETECT_ALL_SECTIONS_SCHEMA,
                budget=ctx.budget,
                label="detect_sections",
                guardrails=detect_gc,
            )
        except GuardrailExit as e:
            raise SectionFailed(
                f"detect_sections hit guardrail ({e.reason}) before returning data"
            )
        section_tabs: list[str] = [
            t.strip() for t in detected.get("section_tabs", []) if t.strip()
        ]
        if not section_tabs:
            raise SectionFailed(
                f"LLM detected zero sections. Got: {detected}"
            )

        # Filter: if the first detected section has a numbered prefix
        # (e.g. "1. First form of ID"), drop anything that doesn't match
        # that pattern. LLMs occasionally pull in page-level wizard
        # navigation (Contact Info, Documents, PDF / Other Details) and we
        # don't want the loop trying to "enter" those as sections.
        numbered = re.compile(r"^\s*\d+\s*[\.\)\-:]\s*\S+")
        if numbered.match(section_tabs[0]):
            before = len(section_tabs)
            section_tabs = [t for t in section_tabs if numbered.match(t)]
            if len(section_tabs) < before:
                print(
                    f"  [orch]      filtered {before - len(section_tabs)} "
                    f"non-numbered false-positive(s) from detection"
                )

        print(f"  [orch]      detected {len(section_tabs)} section(s):")
        for t in section_tabs:
            print(f"  [orch]        - {t!r}")

        # ── Loop each section ────────────────────────────────────────────
        used_files: set[str] = set()
        captured: list[ScreenKnowledge] = []

        for idx, tab_label in enumerate(section_tabs):
            print(f"\n  [orch] ── Section {idx + 1}/{len(section_tabs)}: {tab_label!r} ──")

            # Navigate to this section (skip for first — should already be active)
            if idx > 0:
                # Debug: heading BEFORE tab click (should still be the
                # previous section's heading)
                await _debug_log_heading(adapter, f"pre-tab-click-section-{idx + 1}")
                print(f"  [orch] nav  click tab {tab_label!r}")
                clicked = await _click_tab(adapter, tab_label)
                if not clicked:
                    raise SectionFailed(
                        f"Could not click tab {tab_label!r} — section {idx + 1} unreachable"
                    )
                await asyncio.sleep(1.5)  # let React mount the new section
                # Debug: heading AFTER tab click + 1.5s settle. If still
                # the previous section's heading, the click didn't
                # navigate (likely an error toast blocking the change).
                await _debug_log_heading(adapter, f"post-tab-click-section-{idx + 1}")

            try:
                screen = await _run_single_section(
                    ctx=ctx,
                    app_name=app_name,
                    expected_section_name=tab_label,
                    used_files=used_files,
                    page_gc=page_gc,
                )
            except SectionFailed as e:
                print(f"\n  [orch] ✗ Section {idx + 1} failed: {e}")
                print(f"  [orch]   Keeping {len(captured)} prior section(s). Stopping loop.")
                raise  # propagate — caller / CLI will report
            else:
                captured.append(screen)
                for fname in _filenames_of(screen):
                    used_files.add(fname)

                # ── Checkpoint KB immediately after each success ──────────
                existing = ctx.knowledge.get_screen(screen.screen_name)
                if existing:
                    ctx.knowledge.screens = [
                        s for s in ctx.knowledge.screens
                        if s.screen_name != screen.screen_name
                    ]
                ctx.knowledge.screens.append(screen)
                path = KnowledgeStore().save(ctx.knowledge)
                print(
                    f"  [orch] ✓ Section {idx + 1} captured ({len(screen.l0)} elements). "
                    f"KB → {path}"
                )
                print(f"  [orch]   Running cost: ${ctx.budget.current_cost:.4f}")

        print(f"\n  [orch] ══ Phase 2 complete: {len(captured)} sections captured ══")
        print(f"  [orch] Final cost: ${ctx.budget.current_cost:.4f}")
        return captured


async def _run_single_section(
    ctx: RunContext,
    app_name: str,
    expected_section_name: str,
    used_files: set[str],
    page_gc: GuardrailContext | None = None,
) -> ScreenKnowledge:
    """Run the 9-step dropdown→upload→OCR→extract flow for whichever
    section is currently active. Returns the ScreenKnowledge on success.

    page_gc — parent guardrail scope. Per-sub-task child scopes are
    spawned inside this function for cost bounding. If None, a fresh
    per_page_scope() is used (less ideal for multi-section runs where
    you want page-wide ceiling, but safe fallback)."""
    adapter = ctx.adapter
    if page_gc is None:
        page_gc = per_page_scope()

    # ── Step 1: snapshot the currently active section ────────────────
    print("  [orch] 1/9  take_snapshot")
    snap = await adapter.raw_snapshot_text()

    # ── Step 2: LLM finds the active section's dropdown + upload ─────
    # Bounded sub-task: one call, small cost — if it goes over, the
    # section can't proceed so we convert to SectionFailed.
    print("  [orch] 2/9  LLM: find active section")
    find_gc = page_gc.child(
        "find_section", hard_max_calls=2, hard_max_cost=0.01,
    )
    try:
        section = await llm_classify(
            FIND_ACTIVE_SECTION_PROMPT,
            snap,
            FIND_ACTIVE_SECTION_SCHEMA,
            budget=ctx.budget,
            label="find_section",
            guardrails=find_gc,
        )
    except GuardrailExit as e:
        raise SectionFailed(
            f"find_section hit guardrail ({e.reason}) in section {expected_section_name!r}"
        )
    section_name = section.get("section_name", "").strip() or expected_section_name
    dropdown_label = section.get("dropdown_label", "").strip()
    upload_label = section.get("upload_field_label", "").strip()
    options = section.get("options", [])

    if not dropdown_label:
        raise SectionFailed(
            f"LLM could not find a dropdown in section {expected_section_name!r}. "
            f"Got: {section}"
        )

    print(f"  [orch]      section={section_name!r}")
    print(f"  [orch]      dropdown={dropdown_label!r}")
    print(f"  [orch]      upload={upload_label!r}")
    print(f"  [orch]      options (from initial snapshot): {options}")

    # ── Step 3: extract dropdown options if missing ──────────────────
    if not options:
        print("  [orch] 3/9a try native <select> via JS")
        native_opts = await _read_native_select_options(adapter, dropdown_label)
        if native_opts:
            options = native_opts
            print(f"  [orch]      native options: {options}")
        else:
            print("  [orch] 3/9b open dropdown + re-extract options")
            opened = await _open_dropdown(adapter, dropdown_label)
            if not opened:
                raise SectionFailed(f"Could not open dropdown {dropdown_label!r}")
            await asyncio.sleep(0.8)
            open_snap = await adapter.raw_snapshot_text()
            print(f"  [orch]      post-open snapshot: {len(open_snap)} chars")
            dropdown_gc = per_dropdown_scope(parent=page_gc)
            try:
                opts = await llm_classify(
                    EXTRACT_DROPDOWN_OPTIONS_PROMPT,
                    open_snap,
                    EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
                    budget=ctx.budget,
                    label="extract_options",
                    guardrails=dropdown_gc,
                )
            except GuardrailExit as e:
                raise SectionFailed(
                    f"extract_options hit guardrail ({e.reason}) for "
                    f"dropdown {dropdown_label!r}"
                )
            options = opts.get("options", [])

            # ── CoVe on dropdown options (Wall 2.7) ─────────────────
            # Drop any option the LLM claimed that isn't actually in
            # the opened popup's snapshot. Tier 1 deterministic only
            # here — same popup, same snapshot, string-match is enough.
            if options:
                verify_result = verify_list_field(
                    {"options": options},
                    "options",
                    open_snap,
                    label="extract_options_cove",
                    log=False,
                )
                if verify_result.dropped:
                    print(
                        f"  [orch]      🔍 CoVe dropped "
                        f"{len(verify_result.dropped)} hallucinated option(s): "
                        f"{[d['value'] for d in verify_result.dropped]}"
                    )
                options = verify_result.claim.get("options", options)

            print(f"  [orch]      extracted options: {options}")
            if not options:
                dbg = _dump_debug_snapshot(
                    "dropdown_empty",
                    app_name,
                    open_snap,
                    extra={
                        "dropdown_label": dropdown_label,
                        "section_name": section_name,
                        "llm_response": opts,
                    },
                )
                print(f"  [orch]      ⚠ debug snapshot: {dbg}")
                raise SectionFailed(
                    f"No dropdown options found in section {section_name!r}. "
                    f"Inspect {dbg} to see the a11y tree."
                )
    else:
        print("  [orch] 3/9  (skipped — options already in first snapshot)")

    # ── Step 4: Python picks the dropdown option ────────────────────
    print("  [orch] 4/9  pick dropdown option")
    remaining_files = [f for f in ctx.available_files if f not in used_files]
    print(f"  [orch]      available files (unused): {remaining_files}")
    chosen_option, _preview = _match_option_to_file(options, remaining_files)
    if not chosen_option:
        raise SectionFailed(
            f"No option in {options!r} matches any unused file in "
            f"{remaining_files!r} (already used: {sorted(used_files)})"
        )
    print(f"  [orch]      picked option={chosen_option!r}")

    # ── Step 5: select the option ────────────────────────────────────
    print("  [orch] 5/9  select dropdown option")
    selected = await _select_dropdown_option(adapter, dropdown_label, chosen_option)
    if not selected:
        raise SectionFailed(f"Could not select option {chosen_option!r}")
    # Selection may reveal additional upload inputs (e.g. front+back for IDs).
    await asyncio.sleep(1.2)

    # ── Step 6: detect every input[type=file] in the section and upload
    #             one file to each before waiting for OCR. ─────────────
    print("  [orch] 6/9  detect upload inputs + attach one file per input")
    inputs = await _detect_upload_inputs(adapter)
    print(f"  [orch]      detected {len(inputs)} input(s):")
    for inp in inputs:
        print(f"  [orch]        - index={inp.get('index')} id={inp.get('id')!r} "
              f"label={(inp.get('label') or '')[:60]!r}")
    if not inputs:
        raise SectionFailed("No input[type=file] elements found after option select")

    # Enforce strict top-to-bottom order: inputs are already in DOM order
    # from querySelectorAll, but sort by index defensively so a reordered
    # detection list can't break the front-before-back invariant.
    inputs_ordered = sorted(inputs, key=lambda i: i.get("index", 0))

    assignments = _match_files_to_inputs(
        inputs=inputs_ordered,
        option_text=chosen_option,
        files=ctx.available_files,
        used_files=used_files,
    )
    if len(assignments) < len(inputs_ordered):
        raise SectionFailed(
            f"Not enough files to satisfy {len(inputs_ordered)} inputs — "
            f"only assigned {len(assignments)}. "
            f"Available: {ctx.available_files} Used so far: {sorted(used_files)}"
        )

    from qa.tools.web_tools import _upload_file_for_field_impl
    uploaded_files: list[str] = []
    for upload_idx, (inp, fname, is_reused) in enumerate(assignments):
        tid = inp.get("id") or ""
        label = (inp.get("label") or "").strip()[:60]
        reuse_tag = " [reused from prior section]" if is_reused else ""
        print(f"  [orch]      → input id={tid!r} ← file={fname!r} (label={label!r}){reuse_tag}")
        attach_result = await _upload_file_for_field_impl(
            field_name=label or upload_label or dropdown_label,
            file_name=fname,
            wait_for_ocr=False,
            target_input_id=tid,
        )
        try:
            attach_json = json.loads(attach_result)
        except json.JSONDecodeError:
            attach_json = {"status": "UNKNOWN", "raw": attach_result[:200]}
        status = attach_json.get("status")
        if status not in ("ATTACHED", "PASS"):
            raise SectionFailed(
                f"Attachment failed for input {tid!r}: {attach_json}"
            )
        # We don't read .files[0] back — React's onChange handler synchronously
        # consumes the file into component state, so the input is "empty"
        # immediately after upload even on a successful attach. Routing
        # correctness for multi-input sections is enforced upstream by
        # expose_js clearing all prior MARKER attributes before tagging the
        # current target.

        uploaded_files.append(fname)
        used_files.add(fname)

        # Debug: confirm React received the file. If the input's
        # .files[0] is empty, the upload didn't stick.
        await _debug_log_file_inputs(adapter, f"post-upload-{upload_idx}")

        # Reverting to the simple inter-upload sleep that worked
        # before — adding poll-based wait helpers caused regressions
        # by repeatedly snapshotting the page while React was mid-
        # processing the front upload.
        await asyncio.sleep(0.8)

        # Debug: re-check after the sleep, before the next upload.
        # If the front file disappeared between iterations, that's
        # the React-state-reset bug we've been chasing.
        if upload_idx < len(assignments) - 1:
            await _debug_log_file_inputs(adapter, f"pre-next-upload-{upload_idx + 1}")

    # Primary file for the section report = first uploaded (label-wise
    # this is typically the 'front' / main doc).
    chosen_file = uploaded_files[0] if uploaded_files else ""

    # ── Step 7: classify post-attach state ───────────────────────────
    print("  [orch] 7/9  LLM: classify post-attach state")
    post_attach_snap = await adapter.raw_snapshot_text()
    post_attach_gc = page_gc.child(
        "post_attach", hard_max_calls=2, hard_max_cost=0.01,
    )
    try:
        classify = await llm_classify(
            CLASSIFY_POST_ATTACH_STATE_PROMPT,
            post_attach_snap,
            CLASSIFY_POST_ATTACH_STATE_SCHEMA,
            budget=ctx.budget,
            label="post_attach",
            guardrails=post_attach_gc,
        )
    except GuardrailExit as e:
        raise SectionFailed(
            f"post_attach state classification hit guardrail ({e.reason})"
        )
    state = classify.get("state", "")
    button_label = (classify.get("button_label") or "").strip()
    button_safe = classify.get("button_is_safe", False)
    print(
        f"  [orch]      state={state!r} button={button_label!r} "
        f"safe={button_safe} — {classify.get('reasoning', '')}"
    )

    # ── Step 7b: click confirm button if needed + safe ──────────────
    if state == "needs_button_click":
        if not button_label or not button_safe:
            raise SectionFailed(
                "Post-attach state requires a button click but no safe "
                f"button identified (label={button_label!r} safe={button_safe})"
            )
        print(f"  [orch]      clicking confirm button {button_label!r}")
        clicked = await _click_button_by_label(adapter, button_label)
        if not clicked:
            raise SectionFailed(f"Could not click safe button {button_label!r}")

    # ── Step 8: wait for OCR (skip if already done) ──────────────────
    if state == "ocr_completed":
        print("  [orch] 8/9  (skipped — OCR already completed)")
    else:
        print("  [orch] 8/9  poll for OCR completion")
        from qa.tools.web_tools import _wait_for_verification, _detect_upload_success
        # 60s cap — TECU's KYC OCR was observed at 22-28s on a fast day and
        # has burst past 30s under load. Cheap to wait longer than to
        # fail-then-rerun-from-scratch.
        OCR_TIMEOUT = 60.0
        verify_snap, signal, elapsed = await _wait_for_verification(
            post_attach_snap, timeout=OCR_TIMEOUT, poll_interval=1.5,
        )
        success = _detect_upload_success(post_attach_snap, verify_snap, chosen_file)
        # On timeout, fall through to a second chance: peek for new fields
        # appearing anyway (some apps keep the spinner visible after the
        # data layer has already populated).
        if not success and "timeout" in signal:
            await asyncio.sleep(1.0)
            late_snap = await adapter.raw_snapshot_text()
            success = _detect_upload_success(post_attach_snap, late_snap, chosen_file)
            if success:
                print(f"  [orch]      late success after timeout: {success!r}")
                verify_snap = late_snap
        print(f"  [orch]      OCR wait: {signal} ({elapsed:.1f}s) success={success!r}")
        if not success:
            raise SectionFailed(
                f"OCR did not produce a success signal within {OCR_TIMEOUT:.0f}s ({signal})"
            )

    # ── Step 9: extract post-OCR auto-filled fields ──────────────────
    print("  [orch] 9/9  LLM: extract post-OCR fields")
    await asyncio.sleep(1.0)
    post_snap = await adapter.raw_snapshot_text()
    post_ocr_gc = page_gc.child(
        "extract_post_ocr", hard_max_calls=2, hard_max_cost=0.03,
    )
    try:
        post = await llm_classify(
            EXTRACT_POST_OCR_FIELDS_PROMPT,
            post_snap,
            EXTRACT_POST_OCR_FIELDS_SCHEMA,
            budget=ctx.budget,
            label="extract_post_ocr",
            guardrails=post_ocr_gc,
        )
    except GuardrailExit as e:
        print(f"  [orch]      ⚠ post_ocr guardrail hit ({e.reason}) — "
              "proceeding with no auto-filled fields")
        post = {"fields": []}
    new_fields = post.get("fields", [])

    # ── CoVe on post-OCR field names (Wall 2.7) ─────────────────────
    # Verify each claimed field name actually appears in post_snap. This
    # catches hallucinated fields before they enter the KB. We verify by
    # the 'name' field of each field dict.
    if new_fields:
        claim = {"field_names": [f.get("name", "") for f in new_fields if f.get("name")]}
        verify_result = verify_list_field(
            claim, "field_names", post_snap,
            label="post_ocr_cove", log=False,
        )
        verified_names = set(verify_result.claim.get("field_names", []))
        if verify_result.dropped:
            print(
                f"  [orch]      🔍 CoVe dropped "
                f"{len(verify_result.dropped)} hallucinated field(s): "
                f"{[d['value'] for d in verify_result.dropped]}"
            )
            new_fields = [f for f in new_fields if f.get("name") in verified_names]
    print(f"  [orch]      captured {len(new_fields)} post-OCR fields")

    return _build_screen(
        section_name=section_name,
        page_url=ctx.inp.app.url or "",
        dropdown_label=dropdown_label,
        options=options,
        chosen_option=chosen_option,
        chosen_file=chosen_file,
        upload_label=upload_label,
        new_fields=new_fields,
        uploaded_files=uploaded_files,
    )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

_TRIGGER_ROLES = ("combobox", "button", "select", "listbox", "textbox")
_LINE_RE = re.compile(r"uid=(\S+)\s+(\S+)\s+\"([^\"]*)\"")


def _parse_snapshot_line(line: str) -> tuple[str, str, str] | None:
    """Parse a snapshot line into (uid, role, visible_text).
    Lines look like: `  uid=1_25 button "Choose an option"`.
    Returns None for lines that don't match (StaticText without quotes, etc.)."""
    m = _LINE_RE.search(line)
    if not m:
        # Some lines use StaticText with quotes but no trailing bits —
        # the regex above already handles those. Fall back: try without text.
        m2 = re.search(r"uid=(\S+)\s+(\S+)", line)
        if not m2:
            return None
        return m2.group(1), m2.group(2), ""
    return m.group(1), m.group(2), m.group(3)


def _find_dropdown_trigger_uid(snapshot: str, label: str) -> str:
    """Find the uid of the dropdown TRIGGER (combobox/button/select),
    not just any element whose text contains the label.

    TECU-style pages often have: `StaticText "Please select..."` followed by
    `button "Choose an option"`. We want the button uid, not the label uid."""
    if not snapshot or not label:
        return ""
    label_l = label.lower()
    lines = snapshot.split("\n")
    parsed: list[tuple[int, str, str, str]] = []  # (line_idx, uid, role, text)
    for i, line in enumerate(lines):
        p = _parse_snapshot_line(line)
        if p:
            parsed.append((i, p[0], p[1].lower(), p[2].lower()))

    # Pass 1: a trigger whose OWN text contains the label (rare but possible).
    for _, uid, role, text in parsed:
        if role in _TRIGGER_ROLES and label_l in text:
            return uid

    # Pass 2: find the label line (any role with that text), then pick the
    # NEXT trigger-role element within a small window.
    for idx, (line_idx, uid, role, text) in enumerate(parsed):
        if label_l not in text:
            continue
        # Scan following parsed entries (not raw lines — skips filtered noise).
        for _, uid2, role2, _ in parsed[idx + 1 : idx + 10]:
            if role2 in _TRIGGER_ROLES:
                return uid2
        break

    # Pass 3: last-resort — any uid on a line that contains the label text.
    for line in lines:
        if label_l in line.lower():
            m = re.search(r"uid=(\S+)", line)
            if m:
                return m.group(1)
    return ""


async def _read_native_select_options(
    adapter: PlatformAdapter, dropdown_label: str
) -> list[str]:
    """If the dropdown is a native <select>, read options directly via JS —
    no click needed, no snapshot parsing. Returns [] if not a native select."""
    js = (
        "() => {"
        "  const all = [...document.querySelectorAll('select')];"
        f"  const label = {json.dumps(dropdown_label)}.toLowerCase();"
        "  const match = all.find(s => {"
        "    const id = s.id || s.name || '';"
        "    const aria = s.getAttribute('aria-label') || '';"
        "    const parent = s.closest('label, .MuiFormControl-root, .field, .form-group');"
        "    const ptext = parent ? parent.textContent.toLowerCase() : '';"
        "    return ptext.includes(label) || aria.toLowerCase().includes(label) "
        "      || id.toLowerCase().includes(label);"
        "  });"
        "  if (!match) return JSON.stringify({native: false, options: []});"
        "  const opts = [...match.options].map(o => o.textContent.trim()).filter(Boolean);"
        "  return JSON.stringify({native: true, options: opts});"
        "}"
    )
    raw = await adapter.evaluate_script(js)
    from qa.tools.web_tools import _safe_parse
    data = _safe_parse(raw)
    if isinstance(data, dict) and data.get("native"):
        return data.get("options", []) or []
    return []


async def _open_dropdown(adapter: PlatformAdapter, dropdown_label: str) -> bool:
    """Open a dropdown. Tries MCP native click on the combobox/button trigger.
    Returns True on non-error click. For native <select>, see
    _read_native_select_options which sidesteps opening entirely."""
    snap = await adapter.raw_snapshot_text()
    uid = _find_dropdown_trigger_uid(snap, dropdown_label)
    if not uid:
        print(f"    [open_dropdown] no trigger uid for {dropdown_label!r}")
        return False
    print(f"    [open_dropdown] trigger uid={uid}")

    server = adapter.get_mcp_server()
    result = await server.call_tool("click", {"uid": uid})
    text = ""
    if result.content:
        text = result.content[0].text or ""
    if "error" in text.lower():
        print(f"    [open_dropdown] click error: {text[:120]}")
        return False
    return True


def _dump_debug_snapshot(
    label: str, app_name: str, snapshot: str, extra: dict | None = None
) -> Path:
    """Save a snapshot to artifacts/debug/orchestrator/ so failures can be
    inspected later. Path is printed so the caller can share it back."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")
    outdir = Path("artifacts/debug/orchestrator") / safe
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{ts}_{label}.txt"
    payload = []
    if extra:
        payload.append("# Context")
        for k, v in extra.items():
            payload.append(f"{k}: {v}")
        payload.append("")
    payload.append("# Snapshot")
    payload.append(snapshot or "(empty)")
    path.write_text("\n".join(payload))
    return path


async def _select_dropdown_option(
    adapter: PlatformAdapter, dropdown_label: str, option_text: str
) -> bool:
    """Select `option_text` on the dropdown labelled `dropdown_label`.
    Assumes the dropdown is currently open (its options are in the snapshot)."""
    from qa.tools.web_tools import _find_uid_by_text

    # If it wasn't already open, open it first.
    snap = await adapter.raw_snapshot_text()
    option_uid = _find_uid_by_text(snap, option_text)

    if not option_uid:
        opened = await _open_dropdown(adapter, dropdown_label)
        if not opened:
            return False
        await asyncio.sleep(0.5)
        snap = await adapter.raw_snapshot_text()
        option_uid = _find_uid_by_text(snap, option_text)

    if not option_uid:
        print(f"    [select_option] option uid not found: {option_text!r}")
        return False

    server = adapter.get_mcp_server()
    result = await server.call_tool("click", {"uid": option_uid})
    text = ""
    if result.content:
        text = result.content[0].text or ""
    if "error" in text.lower():
        print(f"    [select_option] click error: {text[:120]}")
        return False
    return True


async def _click_tab(adapter: PlatformAdapter, tab_label: str) -> bool:
    """Click a section/tab navigation button by its visible label.
    Used by the multi-section loop to advance from one section to the next."""
    from qa.tools.web_tools import _find_uid_by_text

    snap = await adapter.raw_snapshot_text()
    uid = _find_uid_by_text(snap, tab_label)
    if not uid:
        print(f"    [click_tab] no uid found for tab {tab_label!r}")
        return False
    print(f"    [click_tab] clicking tab uid={uid} {tab_label!r}")

    server = adapter.get_mcp_server()
    result = await server.call_tool("click", {"uid": uid})
    text = ""
    if result.content:
        text = result.content[0].text or ""
    if "error" in text.lower():
        print(f"    [click_tab] click error: {text[:120]}")
        return False
    return True


def _filenames_of(screen: ScreenKnowledge) -> list[str]:
    """Pull every chosen filename out of a captured screen's file_upload L0s
    so the loop can mark them as used and exclude them from later sections."""
    names: list[str] = []
    for el in screen.l0:
        if el.type == ElementType.FILE_UPLOAD and el.default_value:
            names.append(el.default_value)
    return names


async def _debug_log_file_inputs(adapter, label: str) -> None:
    """Diagnostic: print the state of every input[type=file] in the DOM.
    Used to confirm whether React kept the front file when the back was
    uploaded. No behavioral side effects — pure read."""
    js = (
        "() => {"
        "  const inps = [...document.querySelectorAll('input[type=file]')];"
        "  return JSON.stringify(inps.map((el, i) => ({"
        "    i: i,"
        "    id: el.id || '',"
        "    name: el.name || '',"
        "    n: el.files ? el.files.length : 0,"
        "    fname: el.files && el.files[0] ? el.files[0].name : ''"
        "  })));"
        "}"
    )
    try:
        from qa.tools.web_tools import _safe_parse
        raw = await adapter.evaluate_script(js)
        parsed = _safe_parse(raw)
        if isinstance(parsed, list):
            print(f"  [debug:{label}] file inputs:")
            for entry in parsed:
                print(
                    f"  [debug:{label}]   #{entry.get('i')} id={entry.get('id')!r} "
                    f"files={entry.get('n')} name={entry.get('fname')!r}"
                )
        else:
            print(f"  [debug:{label}] could not parse: {raw[:200]!r}")
    except Exception as e:
        print(f"  [debug:{label}] raised: {type(e).__name__}: {e}")


async def _debug_log_heading(adapter, label: str) -> None:
    """Diagnostic: print the visible h1/h2/heading text. Used at section
    transitions to verify whether the snapshot reflects the new section."""
    js = (
        "() => {"
        "  const headings = [...document.querySelectorAll('h1, h2, [role=heading]')]"
        "    .filter(e => e.offsetParent !== null);"
        "  return JSON.stringify({"
        "    headings: headings.map(h => (h.textContent || '').trim().slice(0, 100)),"
        "    url: window.location.href"
        "  });"
        "}"
    )
    try:
        from qa.tools.web_tools import _safe_parse
        raw = await adapter.evaluate_script(js)
        parsed = _safe_parse(raw)
        if isinstance(parsed, dict):
            heads = parsed.get("headings") or []
            url = parsed.get("url", "")
            print(f"  [debug:{label}] url={url!r}")
            print(f"  [debug:{label}] visible headings: {heads[:5]}")
        else:
            print(f"  [debug:{label}] could not parse: {raw[:200]!r}")
    except Exception as e:
        print(f"  [debug:{label}] raised: {type(e).__name__}: {e}")


async def _detect_upload_inputs(adapter: PlatformAdapter) -> list[dict]:
    """Enumerate all input[type=file] elements currently in the DOM, along
    with any nearby label text that hints at what goes in each (Front / Back
    / etc.). Returns a list of {id, name, label, index} dicts.

    Uses web_tools._safe_parse — same parser the existing upload tool uses,
    which handles Chrome MCP's various result-wrapping formats (markdown
    fences, escaped quoted strings, etc.)."""
    from qa.tools.web_tools import _safe_parse

    js = (
        "() => {"
        "  const inps = [...document.querySelectorAll('input[type=file]')];"
        "  return JSON.stringify(inps.map((el, i) => {"
        "    const parent = el.closest('label, .MuiFormControl-root, .field, .form-group, div');"
        "    const label = parent ? (parent.textContent||'').trim().slice(0, 80) : '';"
        "    return {"
        "      index: i,"
        "      id: el.id || '',"
        "      name: el.name || '',"
        "      accept: el.accept || '',"
        "      label: label"
        "    };"
        "  }));"
        "}"
    )
    raw = await adapter.evaluate_script(js)
    parsed = _safe_parse(raw)
    if isinstance(parsed, list):
        return parsed
    print(f"    [detect_inputs] could not parse result. raw={raw[:300]!r}")
    return []


def _match_files_to_inputs(
    inputs: list[dict],
    option_text: str,
    files: list[str],
    used_files: set[str],
) -> list[tuple[dict, str, bool]]:
    """Assign files to inputs. Each input gets ONE file.

    Matching uses token overlap between filename and the input's id/label
    (e.g. "upload-front-input" ↔ "front.png"). The selected dropdown option
    is a weaker hint for context.

    Two-tier: first pass prefers FRESH files (not in used_files), second
    pass allows reuse from prior sections. Reuse is legitimate for cases
    like a "nominee" upload slot that accepts an already-submitted ID.
    Within a single section, each file is still picked at most once.

    Returns list of (input_dict, chosen_filename, is_reused).
    """
    assignments: list[tuple[dict, str, bool]] = []
    already_picked: set[str] = set()

    for inp in inputs:
        context = " ".join(filter(None, [
            inp.get("id", ""),
            inp.get("name", ""),
            inp.get("label", ""),
            option_text,
        ]))

        # Tier 1: fresh files not yet used anywhere
        fresh = [
            f for f in files
            if f not in used_files and f not in already_picked
        ]
        # Tier 2: any file not picked in THIS section (cross-section reuse OK)
        any_available = [f for f in files if f not in already_picked]

        pool = fresh if fresh else any_available
        is_reused = not bool(fresh)
        if not pool:
            break

        best_score = 0.0
        best_file = pool[0]  # fallback so we always assign something
        for fname in pool:
            stem = Path(fname).stem
            score = _score_file(
                filename_stem=stem,
                element_name=context,
                semantic_hint="",
                accept="",
            )
            if score > best_score:
                best_score = score
                best_file = fname

        assignments.append((inp, best_file, is_reused))
        already_picked.add(best_file)

    return assignments


async def _click_button_by_label(
    adapter: PlatformAdapter, label: str
) -> bool:
    """Find and click a button whose visible text contains `label`.
    Uses the a11y snapshot — no JS events (those silently fail on
    React/MUI buttons). Returns True if the click didn't error."""
    from qa.tools.web_tools import _find_uid_by_text

    snap = await adapter.raw_snapshot_text()
    uid = _find_uid_by_text(snap, label)
    if not uid:
        print(f"    [click_button] no uid found for label {label!r}")
        return False

    server = adapter.get_mcp_server()
    result = await server.call_tool("click", {"uid": uid})
    text = ""
    if result.content:
        text = result.content[0].text or ""
    if "error" in text.lower():
        print(f"    [click_button] click error: {text[:120]}")
        return False
    await asyncio.sleep(0.6)  # brief settle before caller polls for OCR
    return True


def _match_option_to_file(
    options: list[str], files: list[str]
) -> tuple[str, str]:
    """Pick the (option, file) pair whose names have the strongest direct
    token overlap.

    Scoring is option-vs-filename only — no semantic_hint expansion, because
    the orchestrator already knows which section it's in. We don't want
    generic "id_document" synonyms making passport.png win for every option.
    Returns ("", "") if no pair has any shared token.
    """
    if not options or not files:
        return "", ""

    best_score = 0.0
    best_option = ""
    best_file = ""

    for option in options:
        for fname in files:
            stem = Path(fname).stem
            score = _score_file(
                filename_stem=stem,
                element_name=option,
                semantic_hint="",   # no synonym bias in the matcher
                accept="",
            )
            if score > best_score:
                best_score = score
                best_option = option
                best_file = fname

    return best_option, best_file


def _build_screen(
    *,
    section_name: str,
    page_url: str,
    dropdown_label: str,
    options: list[str],
    chosen_option: str,
    chosen_file: str,
    upload_label: str,
    new_fields: list[dict],
    uploaded_files: list[str] | None = None,
) -> ScreenKnowledge:
    """Assemble a ScreenKnowledge from orchestrator findings.

    `uploaded_files` is the ordered list of every file we attached in this
    section — one entry per input[type=file]. For single-upload sections
    pass [chosen_file] or leave None (derived from chosen_file).
    """
    l0: list[L0Element] = []
    l1: list[L1Element] = []

    if uploaded_files is None:
        uploaded_files = [chosen_file] if chosen_file else []

    # Record the dropdown itself
    dd_id = make_element_id(section_name, dropdown_label, "dropdown")
    l0.append(L0Element(
        element_id=dd_id,
        name=dropdown_label,
        type=ElementType.DROPDOWN,
        required=True,
        behavior=f"Choose document type for {section_name}",
        options=options,
        screen_name=section_name,
        default_value=chosen_option,
    ))
    l1.append(L1Element(element_id=dd_id, screen_name=section_name))

    # Record each upload performed in this section. Sections that need
    # multiple files (front + back of ID, for example) get one L0 per file.
    for i, fname in enumerate(uploaded_files):
        slot_label = upload_label or "Upload"
        if len(uploaded_files) > 1:
            slot_label = f"{upload_label or 'Upload'} #{i + 1}"
        up_id = make_element_id(section_name, slot_label, "file_upload")
        l0.append(L0Element(
            element_id=up_id,
            name=slot_label,
            type=ElementType.FILE_UPLOAD,
            required=True,
            behavior=f"Upload document for {section_name} (OCR-verified)",
            screen_name=section_name,
            semantic_hint="id_document",
            default_value=fname,
        ))
        l1.append(L1Element(element_id=up_id, screen_name=section_name))

    # Record post-OCR auto-filled fields
    for field in new_fields:
        fname = field.get("name", "").strip()
        if not fname:
            continue
        ftype_str = field.get("type", "text_input")
        try:
            ftype = ElementType(ftype_str)
        except ValueError:
            ftype = ElementType.TEXT_INPUT
        fid = make_element_id(section_name, fname, ftype.value)
        l0.append(L0Element(
            element_id=fid,
            name=fname,
            type=ftype,
            required=bool(field.get("required", False)),
            behavior=field.get("behavior", ""),
            screen_name=section_name,
            default_value=field.get("value_entered", ""),
        ))
        l1.append(L1Element(element_id=fid, screen_name=section_name))

    return ScreenKnowledge(
        screen_name=section_name,
        screen_url=page_url,
        l0=l0,
        l1=l1,
    )
