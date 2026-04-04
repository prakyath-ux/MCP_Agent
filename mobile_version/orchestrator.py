# mobile_version/orchestrator.py — Managed turn loop for mobile testing agent
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agents import Agent, Runner, ModelSettings
from agents.mcp import MCPServerStdio
from agents.lifecycle import RunHooks
from agents.items import ModelResponse, TResponseInputItem
from agents.run_context import RunContextWrapper
from agents.exceptions import MaxTurnsExceeded
from agents.run_error_handlers import RunErrorHandlerInput, RunErrorHandlerResult
from compound_tools import TASK_TOOLS, SIMPLE_TOOLS, set_server

from config import (
    MODEL, MODEL_PROVIDER, DEVICE_ID, TURN_LIMITS, MAX_BUDGET, BUDGET_WARNING_PCT, PRICING,
    RUNS_DIR, PASS1_RUNS_DIR, PASS2_RUNS_DIR, ELEMENT_NUDGE_BEFORE_END,
    LOOP_WINDOW, LOOP_THRESHOLD, KNOWLEDGE_DIR, KEEP_LAST_N_TURNS,
    PER_TEST_BUDGET, RUNAWAY_MULTIPLIER, TESTCASE_PLAN_MARKER,
)
from prompts import SYSTEM_PROMPT, TESTCASE_PLAN_PROMPT, TESTCASE_EXEC_PROMPT
from compactor import compact_history, update_summary

# Add parent dir to path so we can import state modules
sys.path.insert(0, str(Path(__file__).parent.parent))
from state.usage_tracker import log_usage
from state.tracker import StateTracker


# ── Live Logger (prints per-turn stats to terminal) ───────────────────────────

class LiveTurnLogger(RunHooks):

    def __init__(self) -> None:
        self.turn = 0
        self._header_printed = False
        self.total_input = 0
        self.total_output = 0
        self.total_cached = 0

    async def on_llm_end(
        self, context: RunContextWrapper, agent: Agent, response: ModelResponse
    ) -> None:
        self.turn += 1
        usage = response.usage

        t_input = getattr(usage, "input_tokens", 0) or 0
        t_output = getattr(usage, "output_tokens", 0) or 0
        t_cached = 0
        details = getattr(usage, "input_tokens_details", None)
        if details:
            t_cached = getattr(details, "cached_tokens", 0) or 0

        self.total_input += t_input
        self.total_output += t_output
        self.total_cached += t_cached

        cache_pct = (t_cached / t_input * 100) if t_input > 0 else 0

        # Extract action name — mobile tools use different arg names
        action = "text_output"
        target = ""
        for item in response.output:
            name = getattr(item, "name", None)
            if name:
                action = name.replace("mobile_", "")  # Shorten for display
                args_raw = getattr(item, "arguments", "{}")
                try:
                    args = json.loads(args_raw)
                    # Mobile uses x,y coordinates or packageName
                    if "x" in args and "y" in args:
                        target = f"({args['x']},{args['y']})"
                    elif "packageName" in args:
                        target = args["packageName"][:20]
                    elif "text" in args:
                        target = args["text"][:20]
                    elif "button" in args:
                        target = args["button"]
                    elif "direction" in args:
                        target = args["direction"]
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        if not self._header_printed:
            print(f"  {'Turn':<5} {'Action':<30} {'Target':<20} {'Input':>7} {'Cached':>7} {'Out':>6} {'Cache%':>7}")
            print(f"  {'-'*5} {'-'*30} {'-'*20} {'-'*7} {'-'*7} {'-'*6} {'-'*7}")
            self._header_printed = True

        print(f"  {self.turn:<5} {action:<30} {target:<20} {t_input:>7,} {t_cached:>7,} {t_output:>6,} {cache_pct:>6.0f}%")

    def get_current_cost(self) -> float:
        prices = PRICING.get(MODEL, PRICING["gpt-5"])
        uncached = self.total_input - self.total_cached
        return (
            uncached * prices["input"]
            + self.total_cached * prices["input_cached"]
            + self.total_output * prices["output"]
        )


# ── Main Orchestrated Run ────────────────────────────────────────────────────

async def run_orchestrated(device_id: str, package_name: str, app_name: str, mode: str) -> None:
    max_turns = TURN_LIMITS[mode]

    # ── Select prompt and task based on mode ───────────────────
    knowledge_json = None
    active_prompt = SYSTEM_PROMPT

    if mode == "testcase":
        knowledge_json = _load_knowledge(package_name)
        if not knowledge_json:
            print("  ERROR: No Pass 1 knowledge found for this app.")
            print(f"  Run a 'poc' first to generate knowledge in {KNOWLEDGE_DIR}/")
            return
        active_prompt = TESTCASE_PLAN_PROMPT
        task = (
            f"Here is the knowledge JSON from Pass 1 exploration of {app_name} ({package_name}):\n\n"
            f"{knowledge_json}\n\n"
            "Analyze this and generate a test plan. Output ONLY the ## TEST PLAN section."
        )
    elif mode == "safe_test":
        task = (
            f"The app {app_name} ({package_name}) is open on device {device_id}. "
            "List all elements on screen. Describe what you see. Do NOT interact."
        )
    elif mode == "recon":
        task = (
            f"The app {app_name} ({package_name}) is open on device {device_id}. "
            "List all interactive elements on the current screen. "
            "Do NOT tap or type anything. Just observe and report."
        )
    else:
        task = (
            f"Test the mobile app {app_name} ({package_name}) on device {device_id}. "
            "Explore the current screen, identify all interactive elements, "
            "fill text fields with test data, interact with dropdowns, "
            "and extract element information. "
            "Do NOT press the HOME button or navigate away from the app."
        )

    print(f"\n{'='*60}")
    print(f"  Mode: {mode} (mobile — orchestrated loop)")
    print(f"  Device: {device_id}")
    print(f"  Package: {package_name}")
    print(f"  App: {app_name}")
    print(f"  Model: {MODEL}")
    print(f"  Max turns: {max_turns}")
    print(f"  Budget: ${MAX_BUDGET}")
    print(f"{'='*60}\n")

    # ── Set up model (OpenAI or Groq) ─────────────────────────
    model_config = MODEL
    if MODEL_PROVIDER == "groq":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        groq_client = AsyncOpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
        model_config = OpenAIChatCompletionsModel(
            model=MODEL,
            openai_client=groq_client,
        )
        print(f"  Provider: Groq Cloud (free)")
    elif MODEL_PROVIDER == "openai_chat":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        openai_client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
        )
        model_config = OpenAIChatCompletionsModel(
            model=MODEL,
            openai_client=openai_client,
        )
        print(f"  Provider: OpenAI (Chat Completions API)")

    async with MCPServerStdio(
        name="mobile-mcp",
        params={
            "command": "npx",
            "args": ["-y", "@mobilenext/mobile-mcp@latest"],
        },
        cache_tools_list=True,
        client_session_timeout_seconds=30.0,
    ) as server:

        # Inject MCP server into compound tools
        set_server(server, DEVICE_ID)

        # Pick tools based on provider
        # Task tools work with Chat Completions API (Groq + OpenAI Chat)
        # Simple tools for Responses API (default OpenAI)
        active_tools = TASK_TOOLS if MODEL_PROVIDER in ("groq", "openai_chat") else SIMPLE_TOOLS

        model_tuning = ModelSettings()

        agent = Agent(
            name="Mobile QA Tester" if mode != "testcase" else "Mobile Test Case Runner",
            instructions=active_prompt,
            model=model_config,
            model_settings=model_tuning,
            mcp_servers=[server],
            tools=active_tools,
        )

        logger = LiveTurnLogger()
        tracker = StateTracker()
        tracker.start_run(url=package_name, app_name=app_name)

        print("Agent ready. Starting orchestrated run...\n")
        start_time = time.time()

        # ── The Orchestrated Loop ─────────────────────────────────
        input_items: str | list = task
        rolling_summary = ""
        turn_log: list[dict] = []
        final_output = ""
        agent_done = False

        # ── Phase tracking for testcase mode ──────────────────────
        tc_phase = "2a" if mode == "testcase" else None
        test_plan = ""
        tc_cost_log: list[dict] = []
        tc_turn_start_cost = 0.0

        for turn_num in range(1, max_turns + 1):

            # ── Run exactly ONE turn ──────────────────────────────
            def _on_max_turns(info: RunErrorHandlerInput) -> RunErrorHandlerResult:
                return RunErrorHandlerResult(
                    final_output="__TURN_LIMIT__",
                    include_in_history=False,
                )

            result = await Runner.run(
                starting_agent=agent,
                input=input_items,
                max_turns=1,
                hooks=logger,
                error_handlers={"max_turns": _on_max_turns},
            )

            # ── Check if agent produced final output ──────────────
            if result.final_output and result.final_output != "__TURN_LIMIT__":
                final_output = result.final_output
                agent_done = True

            # ── Extract turn log entry ────────────────────────────
            turn_entry = _extract_turn_log(result, turn_num, turn_log)
            turn_log.append(turn_entry)

            # ── Update rolling summary ────────────────────────────
            full_history = result.to_input_list()
            new_items_count = len(result.new_items) if hasattr(result, "new_items") else 3
            recent = full_history[-new_items_count:] if new_items_count > 0 else []
            rolling_summary = update_summary(rolling_summary, recent)

            # ── Budget check ──────────────────────────────────────
            current_cost = logger.get_current_cost()
            if current_cost >= MAX_BUDGET:
                print(f"\n  BUDGET LIMIT REACHED: ${current_cost:.4f} >= ${MAX_BUDGET}")
                break
            if current_cost >= MAX_BUDGET * BUDGET_WARNING_PCT:
                print(f"\n  BUDGET WARNING: ${current_cost:.4f} ({current_cost/MAX_BUDGET*100:.0f}% of ${MAX_BUDGET})")

            # ── Runaway detection ─────────────────────────────────
            if turn_num > 3:
                avg_cost = current_cost / turn_num
                turn_cost = (turn_entry["uncached_tokens"] * PRICING[MODEL]["input"]
                           + turn_entry["cached_tokens"] * PRICING[MODEL]["input_cached"]
                           + turn_entry["output_tokens"] * PRICING[MODEL]["output"])
                if turn_cost > avg_cost * RUNAWAY_MULTIPLIER:
                    print(f"\n  RUNAWAY WARNING: Turn {turn_num} cost ${turn_cost:.4f} vs avg ${avg_cost:.4f}")

            # ── Phase 2a → 2b switch (testcase mode only) ─────────
            if tc_phase == "2a" and agent_done and TESTCASE_PLAN_MARKER in final_output:
                test_plan = final_output
                final_output = ""
                agent_done = False
                tc_phase = "2b"

                tc_count = sum(1 for line in test_plan.split("\n") if line.strip().startswith("TC"))
                print(f"\n  PHASE 2a COMPLETE: {tc_count} test cases planned")
                print(f"  Switching to Phase 2b (execution)...\n")

                agent = Agent(
                    name="Mobile Test Case Executor",
                    instructions=TESTCASE_EXEC_PROMPT,
                    model=model_config,
                    model_settings=model_tuning,
                    mcp_servers=[server],
                    tools=active_tools,
                )

                input_items = (
                    f"Here is your test plan:\n\n{test_plan}\n\n"
                    f"Execute ALL test cases now on device {device_id}. "
                    f"The app {app_name} ({package_name}) is already open. "
                    "Start by calling mobile_list_elements_on_screen to see the current state."
                )
                rolling_summary = f"Phase 2a: Generated {tc_count} test cases."
                tc_turn_start_cost = current_cost
                continue

            # ── Stop if agent is done ─────────────────────────────
            if agent_done:
                print(f"\n  Agent completed in {turn_num} turns.")
                break

            # ── End-of-run nudge ──────────────────────────────────
            turns_remaining = max_turns - turn_num
            if turns_remaining == ELEMENT_NUDGE_BEFORE_END and not agent_done:
                if mode == "testcase":
                    nudge_msg = (
                        "IMPORTANT: You have only a few turns left. "
                        "Stop running new test cases. Produce your FINAL REPORT now "
                        "with TEST CASE RESULTS, BUGS FOUND, TEST SUMMARY, and "
                        "RECOMMENDATIONS sections."
                    )
                else:
                    nudge_msg = (
                        "IMPORTANT: You have only a few turns left. "
                        "Extract all element data NOW and produce your final report "
                        "with RESULTS, ELEMENTS, KNOWLEDGE, and ISSUES sections."
                    )
                print(f"\n  NUDGE: {turns_remaining} turns left — injecting report reminder")
                full_history.append({
                    "role": "user",
                    "content": nudge_msg,
                })

            # ── Loop detection ────────────────────────────────────
            loop_warning = _detect_loop(turn_log)
            if loop_warning:
                print(f"\n  LOOP DETECTED: {loop_warning}")
                full_history.append({
                    "role": "user",
                    "content": loop_warning,
                })

            # ── Compact history for next turn ─────────────────────
            if tc_phase == "2b":
                keep_n = 1
            else:
                keep_n = KEEP_LAST_N_TURNS
            input_items = compact_history(full_history, rolling_summary, keep_last_n=keep_n)

        # ── End of loop ───────────────────────────────────────────

        duration = time.time() - start_time
        tracker.end_run("completed" if agent_done else "max_turns_reached")

        # ── Save knowledge from Pass 1 ────────────────────────────
        if mode not in ("safe_test", "recon", "testcase") and final_output:
            knowledge_path = _save_knowledge(final_output, package_name, app_name)
            if knowledge_path:
                print(f"  Pass 1 knowledge ready — run 'testcase' mode next")

        # ── Save results ──────────────────────────────────────────
        _save_results(
            mode=mode,
            model=MODEL,
            logger=logger,
            turn_log=turn_log,
            final_output=final_output,
            rolling_summary=rolling_summary,
            duration=duration,
            device_id=device_id,
            package_name=package_name,
            app_name=app_name,
            max_turns=max_turns,
        )


# ── Helper: Extract turn log entry ───────────────────────────────────────────

def _extract_turn_log(result, turn_num: int, prev_log: list[dict]) -> dict:
    t_input = t_output = t_cached = 0
    actions = []
    model_text = ""

    if result.raw_responses:
        response = result.raw_responses[-1]
        usage = getattr(response, "usage", None)
        if usage:
            t_input = getattr(usage, "input_tokens", 0) or 0
            t_output = getattr(usage, "output_tokens", 0) or 0
            details = getattr(usage, "input_tokens_details", None)
            if details:
                t_cached = getattr(details, "cached_tokens", 0) or 0

        for item in getattr(response, "output", []):
            name = getattr(item, "name", None)
            if name:
                args_raw = getattr(item, "arguments", "{}")
                try:
                    args = json.loads(args_raw)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                # Mobile targets: coordinates, text, packageName, button
                if "x" in args and "y" in args:
                    target = f"({args['x']},{args['y']})"
                elif "text" in args:
                    target = str(args["text"])[:40]
                elif "packageName" in args:
                    target = str(args["packageName"])[:40]
                elif "button" in args:
                    target = str(args["button"])
                elif "direction" in args:
                    target = str(args["direction"])
                else:
                    target = ""
                actions.append({"tool": name, "target": target})
            content = getattr(item, "content", None)
            if content and not name:
                for part in (content if isinstance(content, list) else [content]):
                    text = getattr(part, "text", None)
                    if text:
                        model_text = text[:200]

    entry = {
        "turn": turn_num,
        "input_tokens": t_input,
        "cached_tokens": t_cached,
        "uncached_tokens": t_input - t_cached,
        "output_tokens": t_output,
        "cache_hit_ratio": round(t_cached / t_input, 3) if t_input > 0 else 0,
        "actions": actions,
    }
    if model_text:
        entry["thought"] = model_text
    if prev_log:
        entry["new_tokens"] = t_input - prev_log[-1]["input_tokens"]

    return entry


# ── Helper: Loop detection ──────────────────────────────────────────────────

def _detect_loop(turn_log: list[dict]) -> str | None:
    if len(turn_log) < LOOP_THRESHOLD:
        return None

    # Mobile observation tools (don't count as retries)
    observation_tools = {
        "mobile_list_elements_on_screen", "mobile_take_screenshot",
        "mobile_save_screenshot", "mobile_list_available_devices",
        "mobile_get_screen_size", "mobile_get_orientation",
        "mobile_list_apps",
    }

    recent = turn_log[-LOOP_WINDOW:]
    target_counts: dict[str, int] = {}

    for entry in recent:
        for action in entry.get("actions", []):
            tool = action.get("tool", "")
            target = action.get("target", "")
            if tool not in observation_tools and target:
                target_counts[target] = target_counts.get(target, 0) + 1

    for target, count in target_counts.items():
        if count >= LOOP_THRESHOLD:
            return (
                f"LOOP DETECTED: You have attempted '{target}' "
                f"{count} times in the last {LOOP_WINDOW} actions without success. "
                f"This element is not responding. Mark it as FAILED, skip it, "
                f"and move to the next element or produce your final report."
            )

    return None


# ── Helper: Knowledge Storage ────────────────────────────────────────────────

def _save_knowledge(final_output: str, package_name: str, app_name: str) -> str | None:
    if not final_output:
        return None

    knowledge_start = final_output.find("## KNOWLEDGE")
    if knowledge_start == -1:
        knowledge_start = final_output.find("KNOWLEDGE\n")
    if knowledge_start == -1:
        return None

    section = final_output[knowledge_start:]

    # Try code-fenced JSON first
    json_fence_start = section.find("```json")
    if json_fence_start != -1:
        json_fence_start += len("```json")
        json_fence_end = section.find("```", json_fence_start)
        raw_json = section[json_fence_start:json_fence_end].strip() if json_fence_end != -1 else section[json_fence_start:].strip()
    else:
        brace_start = section.find("{")
        if brace_start == -1:
            return None
        depth = 0
        raw_json_end = -1
        for i in range(brace_start, len(section)):
            if section[i] == "{":
                depth += 1
            elif section[i] == "}":
                depth -= 1
                if depth == 0:
                    raw_json_end = i + 1
                    break
        if raw_json_end == -1:
            return None
        raw_json = section[brace_start:raw_json_end].strip()

    try:
        knowledge = json.loads(raw_json)
    except json.JSONDecodeError:
        print(f"  WARNING: Knowledge JSON found but invalid — skipping save")
        return None

    knowledge_dir = Path(__file__).parent / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    safe_name = package_name.replace(".", "_")[:80]
    knowledge_path = knowledge_dir / f"{safe_name}.json"

    knowledge["_meta"] = {
        "package_name": package_name,
        "app_name": app_name,
        "saved_at": datetime.now().isoformat(),
        "source": "pass1_mobile_exploration",
    }

    with open(knowledge_path, "w") as f:
        json.dump(knowledge, f, indent=2)

    print(f"  Knowledge saved: {knowledge_path}")
    return str(knowledge_path)


def _load_knowledge(package_name: str) -> str | None:
    knowledge_dir = Path(__file__).parent / "knowledge"
    if not knowledge_dir.exists():
        return None

    safe_name = package_name.replace(".", "_")[:80]
    knowledge_path = knowledge_dir / f"{safe_name}.json"

    if not knowledge_path.exists():
        for f in knowledge_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("_meta", {}).get("package_name", "") == package_name:
                    knowledge_path = f
                    break
            except (json.JSONDecodeError, KeyError):
                continue
        else:
            return None

    content = knowledge_path.read_text()
    print(f"  Knowledge loaded: {knowledge_path}")
    return content


# ── Helper: Save results ────────────────────────────────────────────────────

def _save_results(
    mode: str,
    model: str,
    logger: LiveTurnLogger,
    turn_log: list[dict],
    final_output: str,
    rolling_summary: str,
    duration: float,
    device_id: str,
    package_name: str,
    app_name: str,
    max_turns: int,
) -> None:
    turns_used = logger.turn

    summary = log_usage(
        run_mode=mode,
        model=model,
        turns_used=turns_used,
        input_tokens=logger.total_input,
        output_tokens=logger.total_output,
        cached_tokens=logger.total_cached,
        duration_sec=duration,
        status="completed",
        notes=f"mobile orchestrated | {app_name} — {package_name} @ {device_id}",
    )

    print(f"\n{'='*60}")
    print("  AGENT OUTPUT")
    print(f"{'='*60}")
    print(final_output or "(no final output — agent may have hit turn/budget limit)")
    print(f"\n{'='*60}")
    print("  USAGE SUMMARY (mobile — orchestrated)")
    print(f"{'='*60}")
    print(f"  Turns used:    {turns_used}")
    print(f"  Input tokens:  {logger.total_input:,}")
    print(f"    Cached:      {logger.total_cached:,} ({summary['cache_hit_pct']:.1f}%)")
    print(f"    Uncached:    {logger.total_input - logger.total_cached:,}")
    print(f"  Output tokens: {logger.total_output:,}")
    print(f"  Real cost:     ${summary['real_cost']:.6f} (₹{summary['real_cost_inr']:.4f})")
    print(f"  No-cache cost: ${summary['nocache_cost']:.6f}")
    print(f"  Savings:       ${summary['savings']:.6f} ({summary['savings_pct']:.1f}%)")
    print(f"  Duration:      {duration:.1f}s")
    print(f"  Summary:       {rolling_summary}")
    print(f"{'='*60}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if mode == "testcase":
        runs_dir = Path(__file__).parent / "runs" / "pass2"
    else:
        runs_dir = Path(__file__).parent / "runs" / "pass1"
    runs_dir.mkdir(parents=True, exist_ok=True)

    output_path = runs_dir / f"output_mobile_{mode}_{timestamp}.txt"
    turns_path = runs_dir / f"turns_mobile_{mode}_{timestamp}.json"

    with open(output_path, "w") as f:
        f.write(f"Mode: {mode}\n")
        f.write(f"Version: mobile (orchestrated loop)\n")
        f.write(f"Device: {device_id}\n")
        f.write(f"Package: {package_name}\n")
        f.write(f"App: {app_name}\n")
        f.write(f"Model: {model}\n")
        f.write(f"Turns: {turns_used} / {max_turns}\n")
        f.write(f"Real Cost: ${summary['real_cost']:.6f} (₹{summary['real_cost_inr']:.4f})\n")
        f.write(f"No-Cache Cost: ${summary['nocache_cost']:.6f}\n")
        f.write(f"Savings: ${summary['savings']:.6f} ({summary['savings_pct']:.1f}%)\n")
        f.write(f"Cache Hit: {summary['cache_hit_pct']:.1f}%\n")
        f.write(f"Duration: {duration:.1f}s\n")
        f.write(f"Rolling Summary: {rolling_summary}\n")
        f.write(f"{'='*60}\n\n")

        f.write("## TURN LOG\n")
        f.write(f"{'Turn':<5} {'Action':<30} {'Target':<20} {'Input':>7} {'Cached':>7} {'New':>6} {'Out':>6} {'Cache%':>7}\n")
        f.write(f"{'-'*5} {'-'*30} {'-'*20} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*7}\n")
        for t in turn_log:
            action = t["actions"][0]["tool"] if t["actions"] else "text_output"
            action = action.replace("mobile_", "")[:28]
            target = t["actions"][0]["target"][:18] if t["actions"] else ""
            new_tok = t.get("new_tokens", "")
            new_str = f"{new_tok:>6,}" if isinstance(new_tok, int) else f"{'—':>6}"
            cache_pct = f"{t['cache_hit_ratio']*100:.0f}%"
            f.write(f"{t['turn']:<5} {action:<30} {target:<20} {t['input_tokens']:>7,} {t['cached_tokens']:>7,} {new_str} {t['output_tokens']:>6,} {cache_pct:>7}\n")
        f.write(f"\n{'='*60}\n\n")

        f.write(final_output or "(no final output)")

    with open(turns_path, "w") as f:
        json.dump({
            "version": "mobile",
            "model": model,
            "mode": mode,
            "device_id": device_id,
            "package_name": package_name,
            "rolling_summary": rolling_summary,
            "turns": turn_log,
        }, f, indent=2)

    print(f"\n  Output saved:  {output_path}")
    print(f"  Turn log:      {turns_path}")
