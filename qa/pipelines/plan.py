# qa/pipelines/plan.py — Pipeline 2: Test Case Planning (pure LLM, no MCP)

import json
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

    # Filter out non-testable elements (nav tabs, back buttons, headers, decorative)
    skip_types = {"nav_tab", "other"}
    skip_names = {"back", "backbutton", "headerimage", "texture_view"}
    l0_filtered = [
        el for el in l0_index
        if el.type.value not in skip_types
        and el.name.lower().replace(" ", "") not in skip_names
    ]

    if not l0_filtered:
        print("  ERROR: No testable elements after filtering")
        return PlanOutput(model=inp.model)

    l0_json = json.dumps(
        [el.model_dump(exclude_defaults=True) for el in l0_filtered],
        indent=2,
    )
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

    task = (
        f"Here is the L0 knowledge index for the application:\n\n"
        f"{l0_json}\n\n"
        f"Generate a test plan with {inp.max_total_cases} or fewer test cases, "
        f"max {inp.max_cases_per_field} per field. "
        f"Output ONLY the ## TEST PLAN section."
    )

    agent = Agent(
        name="QA Test Planner",
        instructions=PLAN_PROMPT,
        model=model_config,
        # No model_settings — no tools means parallel_tool_calls can't be set
        # No mcp_servers — pure planning
    )

    start_time = time.time()
    result = await Runner.run(agent, input=task, max_turns=1)
    duration = time.time() - start_time

    raw_plan = result.final_output or ""
    test_cases = _parse_test_plan(raw_plan)

    print(f"\n  Generated {len(test_cases)} test cases in {duration:.1f}s")

    return PlanOutput(
        test_cases=test_cases,
        coverage_summary=f"{len(test_cases)} cases covering {len(l0_index)} elements",
        model=inp.model,
        cost_usd=0.0,  # Minimal — one LLM call
        duration_sec=duration,
        raw_plan_text=raw_plan,
    )


def _parse_test_plan(raw: str) -> list[TestCase]:
    """Parse the LLM's test plan output into TestCase models."""
    test_cases: list[TestCase] = []

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Match lines starting with TC or a number followed by |
        if not (line.startswith("TC") or (line[0].isdigit() and "|" in line)):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue

        # Extract TC ID
        tc_id = parts[0].strip().rstrip(".")
        if not tc_id.startswith("TC"):
            tc_id = f"TC{tc_id}"

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

        # Map priority
        priority_text = parts[-1].strip().upper() if len(parts) > 6 else "MED"
        priority = TestCasePriority.MED
        if "HIGH" in priority_text:
            priority = TestCasePriority.HIGH
        elif "LOW" in priority_text:
            priority = TestCasePriority.LOW

        test_cases.append(TestCase(
            tc_id=tc_id,
            element_id=parts[1].strip() if len(parts) > 1 else "",
            field_name=parts[2].strip() if len(parts) > 2 else "",
            approach=approach,
            test_value=parts[4].strip() if len(parts) > 4 else "",
            expected_result=parts[5].strip() if len(parts) > 5 else "",
            priority=priority,
        ))

    return test_cases
