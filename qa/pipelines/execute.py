# qa/pipelines/execute.py — Pipeline 3: Test Execution

import json
import time

from agents import Agent

from qa.models import (
    Platform, ExecuteInput, ExecuteOutput, TestSummary,
    KnowledgeBase, PlanInput,
)
from qa.adapters import make_adapter
from qa.knowledge.store import KnowledgeStore
from qa.engine.model_factory import build_model_config, build_model_settings
from qa.engine.orchestrator import run_agent_loop
from qa.prompts.execute_mobile import EXECUTE_MOBILE_PROMPT
from qa.prompts.execute_web import EXECUTE_WEB_PROMPT
from qa.prompts.plan import PLAN_PROMPT


async def run_execute(inp: ExecuteInput) -> ExecuteOutput:
    """Pipeline 3: Execute test cases on the actual app.

    If no knowledge exists and auto_explore=True, runs Pipeline 1 first.
    If no test_cases provided, runs Pipeline 2 first.
    Multi-screen support: navigates between screens via adapter.
    """
    platform = inp.app.platform
    knowledge = inp.knowledge

    # Auto-explore if no knowledge
    if not knowledge.screens and inp.auto_explore:
        print("  No knowledge found — running Explore pipeline first...")
        from qa.pipelines.explore import run_explore
        from qa.models import ExploreInput
        explore_result = await run_explore(ExploreInput(
            app=inp.app,
            screens=inp.screens,
            model=inp.model,
            provider=inp.provider,
        ))
        knowledge = explore_result.knowledge

    # Auto-plan if no test cases
    if not inp.test_cases:
        print("  No test cases provided — running Plan pipeline first...")
        from qa.pipelines.plan import run_plan
        plan_result = await run_plan(PlanInput(
            knowledge=knowledge,
            screen_names=inp.screens,
            element_filter=inp.element_filter,
            test_values_hint=inp.test_values_hint,
            avoid_recent_values=inp.avoid_recent_values,
            model=inp.model,
            provider=inp.provider,
        ))
        test_cases = plan_result.test_cases
        plan_text = plan_result.raw_plan_text
    else:
        test_cases = inp.test_cases
        plan_text = "\n".join(f"{tc.tc_id} | {tc.field_name} | {tc.approach.value} | {tc.test_value}" for tc in test_cases)

    if not test_cases:
        print("  ERROR: No test cases generated")
        return ExecuteOutput(summary=TestSummary(), model=inp.model)

    adapter = make_adapter(platform, device_id=inp.app.device_id or "")

    print(f"\n{'='*60}")
    print(f"  PIPELINE 3: EXECUTE")
    print(f"  Platform: {platform.value}")
    print(f"  Test cases: {len(test_cases)}")
    print(f"  Model: {inp.model}")
    print(f"{'='*60}\n")

    await adapter.launch(inp.app)

    # Optional manual setup pause — useful for multi-step wizards where
    # tests target a downstream page reachable only by manual navigation.
    if getattr(inp, "wait_for_user", False):
        print()
        print("=" * 60)
        print("  PAUSE: browser is open. Do any manual setup now.")
        print("  Navigate to the page you want to test, then press Enter.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to start running tests... ")
        except EOFError:
            pass

    # Select prompt
    exec_prompt = EXECUTE_MOBILE_PROMPT if platform == Platform.MOBILE else EXECUTE_WEB_PROMPT

    model_config, provider_label = build_model_config(inp.model, inp.provider)
    model_settings = build_model_settings(inp.model)
    print(f"  Provider: {provider_label}")

    # Determine screens
    screens = inp.screens or knowledge.screen_names() or ["default"]

    total_cost = 0.0
    total_turns = 0
    total_duration = 0.0
    all_outputs: list[str] = []

    try:
        for i, screen_name in enumerate(screens):
            if len(screens) > 1:
                print(f"\n{'='*60}")
                print(f"  SCREEN {i+1}/{len(screens)}: {screen_name}")
                print(f"{'='*60}")

                if i > 0:
                    await adapter.dismiss_keyboard()
                    nav_ok = await adapter.navigate_to_screen(screen_name)
                    if not nav_ok:
                        print(f"  Could not navigate to {screen_name} — skipping")
                        continue

            # Always navigate to the named screen (even for single-screen runs)
            # This ensures the correct screen is active when screen_name is specified
            if screen_name and screen_name != "default":
                await adapter.dismiss_keyboard()
                await adapter.navigate_to_screen(screen_name)

            # Get L0 for this screen (for context)
            l0_index = knowledge.get_l0_index(screen_name)
            l0_json = json.dumps(
                [el.model_dump(exclude_defaults=True) for el in l0_index],
                indent=2,
            ) if l0_index else "{}"

            # Filter test cases for this screen
            screen_tcs = [tc for tc in test_cases if tc.screen_name == screen_name] or test_cases

            if platform == Platform.MOBILE:
                opening = (
                    f"The app is already open on the {screen_name} screen. "
                    f"Start by calling scan_screen_summary to see the current state."
                )
            else:  # WEB
                opening = (
                    f"The web page is already loaded at {inp.app.url or 'the target URL'}. "
                    f"Start by calling scan_page_summary to see the current state. "
                    f"Then run each test case in the plan via evaluate_script using "
                    f"the templates documented in your instructions. SELECT_AND_VERIFY "
                    f"cases for dropdowns are TWO evaluate_script calls (open+click, then verify) — "
                    f"do NOT skip them."
                )
            task = (
                f"Here is the test plan:\n\n{plan_text}\n\n"
                f"Knowledge for this screen:\n{l0_json}\n\n"
                f"Execute ALL test cases. {opening}"
            )

            mcp_server = adapter.get_mcp_server()

            tools = []
            if platform == Platform.MOBILE:
                from qa.tools.mobile_tools import get_task_tools
                tools = get_task_tools(mcp_server, inp.app.device_id or "")
            elif platform == Platform.WEB:
                from qa.tools.web_tools import get_task_tools, set_kb
                tools = get_task_tools(mcp_server)
                # Inject the active KB so upload_file_for_field can resolve
                # file paths from L0 metadata (semantic_hint + accept).
                set_kb(knowledge, inp.app.app_name)

            agent = Agent(
                name=f"Executor ({screen_name})",
                instructions=exec_prompt,
                model=model_config,
                model_settings=model_settings,
                mcp_servers=[mcp_server],
                tools=tools,
            )

            # Web execution runs each test case as its own evaluate_script turn,
            # so 15 turns can't cover a 20+ case plan. Floor it at 40 for web.
            effective_max_turns = (
                max(inp.max_turns_per_screen, 40)
                if platform == Platform.WEB
                else inp.max_turns_per_screen
            )
            effective_budget = max(inp.budget, 3.0) if platform == Platform.WEB else inp.budget

            result = await run_agent_loop(
                agent=agent,
                task=task,
                max_turns=effective_max_turns,
                model=inp.model,
                budget=effective_budget,
                keep_last_n=1,
                nudge_message=(
                    "IMPORTANT: You have only a few turns left. "
                    "Produce your FINAL REPORT now with TEST CASE RESULTS, "
                    "BUGS FOUND, TEST SUMMARY, and RECOMMENDATIONS."
                ),
            )

            total_cost += result.cost_usd
            total_turns += result.turns_used
            total_duration += result.duration_sec

            # Append the LLM's report PLUS a raw-evidence appendix so users can
            # cross-check every "Actual" claim against the actual tool returns.
            evidence_block = _format_evidence(result.evidence) if result.evidence else ""
            screen_section = f"## Screen: {screen_name}\n\n{result.final_output}"
            if evidence_block:
                screen_section += f"\n\n{evidence_block}"
            all_outputs.append(screen_section)

    finally:
        await adapter.close()

    # Save combined results
    combined_output = "\n\n".join(all_outputs)
    _save_results(inp, combined_output, total_cost, total_turns, total_duration)

    return ExecuteOutput(
        summary=TestSummary(total=len(test_cases)),
        model=inp.model,
        cost_usd=total_cost,
        duration_sec=total_duration,
        turns_used=total_turns,
    )


def _format_evidence(evidence: list[dict]) -> str:
    """Build a RAW EVIDENCE section listing every tool call with args + return.
    Lets users verify the LLM's final report claims against actual tool outputs.
    """
    lines = [
        "## RAW EVIDENCE",
        "",
        "Below is every tool call the agent made, with its actual return value.",
        "Cross-reference any claim in TEST CASE RESULTS above with the matching call here.",
        "",
    ]
    for i, e in enumerate(evidence, start=1):
        turn = e.get("turn", "?")
        tool = e.get("tool", "?")
        args = e.get("args", {})
        result = e.get("result", "")
        # Pick the most informative arg field for the heading
        label = ""
        for key in ("function", "expression", "uid", "css_selector", "url", "value"):
            if key in args:
                v = str(args[key])
                label = v[:120] + ("…" if len(v) > 120 else "")
                break
        lines.append(f"### {i}. Turn {turn} — {tool}({label})")
        if args:
            lines.append("```json")
            lines.append(json.dumps(args, indent=2)[:1500])
            lines.append("```")
        lines.append("**Returned:**")
        lines.append("```")
        lines.append(str(result)[:1500])
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _save_results(inp: ExecuteInput, output: str, cost: float, turns: int, duration: float) -> None:
    """Save execution results to artifacts/results/."""
    from datetime import datetime
    from pathlib import Path

    results_dir = Path("artifacts/results")
    results_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = inp.app.app_name.lower().replace(" ", "_")[:30]
    model_short = inp.model.split("/")[-1].replace("-", "")[:12]

    path = results_dir / f"result_{safe_name}_{model_short}_{timestamp}.txt"
    with open(path, "w") as f:
        f.write(f"EXECUTION REPORT\n{'='*60}\n")
        f.write(f"App: {inp.app.app_name}\n")
        f.write(f"Platform: {inp.app.platform.value}\n")
        f.write(f"Model: {inp.model}\n")
        f.write(f"Turns: {turns}\n")
        f.write(f"Cost: ${cost:.4f}\n")
        f.write(f"Duration: {duration:.1f}s\n")
        f.write(f"{'='*60}\n\n")
        f.write(output)

    print(f"\n  Results saved: {path}")
