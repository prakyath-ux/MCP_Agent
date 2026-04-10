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

            task = (
                f"Explore the current screen of the app. "
                f"Discover all interactive elements and build the knowledge JSON. "
                f"Screen name: {screen_name}. "
                f"App: {inp.app.app_name}."
            )

            # Build agent with MCP server for exploration
            mcp_server = adapter.get_mcp_server()

            # For mobile, use compound tools
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

            result = await run_agent_loop(
                agent=agent,
                task=task,
                max_turns=inp.max_turns,
                model=inp.model,
                budget=inp.budget,
                nudge_message=(
                    "IMPORTANT: You have only a few turns left. "
                    "Extract all element data NOW and produce your KNOWLEDGE JSON."
                ),
            )

            total_cost += result.cost_usd
            total_turns += result.turns_used
            total_duration += result.duration_sec

            # Parse the LLM's knowledge output into our KB model
            if result.final_output:
                screen_kb = _parse_knowledge_output(
                    result.final_output, inp.app, screen_name, platform
                )
                if screen_kb:
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
    path = store.save(knowledge)
    print(f"\n  Knowledge saved: {path}")
    print(f"  Screens: {knowledge.screen_names()}")
    print(f"  Total elements: {sum(len(s.l0) for s in knowledge.screens)}")

    return ExploreOutput(
        knowledge=knowledge,
        delta=delta,
        duration_sec=total_duration,
        cost_usd=total_cost,
        turns_used=total_turns,
        model=inp.model,
    )


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

    # Find the JSON block
    for marker in ["## KNOWLEDGE", "**KNOWLEDGE**", "KNOWLEDGE\n"]:
        idx = raw_output.find(marker)
        if idx != -1:
            break
    if idx == -1:
        print(f"  WARNING: No KNOWLEDGE marker found in output")
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
