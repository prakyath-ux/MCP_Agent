# qa/pipelines/plan.py — Pipeline 2: Test Case Planning (pure LLM, no MCP)

import asyncio
import json
import re
import time

from agents import Agent, Runner

from qa.models import PlanInput, PlanOutput, TestCase, TestApproach, TestCasePriority
from qa.engine.model_factory import build_model_config, build_model_settings


async def run_plan(inp: PlanInput) -> PlanOutput:
    """Pipeline 2: Generate test cases from L0 knowledge index.

    This pipeline has NO MCP server — pure LLM reasoning.
    Reads only L0 (tiny planning index), generates test cases.
    One LLM call, cheapest pipeline.
    """
    from qa.prompts.plan import PLAN_PROMPT

    # Build L0 index — only the compact planning data
    screen_names = inp.screen_names or inp.knowledge.screen_names()
    l0_index = []
    for screen_name in screen_names:
        l0_index.extend(inp.knowledge.get_l0_index(screen_name))

    if not l0_index:
        print("  ERROR: No L0 elements found in knowledge base")
        return PlanOutput(model=inp.model)

    # Filter out non-testable elements (nav tabs, back buttons, headers, decorative, labels)
    # Wall E3 — radios need their own approach (Yes/No tap) and don't fit
    # SELECT_AND_VERIFY or FILL_CHECK patterns. Skip them for now; a
    # RADIO_TOGGLE approach is future work. Checkboxes likewise.
    skip_types = {"nav_tab", "other", "radio", "checkbox"}
    skip_name_keywords = {
        "back", "backbutton", "headerimage", "headercontainer", "texture_view",
        "screen title", "title", "label", "tab label", "nav text", "header",
        # Dropdown helper inputs that only exist while a parent dropdown is open.
        # These should not be treated as standalone testable fields — selecting
        # an option from the parent dropdown covers their purpose.
        "search", "filter",
    }

    def _is_testable(el) -> bool:
        if el.type.value in skip_types:
            return False
        name_lower = el.name.lower()
        # Skip elements whose name contains label/title/header keywords
        for kw in skip_name_keywords:
            if kw in name_lower:
                # But keep actual fields like "Enter Details" (text_input with real purpose)
                # Rule: if it's a dropdown/date_picker, always keep
                if el.type.value in {"dropdown", "date_picker"}:
                    return True
                # Otherwise, this is a label/header text — skip
                return False
        return True

    l0_filtered = [el for el in l0_index if _is_testable(el)]

    # Drop text_input shadows of dropdowns/combobox/date_picker. Some apps
    # expose a hidden <input> that backs a custom dropdown — enumerate
    # captures both, and Plan then generates duplicate test cases for the
    # same logical field. Group by element_id stem (screen[:section]:label)
    # and keep the richer type when a text_input shares the stem.
    PREFERRED_OVER_TEXT = {"dropdown", "combobox", "date_picker"}

    def _stem(el) -> str:
        parts = el.element_id.rsplit(":", 1)
        return parts[0] if len(parts) == 2 else el.element_id

    groups: dict[str, list] = {}
    for el in l0_filtered:
        groups.setdefault(_stem(el), []).append(el)

    deduped: list = []
    dropped = 0
    for group in groups.values():
        if len(group) == 1:
            deduped.extend(group)
            continue
        types_seen = {e.type.value for e in group}
        if "text_input" in types_seen and (types_seen & PREFERRED_OVER_TEXT):
            for el in group:
                if el.type.value == "text_input":
                    dropped += 1
                else:
                    deduped.append(el)
        else:
            deduped.extend(group)
    if dropped:
        print(f"  Plan dedup: dropped {dropped} text_input shadow(s) of dropdown/combobox/date_picker fields")
    l0_filtered = deduped

    # Apply element_filter (scoped execution — "dropdown", "Transaction Type", etc.)
    if inp.element_filter:
        filter_lower = inp.element_filter.lower().strip()
        scoped = [
            el for el in l0_filtered
            if filter_lower in el.type.value.lower() or filter_lower in el.name.lower()
        ]
        if scoped:
            print(f"  Scoped to filter '{inp.element_filter}': {len(l0_filtered)} → {len(scoped)} element(s)")
            l0_filtered = scoped
        else:
            print(f"  ⚠ Filter '{inp.element_filter}' matched 0 elements — keeping all {len(l0_filtered)} testable elements")

    if not l0_filtered:
        print("  ERROR: No testable elements after filtering")
        return PlanOutput(model=inp.model)

    # Truncate long dropdown option lists before sending to Plan. The LLM
    # only needs ~5 options to pick a verbatim test_value — the full lists
    # (e.g. TECU's 474-company Employer, 233-country Nationality) balloon
    # the prompt and add no planning signal. Execute still matches against
    # the live DOM at runtime, not this truncated list. KB stays complete.
    _MAX_OPTS_PER_DROPDOWN = 5

    def _trim_options_for_plan(el_dump: dict) -> dict:
        opts = el_dump.get("options")
        if isinstance(opts, list) and len(opts) > _MAX_OPTS_PER_DROPDOWN:
            total = len(opts)
            el_dump["options"] = opts[:_MAX_OPTS_PER_DROPDOWN]
            # Inline signal to the LLM that more exist — no schema change.
            el_dump["options_note"] = (
                f"showing {_MAX_OPTS_PER_DROPDOWN} of {total} — "
                "pick any for verbatim test_value"
            )
        return el_dump

    l0_for_prompt = [
        _trim_options_for_plan(el.model_dump(exclude_defaults=True))
        for el in l0_filtered
    ]
    l0_json = json.dumps(l0_for_prompt, indent=2)
    l0_index = l0_filtered  # Use filtered count for display

    print(f"\n{'='*60}")
    print(f"  PIPELINE 2: PLAN")
    print(f"  Elements: {len(l0_index)} across {len(screen_names)} screen(s)")
    print(f"  L0 tokens: ~{len(l0_json.split())} words")
    print(f"  Max cases: {inp.max_total_cases}")
    print(f"  Model: {inp.model}")
    print(f"{'='*60}\n")

    model_config, provider_label = build_model_config(inp.model, inp.provider)
    model_settings = build_model_settings(inp.model)
    print(f"  Provider: {provider_label}")

    # Build value hints
    value_hint = ""
    if inp.test_values_hint:
        value_hint = (
            f"\n\nUSER REQUESTED THESE SPECIFIC VALUES: {', '.join(inp.test_values_hint)}\n"
            f"Use these EXACT values in your test cases. Match them to the appropriate elements."
        )
    elif inp.avoid_recent_values:
        # Look at L2 history to find recently used values
        recent_values: list[str] = []
        for screen in inp.knowledge.screens:
            for l2_el in screen.l2:
                for run in l2_el.runs[-2:]:  # last 2 runs
                    if run.value_entered:
                        recent_values.append(run.value_entered)
        if recent_values:
            value_hint = (
                f"\n\nAVOID THESE RECENTLY USED VALUES (user wants different data): {', '.join(set(recent_values))}\n"
                f"Pick OTHER options from each dropdown's options list."
            )

    task = (
        f"Here is the L0 knowledge index for the application:\n\n"
        f"{l0_json}\n\n"
        f"Generate a test plan with {inp.max_total_cases} or fewer test cases, "
        f"max {inp.max_cases_per_field} per field. "
        f"Output ONLY the ## TEST PLAN section."
        f"{value_hint}"
    )

    agent = Agent(
        name="QA Test Planner",
        instructions=PLAN_PROMPT,
        model=model_config,
        # No model_settings — no tools means parallel_tool_calls can't be set
        # No mcp_servers — pure planning
    )

    # Attach a TurnLogger so plan's single LLM call gets cost-tracked just like
    # explore/execute do. Without this, plan always reports cost=$0 even though
    # it makes a real LLM call (~$0.001-$0.005).
    from qa.engine.orchestrator import TurnLogger
    plan_logger = TurnLogger(model=inp.model)

    start_time = time.time()
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, input=task, max_turns=1, hooks=plan_logger),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        print(f"\n  TIMEOUT: plan stalled for {elapsed:.1f}s — returning empty plan")
        return PlanOutput(model=inp.model, duration_sec=elapsed)
    duration = time.time() - start_time

    raw_plan = result.final_output or ""
    test_cases = _parse_test_plan(raw_plan)

    # Resolve UPLOAD_FILE placeholders to real file paths via the 4-tier lookup.
    # Plan prompt tells the LLM to emit test_value="AUTO" for uploads; we swap
    # it for an actual path from artifacts/test_files/ using the element's
    # accept + semantic_hint captured during explore.
    from qa.models.plan import TestApproach
    from qa.knowledge.file_resolver import resolve_upload_path
    l0_by_id = {el.element_id: el for el in l0_filtered}
    upload_l0s = [el for el in l0_filtered if el.type.value == "file_upload"]

    def _match_l0(tc) -> object | None:
        # 1. Exact element_id match
        l0 = l0_by_id.get(tc.element_id)
        if l0:
            return l0
        # 2. Suffix match — LLM sometimes truncates the screen prefix
        for el in l0_filtered:
            if el.element_id.endswith(tc.element_id) or tc.element_id.endswith(el.element_id):
                return el
        # 3. Match by field name (case-insensitive) for file uploads
        if upload_l0s:
            tc_name_lower = tc.field_name.lower()
            for el in upload_l0s:
                if el.name.lower() in tc_name_lower or tc_name_lower in el.name.lower():
                    return el
        return None

    # Iterate KB's file_upload elements directly. For each one, find or create
    # a matching test case with a resolved file path. Don't trust the LLM to
    # emit the right element_id or approach.
    from qa.models.plan import TestApproach, TestCasePriority
    from qa.knowledge.file_resolver import resolve_upload_path

    def _resolve(l0):
        return resolve_upload_path(
            element_id=l0.element_id,
            semantic_hint=l0.semantic_hint or "other",
            accept=l0.accept,
            app_name=inp.knowledge.app.app_name,
            element_name=l0.name,
        )

    handled_l0_ids: set[str] = set()
    for upload_l0 in upload_l0s:
        # Find the LLM-emitted case that corresponds to this L0 (by id, suffix,
        # or field name). If found, fix it. If not, append a new one.
        match: TestCase | None = None
        for tc in test_cases:
            l0_for_tc = _match_l0(tc)
            if l0_for_tc and l0_for_tc.element_id == upload_l0.element_id:
                match = tc
                break

        resolved_path = _resolve(upload_l0)
        if match:
            old_value = match.test_value
            match.element_id = upload_l0.element_id
            match.field_name = upload_l0.name
            match.approach = TestApproach.UPLOAD_FILE
            match.test_value = resolved_path
            match.expected_result = "uploaded"
            print(f"  ✓ Plan: fixed UPLOAD case {match.tc_id} ({upload_l0.name}) — was {old_value!r} → {resolved_path}")
        else:
            new_tc = TestCase(
                tc_id=f"TC{len(test_cases) + 1}",
                element_id=upload_l0.element_id,
                field_name=upload_l0.name,
                approach=TestApproach.UPLOAD_FILE,
                test_value=resolved_path,
                expected_result="uploaded",
                priority=TestCasePriority.MED,
                screen_name=upload_l0.screen_name,
            )
            test_cases.append(new_tc)
            print(f"  ✓ Plan: injected UPLOAD case {new_tc.tc_id} ({upload_l0.name}) → {resolved_path}")
        handled_l0_ids.add(upload_l0.element_id)

    # Wall 1.7 — Auto-fill / read-only guard. Any test case targeting an
    # L0 element whose behavior declares it populated by the app (OCR,
    # session pre-fill, computed value) gets forced to VERIFY_ONLY. This
    # runs regardless of what the LLM emitted — Python enforces the rule
    # so a missed prompt instruction can't corrupt the form.
    _AUTOFILL_MARKERS = (
        "auto_filled", "auto-filled", "autofilled",
        "read_only",   "read-only",   "readonly",
        "masked",
    )

    def _is_autofilled(el) -> bool:
        b = (getattr(el, "behavior", "") or "").lower()
        return any(m in b for m in _AUTOFILL_MARKERS)

    deduped: list[TestCase] = []
    seen_verify_only: set[str] = set()
    forced_count = 0
    for tc in test_cases:
        l0 = inp.knowledge.get_l0_for_element(tc.element_id)
        if l0 is not None and _is_autofilled(l0):
            # Keep a single VERIFY_ONLY per field; drop FILL_CHECK/etc.
            if tc.element_id in seen_verify_only:
                forced_count += 1
                continue
            if tc.approach != TestApproach.VERIFY_ONLY:
                tc.approach = TestApproach.VERIFY_ONLY
                tc.test_value = ""
                tc.expected_result = "field present and populated (auto-fill)"
                forced_count += 1
            seen_verify_only.add(tc.element_id)
        deduped.append(tc)
    if forced_count:
        print(f"  ⚠ Wall 1.7: coerced {forced_count} case(s) on auto-fill / "
              f"read-only fields to VERIFY_ONLY (protects OCR pre-fills)")
    test_cases = deduped

    # G2 — Type-priority ordering: all dropdowns first, then text fields,
    # then date pickers, uploads, buttons. Within each type, DOM source
    # order. Rationale per user policy:
    #   1. Dropdowns set form state (especially cascade parents and
    #      cross-field constraints like Income Range). Running them
    #      first means text-field tests run against a form with all
    #      dropdowns committed — cleaner validation signal.
    #   2. Python owns the final sort so the LLM's emission order is
    #      irrelevant.
    approach_priority = {
        "select_verify": 0,   # dropdowns
        "fill_check": 1,      # text inputs
        "tap_verify": 2,      # date pickers
        "upload_file": 3,
        "verify_only": 4,     # buttons / readonly existence checks
        "skip": 99,
    }
    priority_rank = {"HIGH": 0, "MED": 1, "LOW": 2}
    field_dom_pos: dict[str, int] = {}
    eid_dom_pos: dict[str, int] = {}
    for i, el in enumerate(l0_filtered):
        if el.name and el.name not in field_dom_pos:
            field_dom_pos[el.name] = i
        if el.element_id:
            eid_dom_pos[el.element_id] = i

    def _dom_position(tc: TestCase) -> int:
        # Prefer exact element_id match (handles repeated field names
        # across sections). Fall back to field_name. Unknown fields
        # sink to the end.
        if tc.element_id in eid_dom_pos:
            return eid_dom_pos[tc.element_id]
        if tc.field_name in field_dom_pos:
            return field_dom_pos[tc.field_name]
        return 10000

    test_cases.sort(key=lambda tc: (
        approach_priority.get(tc.approach.value, 99),
        _dom_position(tc),
        priority_rank.get(tc.priority.value.upper(), 99),
    ))

    print(f"\n  Generated {len(test_cases)} test cases in {duration:.1f}s")

    return PlanOutput(
        test_cases=test_cases,
        coverage_summary=f"{len(test_cases)} cases covering {len(l0_index)} elements",
        model=inp.model,
        cost_usd=plan_logger.budget.current_cost,
        duration_sec=duration,
        raw_plan_text=raw_plan,
    )


_TC_ID_RE = re.compile(r"^TC\d+\b")


def _looks_like_element_id(s: str) -> bool:
    """An element_id is a slug: colon-separated, underscored, no spaces.
    A screen_name (common swap-target) has spaces and no colons."""
    if not s:
        return False
    return ":" in s and " " not in s.strip()


def _looks_like_screen_name(s: str) -> bool:
    """Screen names have spaces, are longer than 10 chars, and don't
    contain colons. The LLM sometimes stuffs the screen name into the
    field_name column when the prompt is ambiguous about where to put it."""
    s = (s or "").strip()
    return bool(s) and " " in s and ":" not in s and len(s) > 10


def _humanize_element_id(element_id: str) -> str:
    """Extract the human-readable label from a slug element_id.
    Format: `screen:label:type` or `screen:section:label:type`.
    Second-to-last segment is the label slug — reverse the slug-normalization
    (underscores → spaces, title-case) to recover a readable name.
    """
    parts = element_id.split(":")
    if len(parts) < 2:
        return element_id
    return parts[-2].replace("_", " ").strip().title()


def _parse_test_plan(raw: str) -> list[TestCase]:
    """Parse the LLM's test plan output into TestCase models.

    Robust to two common LLM deviations:
      • Emitting the literal header row `TC# | Element ID | ...` as a row.
        Solved by requiring TC IDs to match `TC\\d+` (digit after TC).
      • Swapping element_id and field_name columns when the prompt is
        ambiguous about where screen_name belongs. Solved by sniffing the
        content: whichever column has the slug-format string (colons +
        underscores, no spaces) is the element_id.
    """
    test_cases: list[TestCase] = []

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Accept numbered rows too (legacy format), but only valid TC IDs
        # with digits — rejects the literal "TC#" table header.
        is_tc_id_row = bool(_TC_ID_RE.match(line))
        is_numbered_row = line[0].isdigit() and "|" in line
        if not (is_tc_id_row or is_numbered_row):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue

        # Extract TC ID
        tc_id = parts[0].strip().rstrip(".")
        if not tc_id.startswith("TC"):
            tc_id = f"TC{tc_id}"

        # Auto-detect element_id vs field_name columns (see module docstring).
        col1 = parts[1].strip() if len(parts) > 1 else ""
        col2 = parts[2].strip() if len(parts) > 2 else ""
        if _looks_like_element_id(col1):
            element_id, field_name = col1, col2
        elif _looks_like_element_id(col2):
            element_id, field_name = col2, col1
        else:
            element_id, field_name = col1, col2

        # Recover field_name if it got corrupted (common: LLM stuffed the
        # screen name here instead of the human-readable field label).
        # element_id slugs are canonical — we can always derive a clean
        # readable label from them.
        if _looks_like_element_id(element_id):
            if not field_name or _looks_like_screen_name(field_name):
                field_name = _humanize_element_id(element_id)

        # Map approach
        approach_text = parts[3].strip().upper() if len(parts) > 3 else ""
        approach = TestApproach.FILL_CHECK
        if "TAP" in approach_text or "VERIFY" in approach_text:
            approach = TestApproach.TAP_VERIFY
        if "ONLY" in approach_text:
            approach = TestApproach.VERIFY_ONLY
        if "SKIP" in approach_text:
            approach = TestApproach.SKIP
        if "FILL" in approach_text:
            approach = TestApproach.FILL_CHECK
        if "SELECT" in approach_text:
            approach = TestApproach.SELECT_AND_VERIFY
        if "UPLOAD" in approach_text:
            approach = TestApproach.UPLOAD_FILE

        # Map priority
        priority_text = parts[-1].strip().upper() if len(parts) > 6 else "MED"
        priority = TestCasePriority.MED
        if "HIGH" in priority_text:
            priority = TestCasePriority.HIGH
        elif "LOW" in priority_text:
            priority = TestCasePriority.LOW

        # test_value: LLMs often emit the literal word 'EMPTY' (or similar)
        # when they mean an empty string. Translate those markers so the
        # executor actually fills the field with nothing — otherwise the
        # required-field validation never triggers.
        test_value = parts[4].strip() if len(parts) > 4 else ""
        if test_value.upper() in ("EMPTY", "BLANK", "NULL", "NONE", "<EMPTY>", "(EMPTY)"):
            test_value = ""

        test_cases.append(TestCase(
            tc_id=tc_id,
            element_id=element_id,
            field_name=field_name,
            approach=approach,
            test_value=test_value,
            expected_result=parts[5].strip() if len(parts) > 5 else "",
            priority=priority,
        ))

    return test_cases
