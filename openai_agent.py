import os
import sys
import time
import json
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Verify API key exists
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("WARNING: OPENAI_API_KEY not set in .env")
    sys.exit(1)
else:
    print(f"API key loaded: {api_key[:2]}...{api_key[-2:]}")


# SDK + prompt + usage tracker imports
from agents import Agent, Runner
from agents.mcp import MCPServerStdio
from agents.lifecycle import RunHooks
from agents.items import ModelResponse, TResponseInputItem
from agents.run_context import RunContextWrapper
from prompts.system_prompt import SYSTEM_PROMPT
from state.usage_tracker import log_usage


class LiveTurnLogger(RunHooks):
    """Prints real-time per-turn stats as the agent runs."""

    def __init__(self) -> None:
        self.turn = 0
        self._header_printed = False

    async def on_llm_end(self, context: RunContextWrapper, agent: Agent, response: ModelResponse) -> None:
        self.turn += 1
        usage = response.usage

        t_input = getattr(usage, "input_tokens", 0) or 0
        t_output = getattr(usage, "output_tokens", 0) or 0
        t_cached = 0
        details = getattr(usage, "input_tokens_details", None)
        if details:
            t_cached = getattr(details, "cached_tokens", 0) or 0
        cache_pct = (t_cached / t_input * 100) if t_input > 0 else 0

        # Extract action name from output
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

print("openai-agents SDK imported successfully")
print(f"System prompt loaded: {len(SYSTEM_PROMPT)} chars (~{len(SYSTEM_PROMPT) // 4} tokens)")


MODEL = "gpt-5"

# Turn limits per mode
TURNS = {
    "safe_test": 3,
    "poc": 120,
    "full": 600,
}

async def run_agent(url: str, app_name: str, mode: str) -> None:
    max_turns = TURNS[mode]

    if mode == "safe_test":
        task = f"Navigate to {url} and take a snapshot. Describe what you see."
    else:
        task = (
            f"Test the web application at {url} ({app_name}). "
            "Do not click 'Save & Continue'."
        )

    print(f"\n{'='*60}")
    print(f"  Mode: {mode}")
    print(f"  URL: {url}")
    print(f"  App: {app_name}")
    print(f"  Model: {MODEL}")
    print(f"  Max turns: {max_turns}")
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
            name="QA Tester",
            instructions=SYSTEM_PROMPT,
            model=MODEL,
            mcp_servers=[server],
        )

        live_logger = LiveTurnLogger()
        print("Agent ready. Starting run...\n")
        start_time = time.time()

        result = await Runner.run(
            starting_agent=agent,
            input=task,
            max_turns=max_turns,
            hooks=live_logger,
        )

        duration = time.time() - start_time

        # Count tokens from raw responses (with caching breakdown)
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        turn_log = []
        turns_used = len(result.raw_responses)

        for i, response in enumerate(result.raw_responses):
            usage = getattr(response, "usage", None)
            if not usage:
                continue

            t_input = getattr(usage, "input_tokens", 0) or 0
            t_output = getattr(usage, "output_tokens", 0) or 0
            t_cached = 0
            details = getattr(usage, "input_tokens_details", None)
            if details:
                t_cached = getattr(details, "cached_tokens", 0) or 0

            input_tokens += t_input
            output_tokens += t_output
            cached_tokens += t_cached

            # Extract tool calls and model reasoning from this turn
            actions = []
            model_text = ""
            for item in getattr(response, "output", []):
                name = getattr(item, "name", None)
                if name:
                    args_raw = getattr(item, "arguments", "{}")
                    try:
                        args = json.loads(args_raw)
                    except (json.JSONDecodeError, TypeError):
                        args = {"raw": str(args_raw)[:200]}
                    target = args.get("uid") or args.get("selector") or args.get("url", "")
                    actions.append({"tool": name, "target": str(target)[:80]})
                content = getattr(item, "content", None)
                if content and not name:
                    for part in (content if isinstance(content, list) else [content]):
                        text = getattr(part, "text", None)
                        if text:
                            model_text = text[:200]

            turn_entry = {
                "turn": i + 1,
                "input_tokens": t_input,
                "cached_tokens": t_cached,
                "uncached_tokens": t_input - t_cached,
                "output_tokens": t_output,
                "cache_hit_ratio": round(t_cached / t_input, 3) if t_input > 0 else 0,
                "actions": actions,
            }
            if model_text:
                turn_entry["thought"] = model_text
            if i > 0 and turn_log:
                turn_entry["new_tokens"] = t_input - turn_log[-1]["input_tokens"]

            turn_log.append(turn_entry)

        # Log to Excel (with caching data)
        summary = log_usage(
            run_mode=mode,
            model=MODEL,
            turns_used=turns_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            duration_sec=duration,
            status="completed",
            notes=f"{app_name} — {url}",
        )

        # Print results
        print(f"\n{'='*60}")
        print("  AGENT OUTPUT")
        print(f"{'='*60}")
        print(result.final_output)
        print(f"\n{'='*60}")
        print("  USAGE SUMMARY")
        print(f"{'='*60}")
        print(f"  Turns used:    {turns_used}")
        print(f"  Input tokens:  {input_tokens:,}")
        print(f"    Cached:      {cached_tokens:,} ({summary['cache_hit_pct']:.1f}%)")
        print(f"    Uncached:    {input_tokens - cached_tokens:,}")
        print(f"  Output tokens: {output_tokens:,}")
        print(f"  Total tokens:  {summary['total_tokens']:,}")
        print(f"  Real cost:     ${summary['real_cost']:.6f} (₹{summary['real_cost_inr']:.4f})")
        print(f"  No-cache cost: ${summary['nocache_cost']:.6f}")
        print(f"  Savings:       ${summary['savings']:.6f} ({summary['savings_pct']:.1f}%)")
        print(f"  Cumulative:    ${summary['cumulative_cost']:.6f}")
        print(f"  Duration:      {duration:.1f}s")
        print(f"  Saved to:      state/runs/usage_log.xlsx")
        print(f"{'='*60}")

        # Save agent output + per-turn log
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path("state/runs") / f"output_{mode}_{timestamp}.txt"
        turns_path = Path("state/runs") / f"turns_{mode}_{timestamp}.json"

        with open(output_path, "w") as f:
            f.write(f"Mode: {mode}\n")
            f.write(f"URL: {url}\n")
            f.write(f"App: {app_name}\n")
            f.write(f"Model: {MODEL}\n")
            f.write(f"Turns: {turns_used} / {max_turns}\n")
            f.write(f"Real Cost: ${summary['real_cost']:.6f} (₹{summary['real_cost_inr']:.4f})\n")
            f.write(f"No-Cache Cost: ${summary['nocache_cost']:.6f}\n")
            f.write(f"Savings: ${summary['savings']:.6f} ({summary['savings_pct']:.1f}%)\n")
            f.write(f"Cache Hit: {summary['cache_hit_pct']:.1f}%\n")
            f.write(f"Duration: {duration:.1f}s\n")
            f.write(f"{'='*60}\n\n")

            # Turn-by-turn breakdown
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

            f.write(result.final_output)

        with open(turns_path, "w") as f:
            json.dump({"model": MODEL, "mode": mode, "turns": turn_log}, f, indent=2)

        print(f"\n  Output saved:  {output_path}")
        print(f"  Turn log:      {turns_path}")


if __name__ == "__main__":
    url = "https://qa-tq-awp.impactodigifin.xyz/newapplication"
    app_name = "TECU Credit Union"

    # Usage: python openai_agent.py              → safe test (3 turns, ~$0.003)
    #        python openai_agent.py poc           → page 1 only (120 turns, ~$0.10-0.15)
    #        python openai_agent.py full          → all pages (600 turns, ~$0.50-1.00)
    mode = sys.argv[1] if len(sys.argv) > 1 else "safe_test"
    if mode not in TURNS:
        print(f"Unknown mode: {mode}")
        print(f"Available: {', '.join(TURNS.keys())}")
        sys.exit(1)
    asyncio.run(run_agent(url, app_name, mode))
