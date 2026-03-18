"""
Test alternative providers (Groq, OpenRouter) with the OpenAI Agents SDK.
Separate from openai_agent.py — no changes to production code.

Usage:
    python test_providers.py groq              → Llama 3.3 70B on Groq (free)
    python test_providers.py openrouter        → Gemini 2.5 Flash on OpenRouter (free)
    python test_providers.py groq llama-3.1-8b-instant   → specific model
    python test_providers.py openrouter deepseek/deepseek-v3.2-20251201
"""

import os
import sys
import time
import json
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Provider configs ─────────────────────────────────────────────────────────

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "meta-llama/llama-4-scout-17b-16e-instruct",
        ],
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "google/gemini-2.5-flash",
        "models": [
            "google/gemini-2.5-flash",
            "deepseek/deepseek-v3.2-20251201",
        ],
    },
}

# ── Validate args ─────────────────────────────────────────────────────────────

if len(sys.argv) < 2 or sys.argv[1] not in PROVIDERS:
    print("Usage: python test_providers.py <provider> [model]")
    print(f"  Providers: {', '.join(PROVIDERS.keys())}")
    for name, cfg in PROVIDERS.items():
        print(f"  {name} models: {', '.join(cfg['models'])}")
    sys.exit(1)

provider_name = sys.argv[1]
provider_cfg = PROVIDERS[provider_name]

# Check API key
api_key = os.getenv(provider_cfg["api_key_env"])
if not api_key:
    print(f"ERROR: {provider_cfg['api_key_env']} not set in .env")
    sys.exit(1)
print(f"API key loaded: {api_key[:4]}...{api_key[-3:]}")

# Model selection
model = sys.argv[2] if len(sys.argv) > 2 else provider_cfg["default_model"]
print(f"Provider: {provider_name}")
print(f"Model: {model}")

# ── SDK imports (after dotenv) ────────────────────────────────────────────────

from openai import AsyncOpenAI
from agents import Agent, Runner
from agents.model_settings import ModelSettings
from agents.models.openai_provider import OpenAIProvider
from agents.run_config import RunConfig
from agents.mcp import MCPServerStdio
from prompts.system_prompt import SYSTEM_PROMPT
from state.usage_tracker import log_usage

print(f"System prompt loaded: {len(SYSTEM_PROMPT)} chars (~{len(SYSTEM_PROMPT) // 4} tokens)")


async def run_test(url: str, app_name: str) -> None:
    mode = "poc"
    max_turns = 120

    task = (
        f"Test the web application at {url} ({app_name}). "
        "Do not click 'Save & Continue'."
    )

    # Build provider — force chat completions (no Responses API)
    custom_provider = OpenAIProvider(
        api_key=api_key,
        base_url=provider_cfg["base_url"],
        use_responses=False,
    )

    print(f"\n{'='*60}")
    print(f"  Provider: {provider_name}")
    print(f"  Model:    {model}")
    print(f"  URL:      {url}")
    print(f"  App:      {app_name}")
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
            model=model,
            mcp_servers=[server],
        )

        print("Agent ready. Starting run...\n")
        start_time = time.time()

        result = await Runner.run(
            starting_agent=agent,
            input=task,
            max_turns=max_turns,
            run_config=RunConfig(
                model_provider=custom_provider,
                model_settings=ModelSettings(
                    parallel_tool_calls=False,
                    temperature=0.1,
                    max_tokens=4096,
                ),
                tracing_disabled=True,
            ),
        )

        duration = time.time() - start_time

        # Count tokens from raw responses
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        turn_log = []
        turns_used = len(result.raw_responses)

        for i, response in enumerate(result.raw_responses):
            usage = getattr(response, "usage", None)
            if not usage:
                continue

            # Chat completions format: prompt_tokens / completion_tokens
            t_input = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
            t_output = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
            t_cached = 0

            # Try both token detail formats
            details = getattr(usage, "input_tokens_details", None) or getattr(usage, "prompt_tokens_details", None)
            if details:
                t_cached = getattr(details, "cached_tokens", 0) or 0

            input_tokens += t_input
            output_tokens += t_output
            cached_tokens += t_cached

            turn_log.append({
                "turn": i + 1,
                "input_tokens": t_input,
                "cached_tokens": t_cached,
                "uncached_tokens": t_input - t_cached,
                "output_tokens": t_output,
                "cache_hit_ratio": round(t_cached / t_input, 3) if t_input > 0 else 0,
            })

        # Log to Excel — model name includes provider for clarity
        display_model = f"{provider_name}/{model}"
        summary = log_usage(
            run_mode=mode,
            model=display_model,
            turns_used=turns_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            duration_sec=duration,
            status="completed",
            notes=f"{app_name} — {url} — provider: {provider_name}",
        )

        # Print results
        print(f"\n{'='*60}")
        print("  AGENT OUTPUT")
        print(f"{'='*60}")
        print(result.final_output)
        print(f"\n{'='*60}")
        print("  USAGE SUMMARY")
        print(f"{'='*60}")
        print(f"  Provider:      {provider_name}")
        print(f"  Model:         {model}")
        print(f"  Turns used:    {turns_used}")
        print(f"  Input tokens:  {input_tokens:,}")
        print(f"    Cached:      {cached_tokens:,} ({summary['cache_hit_pct']:.1f}%)")
        print(f"    Uncached:    {input_tokens - cached_tokens:,}")
        print(f"  Output tokens: {output_tokens:,}")
        print(f"  Total tokens:  {summary['total_tokens']:,}")
        print(f"  Real cost:     ${summary['real_cost']:.6f} (₹{summary['real_cost_inr']:.4f})")
        print(f"  Duration:      {duration:.1f}s")
        print(f"{'='*60}")

        # Save output + per-turn log
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_provider = provider_name.replace("/", "_")
        output_path = Path("state/runs") / f"output_{mode}_{safe_provider}_{timestamp}.txt"
        turns_path = Path("state/runs") / f"turns_{mode}_{safe_provider}_{timestamp}.json"

        with open(output_path, "w") as f:
            f.write(f"Mode: {mode}\n")
            f.write(f"URL: {url}\n")
            f.write(f"App: {app_name}\n")
            f.write(f"Provider: {provider_name}\n")
            f.write(f"Model: {model}\n")
            f.write(f"Turns: {turns_used} / {max_turns}\n")
            f.write(f"Real Cost: ${summary['real_cost']:.6f} (₹{summary['real_cost_inr']:.4f})\n")
            f.write(f"Cache Hit: {summary['cache_hit_pct']:.1f}%\n")
            f.write(f"Duration: {duration:.1f}s\n")
            f.write(f"{'='*60}\n\n")
            f.write(result.final_output)

        with open(turns_path, "w") as f:
            json.dump({
                "provider": provider_name,
                "model": model,
                "mode": mode,
                "turns": turn_log,
            }, f, indent=2)

        print(f"\n  Output saved:  {output_path}")
        print(f"  Turn log:      {turns_path}")


if __name__ == "__main__":
    url = "https://qa-tq-awp.impactodigifin.xyz/newapplication"
    app_name = "TECU Credit Union"
    asyncio.run(run_test(url, app_name))
