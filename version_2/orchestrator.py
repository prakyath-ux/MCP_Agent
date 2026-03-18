# version_2/orchestrator.py — Managed turn loop with compaction, state tracking, budget control
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agents import Agent, Runner
from agents.mcp import MCPServerStdio
from agents.lifecycle import RunHooks
from agents.items import ModelResponse, TResponseInputItem
from agents.run_context import RunContextWrapper
from agents.exceptions import MaxTurnsExceeded
from agents.run_error_handlers import RunErrorHandlerInput, RunErrorHandlerResult

from config import MODEL, TURN_LIMITS, MAX_BUDGET, BUDGET_WARNING_PCT, PRICING, V2_RUNS_DIR, PASS1_RUNS_DIR, PASS2_RUNS_DIR, XPATH_NUDGE_BEFORE_END, LOOP_WINDOW, LOOP_THRESHOLD, KNOWLEDGE_DIR, KEEP_LAST_N_TURNS
from prompts import SYSTEM_PROMPT, TESTCASE_PROMPT
from compactor import compact_history, update_summary

# Add parent dir to path so we can import state modules
sys.path.insert(0, str(Path(__file__).parent.parent))
from state.usage_tracker import log_usage
from state.tracker import StateTracker


# ── Live Logger (same as v1, prints per-turn stats to terminal) ───────────────

class LiveTurnLogger(RunHooks):

    def __init__(self) -> None:
        self.turn = 0
        self._header_printed = False
        # Track tokens for budget calculation
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

        # Extract action name
        action = "text_output"
        target = ""
        for item in response.output:
            name = getattr(item, "name", None)
            if name:
                action = name
                args_raw = getattr(item, "arguments", "{}")
                try:
                    args = json.loads(args_raw)
                    target = args.get("uid") or args.get("url", "")
                    target = str(target)[:20]
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        if not self._header_printed:
            print(f"  {'Turn':<5} {'Action':<22} {'Target':<20} {'Input':>7} {'Cached':>7} {'Out':>6} {'Cache%':>7}")
            print(f"  {'-'*5} {'-'*22} {'-'*20} {'-'*7} {'-'*7} {'-'*6} {'-'*7}")
            self._header_printed = True

        print(f"  {self.turn:<5} {action:<22} {target:<20} {t_input:>7,} {t_cached:>7,} {t_output:>6,} {cache_pct:>6.0f}%")

    def get_current_cost(self) -> float:
        """Calculate real cost so far using pricing from config."""
        prices = PRICING.get(MODEL, PRICING["gpt-5"])
        uncached = self.total_input - self.total_cached
        return (
            uncached * prices["input"]
            + self.total_cached * prices["input_cached"]
            + self.total_output * prices["output"]
        )


# ── Main Orchestrated Run ────────────────────────────────────────────────────

async def run_orchestrated(url: str, app_name: str, mode: str) -> None:
    max_turns = TURN_LIMITS[mode]

    # ── Select prompt and task based on mode ───────────────────
    knowledge_json = None
    active_prompt = SYSTEM_PROMPT

    if mode == "testcase":
        # Pass 2: load knowledge from Pass 1
        knowledge_json = _load_knowledge(url)
        if not knowledge_json:
            print("  ERROR: No Pass 1 knowledge found for this URL.")
            print(f"  Run a 'poc' first to generate knowledge in {KNOWLEDGE_DIR}/")
            return
        active_prompt = TESTCASE_PROMPT.replace("{knowledge_json}", knowledge_json).replace("{page_url}", url)
        task = (
            f"Run regression test cases on {url} ({app_name}). "
            "Execute test cases using the knowledge JSON. "
            "No snapshots, no screenshots, no exploration. "
            "Fill → check error → record → next field."
        )
    elif mode == "safe_test":
        task = f"Navigate to {url} and take a snapshot. Describe what you see."
    elif mode == "recon":
        task = (
            f"Navigate to {url} ({app_name}). Explore the page and discover all "
            "interactive elements. Do NOT fill or click any elements. "
            "Extract XPaths for all interactive elements using evaluate_script. "
            "Produce a final report with the XPATHS section."
        )
    else:
        task = (
            f"Test the web application at {url} ({app_name}). "
            "Do not click 'Save & Continue'."
        )

    print(f"\n{'='*60}")
    print(f"  Mode: {mode} (v2 — orchestrated loop)")
    print(f"  URL: {url}")
    print(f"  App: {app_name}")
    print(f"  Model: {MODEL}")
    print(f"  Max turns: {max_turns}")
    print(f"  Budget: ${MAX_BUDGET}")
    print(f"{'='*60}\n")

    async with MCPServerStdio(
        name="Chrome DevTools MCP",
        params={
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest"],
        },
        cache_tools_list=True,
        client_session_timeout_seconds=30.0,
    ) as server:

        agent = Agent(
            name="QA Tester" if mode != "testcase" else "QA Test Case Runner",
            instructions=active_prompt,
            model=MODEL,
            mcp_servers=[server],
        )

        logger = LiveTurnLogger()
        tracker = StateTracker()
        tracker.start_run(url=url, app_name=app_name)

        print("Agent ready. Starting orchestrated run...\n")
        start_time = time.time()

        # ── The Orchestrated Loop ─────────────────────────────────
        # v1: Runner.run(max_turns=120) — black box, no control
        # v2: We run one turn at a time, compact between turns

        input_items: str | list = task       # First turn gets the task string
        rolling_summary = ""
        turn_log: list[dict] = []
        final_output = ""
        agent_done = False

        for turn_num in range(1, max_turns + 1):

            # ── Run exactly ONE turn ──────────────────────────────
            # Error handler catches MaxTurnsExceeded and returns a
            # placeholder output so Runner.run returns normally with
            # all history intact (new_items, raw_responses, etc.)
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

            # ── Check if agent produced final output (no more tool calls) ──
            if result.final_output and result.final_output != "__TURN_LIMIT__":
                final_output = result.final_output
                agent_done = True

            # ── Extract what happened this turn for logging ───────
            turn_entry = _extract_turn_log(result, turn_num, turn_log)
            turn_log.append(turn_entry)

            # ── Update rolling summary ────────────────────────────
            full_history = result.to_input_list()
            # Get the items from this turn only (last few items in history)
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

            # ── Stop if agent is done ─────────────────────────────
            if agent_done:
                print(f"\n  Agent completed in {turn_num} turns.")
                break

            # ── End-of-run nudge when turns are running low ───────
            turns_remaining = max_turns - turn_num
            if turns_remaining == XPATH_NUDGE_BEFORE_END and not agent_done:
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
                        "Run the XPath extraction evaluate_script NOW and produce "
                        "your final report with RESULTS, XPATHS, ISSUES, and KNOWLEDGE sections."
                    )
                print(f"\n  NUDGE: {turns_remaining} turns left — injecting report reminder")
                full_history.append({
                    "role": "user",
                    "content": nudge_msg,
                })

            # ── Loop detection ───────────────────────────────────
            loop_warning = _detect_loop(turn_log)
            if loop_warning:
                print(f"\n  LOOP DETECTED: {loop_warning}")
                full_history.append({
                    "role": "user",
                    "content": loop_warning,
                })

            # ── Compact history for next turn ─────────────────────
            # Testcase mode: each test is independent, wipe all old turns
            keep_n = 0 if mode == "testcase" else KEEP_LAST_N_TURNS
            input_items = compact_history(full_history, rolling_summary, keep_last_n=keep_n)

        # ── End of loop ───────────────────────────────────────────

        duration = time.time() - start_time
        tracker.end_run("completed" if agent_done else "max_turns_reached")

        # ── Save knowledge from Pass 1 (for Pass 2 test cases) ───
        if mode not in ("safe_test", "recon", "testcase") and final_output:
            knowledge_path = _save_knowledge(final_output, url, app_name)
            if knowledge_path:
                print(f"  Pass 1 knowledge ready — run 'testcase' mode next")

        # ── Post-run: logging and output (same format as v1) ──────
        _save_results(
            mode=mode,
            model=MODEL,
            logger=logger,
            turn_log=turn_log,
            final_output=final_output,
            rolling_summary=rolling_summary,
            duration=duration,
            url=url,
            app_name=app_name,
            max_turns=max_turns,
        )


# ── Helper: Extract turn log entry ───────────────────────────────────────────

def _extract_turn_log(result, turn_num: int, prev_log: list[dict]) -> dict:
    """Build a turn log entry from the run result."""
    # Get usage from the last raw response
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
                target = args.get("uid") or args.get("selector") or args.get("url", "")
                actions.append({"tool": name, "target": str(target)[:80]})
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
    """Check if the agent is stuck retrying the same element.

    Looks at the last LOOP_WINDOW turns. If any target (uid) appears
    LOOP_THRESHOLD or more times, return a warning message to inject.
    Ignores non-interactive actions (take_snapshot, evaluate_script, etc.)
    """
    if len(turn_log) < LOOP_THRESHOLD:
        return None

    # Only count actions that target a specific element (click, fill, press_key, etc.)
    # Skip observation tools — they don't indicate retries
    observation_tools = {"take_snapshot", "take_screenshot", "evaluate_script",
                         "list_network_requests", "list_console_messages",
                         "get_network_request", "get_console_message"}

    # Collect targets from the last LOOP_WINDOW turns
    recent = turn_log[-LOOP_WINDOW:]
    target_counts: dict[str, int] = {}

    for entry in recent:
        for action in entry.get("actions", []):
            tool = action.get("tool", "")
            target = action.get("target", "")
            if tool not in observation_tools and target:
                target_counts[target] = target_counts.get(target, 0) + 1

    # Check for any target that hit the threshold
    for target, count in target_counts.items():
        if count >= LOOP_THRESHOLD:
            return (
                f"LOOP DETECTED: You have attempted element '{target}' "
                f"{count} times in the last {LOOP_WINDOW} actions without success. "
                f"This element is not responding to your approach. "
                f"Mark it as FAILED in your results, skip it, and move to the next "
                f"field. If all other fields are done, produce your final report now."
            )

    return None


# ── Helper: Knowledge Storage (Pass 1 → Pass 2 bridge) ──────────────────────

def _save_knowledge(final_output: str, url: str, app_name: str) -> str | None:
    """Extract and save the KNOWLEDGE JSON block from Pass 1 output.

    Returns the path to the saved file, or None if no knowledge found.
    Handles both code-fenced (```json...```) and raw JSON formats.
    """
    if not final_output:
        return None

    # Find the KNOWLEDGE section
    knowledge_start = final_output.find("## KNOWLEDGE")
    if knowledge_start == -1:
        return None

    section = final_output[knowledge_start:]

    # Try code-fenced JSON first (```json ... ```)
    json_fence_start = section.find("```json")
    if json_fence_start != -1:
        json_fence_start += len("```json")
        json_fence_end = section.find("```", json_fence_start)
        raw_json = section[json_fence_start:json_fence_end].strip() if json_fence_end != -1 else section[json_fence_start:].strip()
    else:
        # No code fence — find raw JSON starting with {
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

    # Validate it's parseable JSON
    try:
        knowledge = json.loads(raw_json)
    except json.JSONDecodeError:
        print(f"  WARNING: Knowledge JSON found but invalid — skipping save")
        return None

    # Save to knowledge directory
    knowledge_dir = Path(__file__).parent.parent / KNOWLEDGE_DIR
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # Use URL-based filename for easy lookup
    safe_name = url.replace("https://", "").replace("http://", "").replace("/", "_").replace(":", "_")[:80]
    knowledge_path = knowledge_dir / f"{safe_name}.json"

    # Add metadata
    knowledge["_meta"] = {
        "url": url,
        "app_name": app_name,
        "saved_at": datetime.now().isoformat(),
        "source": "pass1_exploration",
    }

    with open(knowledge_path, "w") as f:
        json.dump(knowledge, f, indent=2)

    print(f"  Knowledge saved: {knowledge_path}")
    return str(knowledge_path)


def _load_knowledge(url: str) -> str | None:
    """Load Pass 1 knowledge for a given URL.

    Returns the JSON string to inject into the testcase prompt, or None.
    """
    knowledge_dir = Path(__file__).parent.parent / KNOWLEDGE_DIR
    if not knowledge_dir.exists():
        return None

    safe_name = url.replace("https://", "").replace("http://", "").replace("/", "_").replace(":", "_")[:80]
    knowledge_path = knowledge_dir / f"{safe_name}.json"

    if not knowledge_path.exists():
        # Try partial match — URL might have trailing slash difference
        for f in knowledge_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("_meta", {}).get("url", "").rstrip("/") == url.rstrip("/"):
                    knowledge_path = f
                    break
            except (json.JSONDecodeError, KeyError):
                continue
        else:
            return None

    content = knowledge_path.read_text()
    print(f"  Knowledge loaded: {knowledge_path}")
    return content


# ── Helper: Save results (output file + turn log + Excel) ────────────────────

def _save_results(
    mode: str,
    model: str,
    logger: LiveTurnLogger,
    turn_log: list[dict],
    final_output: str,
    rolling_summary: str,
    duration: float,
    url: str,
    app_name: str,
    max_turns: int,
) -> None:
    """Save all output files and log usage — same format as v1 for dashboard compatibility."""
    turns_used = logger.turn

    # Log to Excel
    summary = log_usage(
        run_mode=mode,
        model=model,
        turns_used=turns_used,
        input_tokens=logger.total_input,
        output_tokens=logger.total_output,
        cached_tokens=logger.total_cached,
        duration_sec=duration,
        status="completed",
        notes=f"v2 orchestrated | {app_name} — {url}",
    )

    # Print summary
    print(f"\n{'='*60}")
    print("  AGENT OUTPUT")
    print(f"{'='*60}")
    print(final_output or "(no final output — agent may have hit turn/budget limit)")
    print(f"\n{'='*60}")
    print("  USAGE SUMMARY (v2 — orchestrated)")
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

    # Save output file — route to pass1/ or pass2/ directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if mode == "testcase":
        runs_dir = Path(__file__).parent.parent / PASS2_RUNS_DIR
    else:
        runs_dir = Path(__file__).parent.parent / PASS1_RUNS_DIR
    runs_dir.mkdir(parents=True, exist_ok=True)

    output_path = runs_dir / f"output_v2_{mode}_{timestamp}.txt"
    turns_path = runs_dir / f"turns_v2_{mode}_{timestamp}.json"

    with open(output_path, "w") as f:
        f.write(f"Mode: {mode}\n")
        f.write(f"Version: v2 (orchestrated loop)\n")
        f.write(f"URL: {url}\n")
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

        # Turn log table
        f.write("## TURN LOG\n")
        f.write(f"{'Turn':<5} {'Action':<25} {'Target':<20} {'Input':>7} {'Cached':>7} {'New':>6} {'Out':>6} {'Cache%':>7}\n")
        f.write(f"{'-'*5} {'-'*25} {'-'*20} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*7}\n")
        for t in turn_log:
            action = t["actions"][0]["tool"] if t["actions"] else "text_output"
            target = t["actions"][0]["target"][:18] if t["actions"] else ""
            new_tok = t.get("new_tokens", "")
            new_str = f"{new_tok:>6,}" if isinstance(new_tok, int) else f"{'—':>6}"
            cache_pct = f"{t['cache_hit_ratio']*100:.0f}%"
            f.write(f"{t['turn']:<5} {action:<25} {target:<20} {t['input_tokens']:>7,} {t['cached_tokens']:>7,} {new_str} {t['output_tokens']:>6,} {cache_pct:>7}\n")
        f.write(f"\n{'='*60}\n\n")

        f.write(final_output or "(no final output)")

    with open(turns_path, "w") as f:
        json.dump({
            "version": "v2",
            "model": model,
            "mode": mode,
            "rolling_summary": rolling_summary,
            "turns": turn_log,
        }, f, indent=2)

    print(f"\n  Output saved:  {output_path}")
    print(f"  Turn log:      {turns_path}")
