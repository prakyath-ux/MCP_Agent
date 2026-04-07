# mobile_version/orchestrator.py — Managed turn loop for mobile testing agent
import os
import sys
import json
import time
import asyncio
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
from compound_tools import TASK_TOOLS, SIMPLE_TOOLS, EXPLORE_TOOLS, set_server, clear_caches

from config import (
    MODEL as DEFAULT_MODEL, MODEL_PROVIDER as DEFAULT_PROVIDER,
    DEVICE_ID, TURN_LIMITS, MAX_BUDGET, BUDGET_WARNING_PCT, PRICING,
    RUNS_DIR, PASS1_RUNS_DIR, PASS2_RUNS_DIR, ELEMENT_NUDGE_BEFORE_END,
    LOOP_WINDOW, LOOP_THRESHOLD, KNOWLEDGE_DIR, KEEP_LAST_N_TURNS,
    PER_TEST_BUDGET, RUNAWAY_MULTIPLIER, TESTCASE_PLAN_MARKER,
    NAV_TAB_COORDINATES, SCREEN_NAME_MAP,
    SAFE_TAP_X, SAFE_TAP_Y,
)
from prompts import SYSTEM_PROMPT, TESTCASE_PLAN_PROMPT, TESTCASE_EXEC_PROMPT
from compactor import compact_history, update_summary

# Add parent dir to path so we can import state modules
sys.path.insert(0, str(Path(__file__).parent.parent))
from state.usage_tracker import log_usage
from state.tracker import StateTracker


# ── Live Logger (prints per-turn stats to terminal) ───────────────────────────

class LiveTurnLogger(RunHooks):

    def __init__(self, model: str = "") -> None:
        self.turn = 0
        self._model = model
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

    def get_current_cost(self, model: str = "") -> float:
        prices = PRICING.get(model or self._model, PRICING["gpt-5"])
        uncached = self.total_input - self.total_cached
        return (
            uncached * prices["input"]
            + self.total_cached * prices["input_cached"]
            + self.total_output * prices["output"]
        )


# ── Helper: Build model config ────────────────────────────────────────────────

def _build_model_config(model: str, provider: str):
    """Build the model config object based on provider. Returns (model_config, provider_label)."""
    if provider == "groq":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        client = AsyncOpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
        return OpenAIChatCompletionsModel(model=model, openai_client=client), "Groq Cloud (free)"
    elif provider == "openai_chat":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
        return OpenAIChatCompletionsModel(model=model, openai_client=client), "OpenAI (Chat Completions API)"
    elif provider == "openrouter":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        client = AsyncOpenAI(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
        )
        return OpenAIChatCompletionsModel(model=model, openai_client=client), "OpenRouter (cheap, no daily limit)"
    else:
        return model, "OpenAI (Responses API)"


# ── Helper: Navigate to screen ────────────────────────────────────────────────

async def _navigate_to_screen(server: MCPServerStdio, device_id: str, screen_name: str) -> bool:
    """Tap the nav tab for the given screen."""
    nav_label = SCREEN_NAME_MAP.get(screen_name.lower(), screen_name.upper())
    coords = NAV_TAB_COORDINATES.get(nav_label)

    if not coords:
        print(f"  WARNING: No nav coordinates for '{screen_name}' (label: {nav_label})")
        return False

    # Tap the nav tab
    x, y = coords
    print(f"  Tapping {nav_label} tab at ({x}, {y})...")
    await server.call_tool("mobile_click_on_screen_at_coordinates", {"device": device_id, "x": x, "y": y})
    await asyncio.sleep(1.5)

    # Verify screen changed
    result = await server.call_tool("mobile_list_elements_on_screen", {"device": device_id})
    screen_text = result.content[0].text.lower() if result.content else ""

    if nav_label.lower() in screen_text or screen_name.lower() in screen_text:
        print(f"  Navigated to {nav_label}")
        return True

    # Retry once
    print(f"  Screen may not have changed — retrying...")
    await server.call_tool("mobile_click_on_screen_at_coordinates", {"device": device_id, "x": x, "y": y})
    await asyncio.sleep(1.5)
    print(f"  Navigated to {nav_label} (retry)")
    return True


# ── Helper: Cleanup between screens ──────────────────────────────────────────

async def _cleanup_screen(server: MCPServerStdio, device_id: str) -> None:
    """Dismiss keyboard, close popups/dialogs, verify screen is clean before navigating."""
    print(f"\n  ── Screen cleanup ──")

    # Step 1: ADB keyevent 111 (ESCAPE) — dismisses keyboard without navigation
    print(f"  Step 1: Dismissing keyboard (ADB ESCAPE)...")
    import subprocess
    subprocess.run(["adb", "-s", device_id, "shell", "input", "keyevent", "111"],
                   capture_output=True, timeout=5)
    await asyncio.sleep(0.8)

    # Step 2: Scan to check state — is keyboard gone? any dialogs open?
    print(f"  Step 2: Scanning screen state...")
    result = await server.call_tool("mobile_list_elements_on_screen", {"device": device_id})
    raw_text = result.content[0].text if result.content else ""

    # Check content frame height to detect keyboard
    import json as _json
    try:
        elements = _json.loads(raw_text[raw_text.index("["):])
        content_el = next((e for e in elements if e.get("identifier") == "android:id/content"), None)
        content_height = content_el.get("coordinates", {}).get("height", 9999) if content_el else 9999
        keyboard_still_up = content_height < 2150
    except (ValueError, _json.JSONDecodeError):
        keyboard_still_up = False

    if keyboard_still_up:
        print(f"  Keyboard still up — sending ESCAPE again...")
        subprocess.run(["adb", "-s", device_id, "shell", "input", "keyevent", "111"],
                       capture_output=True, timeout=5)
        await asyncio.sleep(0.8)

    # Step 3: Check for open dialogs/popups
    screen_text = raw_text.lower()
    dialog_signs = ["cancel", "dismiss", "close", "confirm", "select date"]
    has_dialog = any(sign in screen_text for sign in dialog_signs)

    if has_dialog:
        print(f"  Found open dialog — sending ESCAPE to close...")
        subprocess.run(["adb", "-s", device_id, "shell", "input", "keyevent", "111"],
                       capture_output=True, timeout=5)
        await asyncio.sleep(0.5)

    # Step 4: Verify nav bar is visible at normal position (y > 2150)
    print(f"  Step 3: Verifying nav bar visible...")
    result = await server.call_tool("mobile_list_elements_on_screen", {"device": device_id})
    screen_text = result.content[0].text.lower() if result.content else ""

    has_nav = "dashboard" in screen_text and ("iteller" in screen_text or "loan" in screen_text)
    if has_nav:
        print(f"  Screen clean — nav bar visible")
    else:
        print(f"  ⚠ Nav bar not visible — will attempt navigation anyway")


# ── Single Screen Run ─────────────────────────────────────────────────────────

async def _run_single_screen(
    server: MCPServerStdio,
    device_id: str,
    package_name: str,
    app_name: str,
    mode: str,
    screen_name: str,
    model: str,
    model_config,
    provider: str,
) -> dict:
    """Run a single screen test. Returns summary dict."""
    max_turns = TURN_LIMITS[mode]

    # Clear coordinate caches from previous screen
    clear_caches()
    set_server(server, device_id)

    # ── Select prompt and task based on mode ───────────────────
    knowledge_json = None
    active_prompt = SYSTEM_PROMPT

    if mode == "testcase":
        knowledge_json = _load_knowledge(package_name, screen_name)
        if not knowledge_json:
            print(f"  ERROR: No Pass 1 knowledge for screen '{screen_name}'.")
            print(f"  Run a 'poc' first to generate knowledge in {KNOWLEDGE_DIR}/")
            return {"screen": screen_name, "status": "skipped_no_knowledge", "turns": 0, "cost": 0, "duration": 0}
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

    # Pick tools based on mode + provider
    if mode in ("poc", "safe_test", "recon"):
        active_tools = EXPLORE_TOOLS
    elif provider in ("groq", "openai_chat", "openrouter"):
        active_tools = TASK_TOOLS
    else:
        active_tools = SIMPLE_TOOLS

    # GPT-5.1 defaults to parallel tool calls which breaks execution
    if "gpt-5.1" in model:
        model_tuning = ModelSettings(parallel_tool_calls=False)
    else:
        model_tuning = ModelSettings()

    agent = Agent(
        name="Mobile QA Tester" if mode != "testcase" else "Mobile Test Case Runner",
        instructions=active_prompt,
        model=model_config,
        model_settings=model_tuning,
        mcp_servers=[server],
        tools=active_tools,
    )

    logger = LiveTurnLogger(model=model)
    tracker = StateTracker()
    tracker.start_run(url=package_name, app_name=app_name)

    print("  Agent ready. Starting orchestrated run...\n")
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

    try:
        for turn_num in range(1, max_turns + 1):

            # ── Run exactly ONE turn ──────────────────────────────
            def _on_max_turns(info: RunErrorHandlerInput) -> RunErrorHandlerResult:
                return RunErrorHandlerResult(
                    final_output="__TURN_LIMIT__",
                    include_in_history=False,
                )

            t_start = time.time()
            print(f"\n  ⏱ Turn {turn_num}: sending to API...", end="", flush=True)

            result = await Runner.run(
                starting_agent=agent,
                input=input_items,
                max_turns=1,
                hooks=logger,
                error_handlers={"max_turns": _on_max_turns},
            )

            t_elapsed = time.time() - t_start
            print(f" done ({t_elapsed:.1f}s)")
            if t_elapsed > 30:
                print(f"  ⚠ SLOW RESPONSE: {t_elapsed:.1f}s — likely API latency, not our code")

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
                prices = PRICING.get(model, PRICING["gpt-5"])
                turn_cost = (turn_entry["uncached_tokens"] * prices["input"]
                           + turn_entry["cached_tokens"] * prices["input_cached"]
                           + turn_entry["output_tokens"] * prices["output"])
                if turn_cost > avg_cost * RUNAWAY_MULTIPLIER:
                    print(f"\n  RUNAWAY WARNING: Turn {turn_num} cost ${turn_cost:.4f} vs avg ${avg_cost:.4f}")

            # ── Phase 2a → 2b switch (testcase mode only) ─────────
            if tc_phase == "2a" and agent_done and TESTCASE_PLAN_MARKER in final_output:
                test_plan = final_output
                final_output = ""
                agent_done = False
                tc_phase = "2b"

                tc_count = sum(1 for line in test_plan.split("\n")
                              if line.strip().startswith("TC") or
                              (line.strip()[:2].rstrip(".").isdigit() and "|" in line))
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

    except Exception as e:
        print(f"\n  ERROR on screen '{screen_name}': {e}")
        duration = time.time() - start_time
        return {"screen": screen_name, "status": "error", "error": str(e), "turns": logger.turn, "cost": logger.get_current_cost(), "duration": duration}

    # ── End of loop ───────────────────────────────────────────

    duration = time.time() - start_time
    tracker.end_run("completed" if agent_done else "max_turns_reached")

    # ── Save knowledge from Pass 1 ────────────────────────────
    if mode not in ("safe_test", "recon", "testcase") and final_output:
        knowledge_path = _save_knowledge(final_output, package_name, app_name, screen_name)
        if knowledge_path:
            print(f"  Pass 1 knowledge ready — run 'testcase' mode next")

    # ── Save results ──────────────────────────────────────────
    _save_results(
        mode=mode,
        model=model,
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

    return {
        "screen": screen_name,
        "status": "completed" if agent_done else "max_turns_reached",
        "turns": logger.turn,
        "cost": logger.get_current_cost(),
        "duration": duration,
        "final_output": final_output,
    }


# ── Multi-Screen Run ──────────────────────────────────────────────────────────

async def run_multi_screen(
    device_id: str,
    package_name: str,
    app_name: str,
    mode: str,
    screen_names: list[str],
    model: str = "",
    provider: str = "",
) -> None:
    """Run tests across multiple screens. One MCP server, one model client, multiple screen runs."""
    model = model or DEFAULT_MODEL
    provider = provider or DEFAULT_PROVIDER

    model_config, provider_label = _build_model_config(model, provider)

    print(f"\n{'='*60}")
    print(f"  MULTI-SCREEN RUN")
    print(f"  Mode: {mode}")
    print(f"  Screens: {', '.join(screen_names)}")
    print(f"  Model: {model}")
    print(f"  Provider: {provider_label}")
    print(f"  Device: {device_id}")
    print(f"  Package: {package_name}")
    print(f"  Budget: ${MAX_BUDGET} per screen")
    print(f"{'='*60}\n")

    async with MCPServerStdio(
        name="mobile-mcp",
        params={
            "command": "npx",
            "args": ["-y", "@mobilenext/mobile-mcp@latest"],
        },
        cache_tools_list=True,
        client_session_timeout_seconds=30.0,
    ) as server:

        results = []

        for i, screen_name in enumerate(screen_names):
            print(f"\n{'='*60}")
            print(f"  SCREEN {i+1}/{len(screen_names)}: {screen_name}")
            print(f"{'='*60}")

            # Clean up from previous screen (dismiss keyboard, close popups)
            # 30s timeout — if cleanup hangs, move on regardless
            if i > 0:
                try:
                    await asyncio.wait_for(
                        _cleanup_screen(server, device_id),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    print(f"  ⚠ Cleanup timed out (30s) — proceeding anyway")

            # Navigate to the screen tab
            nav_ok = await _navigate_to_screen(server, device_id, screen_name)
            if not nav_ok:
                print(f"  Skipping {screen_name} — could not navigate")
                results.append({"screen": screen_name, "status": "nav_failed", "turns": 0, "cost": 0, "duration": 0})
                continue

            # Run the screen
            result = await _run_single_screen(
                server=server,
                device_id=device_id,
                package_name=package_name,
                app_name=app_name,
                mode=mode,
                screen_name=screen_name,
                model=model,
                model_config=model_config,
                provider=provider,
            )
            results.append(result)

        # ── Print combined summary ────────────────────────────
        _print_multi_summary(results, model)
        _save_multi_summary(results, model, mode)


# ── Single-Screen Entry Point (backward compatible) ───────────────────────────

async def run_orchestrated(device_id: str, package_name: str, app_name: str, mode: str, screen_name: str = "", model: str = "", provider: str = "") -> None:
    """Single screen run — delegates to run_multi_screen with one screen."""
    await run_multi_screen(
        device_id=device_id,
        package_name=package_name,
        app_name=app_name,
        mode=mode,
        screen_names=[screen_name],
        model=model,
        provider=provider,
    )


# ── Multi-Screen Summary Helpers ──────────────────────────────────────────────

def _print_multi_summary(results: list[dict], model: str) -> None:
    if len(results) <= 1:
        return  # No summary needed for single screen

    total_turns = sum(r.get("turns", 0) for r in results)
    total_cost = sum(r.get("cost", 0) for r in results)
    total_duration = sum(r.get("duration", 0) for r in results)

    print(f"\n{'='*60}")
    print(f"  MULTI-SCREEN SUMMARY ({model})")
    print(f"{'='*60}")
    print(f"  {'Screen':<15} {'Status':<20} {'Turns':>6} {'Cost':>10} {'Duration':>10}")
    print(f"  {'-'*15} {'-'*20} {'-'*6} {'-'*10} {'-'*10}")
    for r in results:
        screen = r.get("screen", "?")[:15]
        status = r.get("status", "?")[:20]
        turns = r.get("turns", 0)
        cost = r.get("cost", 0)
        dur = r.get("duration", 0)
        print(f"  {screen:<15} {status:<20} {turns:>6} ${cost:>8.4f} {dur:>8.1f}s")
    print(f"  {'-'*15} {'-'*20} {'-'*6} {'-'*10} {'-'*10}")
    print(f"  {'TOTAL':<15} {'':<20} {total_turns:>6} ${total_cost:>8.4f} {total_duration:>8.1f}s")
    print(f"{'='*60}")


def _save_multi_summary(results: list[dict], model: str, mode: str) -> None:
    if len(results) <= 1:
        return

    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    date_folder = now.strftime("%Y-%m-%d")

    if mode == "testcase":
        runs_dir = Path(__file__).parent / "runs" / "pass2" / date_folder
    else:
        runs_dir = Path(__file__).parent / "runs" / "pass1" / date_folder
    runs_dir.mkdir(parents=True, exist_ok=True)

    model_short = model.split("/")[-1].replace("-", "")[:12]

    # ── Combined text report (one file for all screens) ──────
    total_turns = sum(r.get("turns", 0) for r in results)
    total_cost = sum(r.get("cost", 0) for r in results)
    total_duration = sum(r.get("duration", 0) for r in results)

    report_path = runs_dir / f"report_multi_{model_short}_{timestamp}.txt"
    with open(report_path, "w") as f:
        f.write(f"MULTI-SCREEN TEST REPORT\n")
        f.write(f"{'='*60}\n")
        f.write(f"Model: {model}\n")
        f.write(f"Mode: {mode}\n")
        f.write(f"Screens: {', '.join(r.get('screen', '?') for r in results)}\n")
        f.write(f"Total Turns: {total_turns}\n")
        f.write(f"Total Cost: ${total_cost:.4f}\n")
        f.write(f"Total Duration: {total_duration:.1f}s\n")
        f.write(f"Timestamp: {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*60}\n\n")

        for r in results:
            screen = r.get("screen", "?")
            f.write(f"\n{'#'*60}\n")
            f.write(f"# SCREEN: {screen}\n")
            f.write(f"# Status: {r.get('status', '?')}  |  Turns: {r.get('turns', 0)}  |  Cost: ${r.get('cost', 0):.4f}  |  Duration: {r.get('duration', 0):.1f}s\n")
            f.write(f"{'#'*60}\n\n")
            f.write(r.get("final_output", "(no output)"))
            f.write(f"\n\n")

        f.write(f"\n{'='*60}\n")
        f.write(f"END OF MULTI-SCREEN REPORT\n")
        f.write(f"{'='*60}\n")

    print(f"\n  Combined report: {report_path}")

    # ── JSON summary ─────────────────────────────────────────
    summary_path = runs_dir / f"summary_multi_{model_short}_{timestamp}.json"

    # Strip final_output from results to keep JSON compact
    clean_results = []
    for r in results:
        entry = {k: v for k, v in r.items() if k != "final_output"}
        clean_results.append(entry)

    with open(summary_path, "w") as f:
        json.dump({
            "type": "multi_screen_summary",
            "model": model,
            "mode": mode,
            "timestamp": now.isoformat(),
            "screens": clean_results,
            "totals": {
                "turns": sum(r.get("turns", 0) for r in results),
                "cost": sum(r.get("cost", 0) for r in results),
                "duration": sum(r.get("duration", 0) for r in results),
            },
        }, f, indent=2)

    print(f"\n  Multi-screen summary: {summary_path}")


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

def _save_knowledge(final_output: str, package_name: str, app_name: str, screen_name: str = "") -> str | None:
    if not final_output:
        return None

    # Try multiple formats: ## KNOWLEDGE, **KNOWLEDGE**, KNOWLEDGE
    for marker in ["## KNOWLEDGE", "**KNOWLEDGE**", "KNOWLEDGE\n"]:
        knowledge_start = final_output.find(marker)
        if knowledge_start != -1:
            break
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
    if screen_name:
        safe_name = f"{safe_name}_{screen_name}"
    knowledge_path = knowledge_dir / f"{safe_name}.json"

    knowledge["_meta"] = {
        "package_name": package_name,
        "app_name": app_name,
        "screen_name": screen_name,
        "saved_at": datetime.now().isoformat(),
        "source": "pass1_mobile_exploration",
    }

    with open(knowledge_path, "w") as f:
        json.dump(knowledge, f, indent=2)

    print(f"  Knowledge saved: {knowledge_path}")
    return str(knowledge_path)


def _load_knowledge(package_name: str, screen_name: str = "") -> str | None:
    knowledge_dir = Path(__file__).parent / "knowledge"
    if not knowledge_dir.exists():
        return None

    safe_name = package_name.replace(".", "_")[:80]
    if screen_name:
        safe_name = f"{safe_name}_{screen_name}"
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

    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    date_folder = now.strftime("%Y-%m-%d")
    if mode == "testcase":
        runs_dir = Path(__file__).parent / "runs" / "pass2" / date_folder
    else:
        runs_dir = Path(__file__).parent / "runs" / "pass1" / date_folder
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Include model short name in filename for comparison
    model_short = model.split("/")[-1].replace("-", "")[:12]  # "gpt-5" or "gptoss120b"
    output_path = runs_dir / f"output_mobile_{mode}_{model_short}_{timestamp}.txt"
    turns_path = runs_dir / f"turns_mobile_{mode}_{model_short}_{timestamp}.json"

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
