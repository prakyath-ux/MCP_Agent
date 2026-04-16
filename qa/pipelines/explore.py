# qa/pipelines/explore.py — Pipeline 1: Discovery / Exploration

import json

from agents import Agent

from qa.models import (
    Platform, ExploreInput, ExploreOutput, KnowledgeBase,
)
from qa.adapters import make_adapter
from qa.knowledge.store import KnowledgeStore
from qa.knowledge.converter import convert_mobile_knowledge, convert_web_knowledge
from qa.engine.model_factory import build_model_config, build_model_settings
from qa.engine.orchestrator import run_agent_loop
from qa.prompts.explore_mobile import EXPLORE_MOBILE_PROMPT
from qa.prompts.explore_web import EXPLORE_WEB_PROMPT


async def run_explore(inp: ExploreInput) -> ExploreOutput:
    """Pipeline 1: Discover screens and elements, build knowledge base.

    Flow:
    1. Create adapter (web or mobile)
    2. Launch the app
    3. For each screen: LLM explores, extracts elements
    4. Parse LLM output into KnowledgeBase
    5. Compute delta if existing knowledge provided
    6. Save to artifacts/knowledge/
    """
    platform = inp.app.platform
    adapter = make_adapter(platform, device_id=inp.app.device_id or "")

    # Preload any existing KB so new screens MERGE with previous ones instead
    # of overwriting. This prevents losing prior-page data when running
    # explore with --wait on a downstream page of the same app.
    from qa.knowledge.store import KnowledgeStore as _KS
    _store_preload = _KS()
    if inp.existing_knowledge is None:
        preloaded = _store_preload.load(inp.app)
        if preloaded and preloaded.screens:
            print(f"  Existing KB found with {len(preloaded.screens)} screen(s): "
                  f"{[s.screen_name for s in preloaded.screens]}")
            print(f"  New screens will be MERGED into this KB.")
            inp.existing_knowledge = preloaded

    print(f"\n{'='*60}")
    print(f"  PIPELINE 1: EXPLORE")
    print(f"  Platform: {platform.value}")
    print(f"  Target: {inp.app.url or inp.app.package_name}")
    print(f"  Model: {inp.model}")
    print(f"{'='*60}\n")

    await adapter.launch(inp.app)

    # Optional manual setup pause — useful for multi-step wizards where the
    # downstream page can only be reached by manually filling/submitting prior
    # pages. User does the navigation in the browser, then hits Enter.
    if getattr(inp, "wait_for_user", False):
        print()
        print("=" * 60)
        print("  PAUSE: browser is open. Do any manual setup now.")
        print("  When the page you want to explore is on screen, press Enter.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to start exploring... ")
        except EOFError:
            pass

    # Select prompt based on platform
    prompt = EXPLORE_MOBILE_PROMPT if platform == Platform.MOBILE else EXPLORE_WEB_PROMPT

    model_config, provider_label = build_model_config(inp.model, inp.provider)
    model_settings = build_model_settings(inp.model)
    print(f"  Provider: {provider_label}")

    # Determine screens to explore
    screens = inp.screens or ["default"]

    knowledge = KnowledgeBase(app=inp.app)
    total_cost = 0.0
    total_turns = 0
    total_duration = 0.0

    try:
        for i, screen_name in enumerate(screens):
            if len(screens) > 1:
                print(f"\n  ── Screen {i+1}/{len(screens)}: {screen_name} ──")
                if i > 0:
                    await adapter.dismiss_keyboard()
                    nav_ok = await adapter.navigate_to_screen(screen_name)
                    if not nav_ok:
                        print(f"  Could not navigate to {screen_name} — skipping")
                        continue

            if platform == Platform.MOBILE:
                task = (
                    f"The app '{inp.app.app_name}' is ALREADY OPEN on the '{screen_name}' screen.\n"
                    f"DO NOT call mobile_launch_app, mobile_list_apps, or mobile_list_available_devices. "
                    f"These tools are forbidden for this task — the app is already running.\n\n"
                    f"Start directly with scan_screen_summary() to see what's on screen.\n"
                    f"Then open EVERY dropdown (test_dropdown with select_option) and record ALL options returned.\n"
                    f"Then test the date picker, then fill the text field.\n\n"
                    f"YOUR FINAL RESPONSE MUST end with exactly this format:\n\n"
                    f"## KNOWLEDGE\n"
                    f"```json\n"
                    f"{{...complete knowledge JSON here...}}\n"
                    f"```\n\n"
                    f"Without the literal text '## KNOWLEDGE' heading above the JSON block, "
                    f"the data will be discarded. Every dropdown's dropdown_options array MUST be filled."
                )
            else:  # WEB
                # Discover available test files so the agent can coordinate
                # dropdown option selection with the right file to upload.
                from pathlib import Path
                from qa.knowledge.file_resolver import _safe_app_dir
                test_files_root = Path("artifacts/test_files").resolve()
                app_files_dir = test_files_root / _safe_app_dir(inp.app.app_name)
                app_files = []
                if app_files_dir.exists():
                    app_files = sorted(
                        f.name for f in app_files_dir.iterdir()
                        if f.is_file() and f.suffix.lower() != ".json"
                    )
                global_files = []
                gdir = test_files_root / "global"
                if gdir.exists():
                    global_files = sorted(
                        f.name for f in gdir.iterdir()
                        if f.is_file() and f.suffix.lower() != ".json" and f.name != "README.md"
                    )
                files_block = ""
                if app_files or global_files:
                    files_block = (
                        "\nAVAILABLE TEST FILES for upload_file_for_field:\n"
                    )
                    if app_files:
                        files_block += f"  In artifacts/test_files/{_safe_app_dir(inp.app.app_name)}/:\n"
                        for f in app_files:
                            files_block += f"    - {f}\n"
                    if global_files:
                        files_block += f"  In artifacts/test_files/global/ (fallback):\n"
                        for f in global_files:
                            files_block += f"    - {f}\n"
                    files_block += (
                        "When calling upload_file_for_field, pass file_name=\"<exact name>\"\n"
                        "to pair the right file with the dropdown option you selected.\n"
                        "Example: if you selected 'Passport' from a dropdown, pass file_name=\"passport.png\".\n"
                    )

                task = (
                    f"The web app '{inp.app.app_name}' is ALREADY LOADED in Chrome at {inp.app.url or 'the target URL'}.\n"
                    f"DO NOT call list_pages, select_page, new_page, or close_page — one tab, already correct.\n\n"
                    f"## YOUR COMPLETE EXPLORATION CHECKLIST\n"
                    f"You MUST complete ALL applicable items below BEFORE outputting KNOWLEDGE.\n"
                    f"Do NOT short-circuit after the first dropdown or first section.\n\n"
                    f"1. take_snapshot — see the initial page state\n"
                    f"2. Run the XPath+CSS extraction evaluate_script ONCE\n"
                    f"3. For EACH visible interactive element (iterate until all done):\n"
                    f"   - text input → fill(uid, value); if React reverts it, use native-setter evaluate_script\n"
                    f"   - custom dropdown → click(uid) to open, snapshot to capture options, click an option to close\n"
                    f"   - file_upload → first try evaluate_script to read input[type=file] attrs (no click).\n"
                    f"     If the page is GATED (next section/tab only unlocks after upload), call\n"
                    f"     upload_file_for_field(field_name=\"<visible label>\", file_name=\"<exact file from list below>\")\n"
                    f"   ⚠ NEVER call the raw `upload_file` MCP tool for uploads. You MUST use\n"
                    f"     the compound `upload_file_for_field` tool — it's the ONLY one that\n"
                    f"     handles: modal cascade, hidden-input exposure, OCR-verification wait\n"
                    f"     (up to 30s), and success-signal detection. Raw upload_file skips all\n"
                    f"     of that and leaves the app in an incomplete state (error toast, no OCR).\n"
                    f"4. TAB-WALKING: if you see numbered tabs (1., 2., 3., \"Tab A\", \"Tab B\") or stepper buttons,\n"
                    f"   click EACH TAB IN SEQUENCE and repeat step 3 for that tab's newly-visible content.\n"
                    f"   Do NOT output KNOWLEDGE until every tab has been explored.\n"
                    f"5. POST-UPLOAD: after upload_file_for_field returns PASS, take_snapshot.\n"
                    f"   Newly-rendered conditional sections (autofill panels) will now be visible —\n"
                    f"   capture them as additional elements in your KNOWLEDGE.\n"
                    f"6. Only output KNOWLEDGE when ALL tabs/sections are captured.\n\n"
                    f"DO NOT click Submit / Save & Continue / Save & Exit / Log In — they navigate away.\n"
                    f"DO NOT invent elements you didn't see in a snapshot or evaluate_script result.\n"
                    f"{files_block}\n"
                    f"YOUR FINAL RESPONSE MUST end with exactly this format:\n\n"
                    f"## KNOWLEDGE\n"
                    f"```json\n"
                    f"{{...complete knowledge JSON here...}}\n"
                    f"```\n\n"
                    f"Without the literal text '## KNOWLEDGE' heading above the JSON block,\n"
                    f"the data will be discarded. Every dropdown's dropdown_options array MUST be filled\n"
                    f"with real option texts observed from the popup snapshot."
                )

            # Build agent with MCP server for exploration
            mcp_server = adapter.get_mcp_server()

            # Compound tools per platform
            # Mobile: compound tools handle keyboard/coordinate quirks.
            # Web: raw Chrome MCP + a few helpers. upload_file_for_field is
            # registered so explore can advance gated pages (TECU-style KYC).
            tools = []
            if platform == Platform.MOBILE:
                from qa.tools.mobile_tools import get_explore_tools
                tools = get_explore_tools(mcp_server, inp.app.device_id or "")
            elif platform == Platform.WEB:
                from qa.tools.web_tools import get_explore_tools, set_kb
                tools = get_explore_tools(mcp_server)
                # Seed the compound tool with KB so far so upload_file_for_field
                # can look up semantic_hint for the currently-visible element.
                # If this is the first screen, KB will be mostly empty.
                set_kb(knowledge, inp.app.app_name)

            agent = Agent(
                name=f"Explorer ({screen_name})",
                instructions=prompt,
                model=model_config,
                model_settings=model_settings,
                mcp_servers=[mcp_server],
                tools=tools,
            )

            # Web needs more room: React/async/fallback-ladder cycles burn turns.
            effective_max_turns = max(inp.max_turns, 40) if platform == Platform.WEB else inp.max_turns
            effective_budget = max(inp.budget, 3.0) if platform == Platform.WEB else inp.budget

            result = await run_agent_loop(
                agent=agent,
                task=task,
                max_turns=effective_max_turns,
                model=inp.model,
                budget=effective_budget,
                nudge_message=(
                    "IMPORTANT: You have only a few turns left. "
                    "Extract all element data NOW and produce your KNOWLEDGE JSON."
                ),
            )

            total_cost += result.cost_usd
            total_turns += result.turns_used
            total_duration += result.duration_sec

            # Parse the LLM's knowledge output into our KB model
            if not result.final_output:
                print(f"  ⚠ No final output from LLM for {screen_name} — skipping save")
                continue

            screen_kb = _parse_knowledge_output(
                result.final_output, inp.app, screen_name, platform
            )
            if not screen_kb:
                print(f"  ⚠ Could not parse KNOWLEDGE block for {screen_name}")
                print(f"    Final output (first 500 chars): {result.final_output[:500]}")
                continue

            if True:  # keep existing validation flow indented
                    # Validate before accepting
                    issues = _validate_screen_knowledge(screen_kb)
                    if issues:
                        print(f"\n  ⚠ Knowledge validation failed for {screen_name}:")
                        for issue in issues:
                            print(f"    - {issue}")
                        print(f"  Retrying with specific guidance...")

                        # One retry with pointed feedback
                        retry_task = (
                            f"Your previous KNOWLEDGE output was incomplete. "
                            f"Fix these issues:\n"
                            + "\n".join(f"  - {i}" for i in issues)
                            + "\n\nRe-open the specific elements if needed. "
                            f"Then output a corrected ## KNOWLEDGE JSON block."
                        )
                        retry_result = await run_agent_loop(
                            agent=agent,
                            task=retry_task,
                            max_turns=10,
                            model=inp.model,
                            budget=inp.budget / 2,
                        )
                        total_cost += retry_result.cost_usd
                        total_turns += retry_result.turns_used
                        total_duration += retry_result.duration_sec

                        if retry_result.final_output:
                            retry_kb = _parse_knowledge_output(
                                retry_result.final_output, inp.app, screen_name, platform
                            )
                            if retry_kb:
                                retry_issues = _validate_screen_knowledge(retry_kb)
                                if not retry_issues:
                                    print(f"  ✓ Validation passed after retry")
                                    screen_kb = retry_kb
                                else:
                                    print(f"  ⚠ Still has {len(retry_issues)} issue(s) after retry — saving anyway")

                    for screen in screen_kb.screens:
                        knowledge.screens.append(screen)

    finally:
        await adapter.close()

    # Compute delta if existing knowledge provided
    delta = None
    store = KnowledgeStore()
    if inp.existing_knowledge:
        delta = store.compute_delta(inp.existing_knowledge, knowledge)
        knowledge = store.merge(inp.existing_knowledge, knowledge)

    # Save
    total_elements = sum(len(s.l0) for s in knowledge.screens)
    if total_elements > 0:
        path = store.save(knowledge)
        print(f"\n  Knowledge saved: {path}")
        print(f"  Screens: {knowledge.screen_names()}")
        print(f"  Total elements: {total_elements}")
    else:
        print(f"\n  NOT SAVED — no elements captured. The LLM did not produce usable knowledge.")

    return ExploreOutput(
        knowledge=knowledge,
        delta=delta,
        duration_sec=total_duration,
        cost_usd=total_cost,
        turns_used=total_turns,
        model=inp.model,
    )


def _validate_screen_knowledge(kb) -> list[str]:
    """Validate that each screen's L0 is complete enough for planning.

    Returns a list of issue strings. Empty list = valid.
    """
    issues: list[str] = []
    for screen in kb.screens:
        if not screen.l0:
            issues.append(f"Screen '{screen.screen_name}' has no elements")
            continue

        for el in screen.l0:
            # Dropdowns must have at least 1 option
            if el.type.value == "dropdown" and not el.options:
                issues.append(
                    f"Dropdown '{el.name}' has empty options[] — you must call test_dropdown to capture all options"
                )
            # Buttons whose name starts with "Select"/"Choose"/"Pick" are almost
            # certainly dropdowns in disguise. Flag and demand re-exploration.
            name_lower = el.name.lower()
            if el.type.value == "button" and any(
                name_lower.startswith(p) for p in ("select ", "choose ", "pick ")
            ):
                issues.append(
                    f"Element '{el.name}' is classified as button but looks like a dropdown — "
                    f"reclassify as type='dropdown' and call test_dropdown to capture its options"
                )
            # Required fields should have behavior description
            if el.required and not el.behavior:
                issues.append(
                    f"Required element '{el.name}' has no behavior description"
                )

    return issues


def _parse_knowledge_output(
    raw_output: str,
    app,
    screen_name: str,
    platform: Platform,
) -> KnowledgeBase | None:
    """Parse the LLM's final output into a KnowledgeBase.

    The LLM outputs a KNOWLEDGE JSON block. We extract it and convert
    using the same converter logic as the flat JSON migration.
    """
    import tempfile
    from pathlib import Path

    # Find the JSON block — try many marker variations
    idx = -1
    for marker in ["## KNOWLEDGE", "**KNOWLEDGE**", "### KNOWLEDGE", "# KNOWLEDGE",
                   "KNOWLEDGE:", "Knowledge:", "KNOWLEDGE\n", "## Knowledge",
                   "**Knowledge**"]:
        idx = raw_output.find(marker)
        if idx != -1:
            break

    # Fallback: look for any JSON code block with our expected keys
    if idx == -1:
        import re
        # Find ```json ... ``` blocks
        json_blocks = re.findall(r"```(?:json)?\s*(\{[^`]*?\})\s*```", raw_output, re.DOTALL)
        for block in json_blocks:
            if '"elements"' in block or '"screen_title"' in block:
                raw_output = "KNOWLEDGE\n```json\n" + block + "\n```"
                idx = 0
                print(f"  Parsed KNOWLEDGE JSON via fallback (no marker found, matched by content)")
                break

    if idx == -1:
        print(f"  WARNING: No KNOWLEDGE marker found in output")
        print(f"    Output (last 500 chars): {raw_output[-500:]}")
        return None

    section = raw_output[idx:]

    # Extract JSON
    json_fence = section.find("```json")
    if json_fence != -1:
        json_start = json_fence + len("```json")
        json_end = section.find("```", json_start)
        raw_json = section[json_start:json_end].strip() if json_end != -1 else section[json_start:].strip()
    else:
        brace_start = section.find("{")
        if brace_start == -1:
            return None
        depth = 0
        raw_json_end = -1
        for i in range(brace_start, len(section)):
            if section[i] == "{": depth += 1
            elif section[i] == "}":
                depth -= 1
                if depth == 0:
                    raw_json_end = i + 1
                    break
        if raw_json_end == -1:
            return None
        raw_json = section[brace_start:raw_json_end]

    try:
        json.loads(raw_json)  # Validate
    except json.JSONDecodeError:
        print(f"  WARNING: Knowledge JSON invalid — skipping")
        return None

    # Write to temp file and use converter
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        # Inject meta
        data = json.loads(raw_json)
        data["_meta"] = {
            "package_name": app.package_name or "",
            "app_name": app.app_name,
            "screen_name": screen_name,
            "source": "explore_pipeline",
        }
        json.dump(data, f)
        tmp_path = Path(f.name)

    try:
        if platform == Platform.MOBILE:
            return convert_mobile_knowledge(tmp_path)
        else:
            return convert_web_knowledge(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
