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

    print(f"\n{'='*60}")
    print(f"  PIPELINE 1: EXPLORE")
    print(f"  Platform: {platform.value}")
    print(f"  Target: {inp.app.url or inp.app.package_name}")
    print(f"  Model: {inp.model}")
    print(f"{'='*60}\n")

    await adapter.launch(inp.app)

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
                task = (
                    f"The web app '{inp.app.app_name}' is ALREADY LOADED in Chrome at {inp.app.url or 'the target URL'}.\n"
                    f"DO NOT call list_pages, select_page, new_page, or close_page — there is one tab and it is correct.\n\n"
                    f"Start with take_snapshot to see the page. Then run the XPath+CSS extraction "
                    f"evaluate_script ONCE. Then click EVERY custom dropdown ('Select X', 'Choose X') "
                    f"to open it and capture ALL options. Then fill EVERY text field with a test value "
                    f"to observe validation. If fill doesn't stick (React controlled input), drop to "
                    f"evaluate_script with the native setter pattern.\n\n"
                    f"Do NOT click Submit, Save & Continue, or Save & Exit — they navigate away.\n\n"
                    f"YOUR FINAL RESPONSE MUST end with exactly this format:\n\n"
                    f"## KNOWLEDGE\n"
                    f"```json\n"
                    f"{{...complete knowledge JSON here...}}\n"
                    f"```\n\n"
                    f"Without the literal text '## KNOWLEDGE' heading above the JSON block, "
                    f"the data will be discarded. Every dropdown's dropdown_options array MUST be filled with real option texts."
                )

            # Build agent with MCP server for exploration
            mcp_server = adapter.get_mcp_server()

            # Compound tools per platform
            # Mobile: compound tools handle keyboard/coordinate quirks — they work well.
            # Web: raw Chrome DevTools MCP tools only (version_2 philosophy). The LLM
            #      needs to adapt at the JS level for React/custom-dropdown quirks
            #      that no compound tool can anticipate. The fat prompt teaches patterns.
            tools = []
            if platform == Platform.MOBILE:
                from qa.tools.mobile_tools import get_explore_tools
                tools = get_explore_tools(mcp_server, inp.app.device_id or "")

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
