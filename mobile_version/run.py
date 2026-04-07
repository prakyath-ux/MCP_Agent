# mobile_version/run.py — CLI entrypoint for mobile testing agent
# Usage:
#   python run.py                                          → safe test with config defaults
#   python run.py poc iTeller                              → POC on iTeller screen
#   python run.py testcase iTeller --model gpt-5.1         → Single screen testcase
#   python run.py testcase iTeller,LOAN,MORE --model gpt-5 → Multi-screen testcase
#   python run.py testcase iTeller,LOAN,MORE -m openai/gpt-oss-120b -p openrouter

import argparse
import asyncio

from config import DEVICE_ID, PACKAGE_NAME, APP_NAME, TURN_LIMITS, MODEL, MODEL_PROVIDER
from orchestrator import run_orchestrated, run_multi_screen

# Model → provider auto-mapping (sensible defaults)
MODEL_PROVIDER_DEFAULTS = {
    "gpt-5": "openai_chat",
    "gpt-5.1": "openai_chat",
    "openai/gpt-oss-120b": "openrouter",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Mobile QA Testing Agent")
    parser.add_argument("mode", nargs="?", default="safe_test",
                        choices=list(TURN_LIMITS.keys()),
                        help=f"Run mode (default: safe_test)")
    parser.add_argument("screen", nargs="?", default="",
                        help="Screen name(s), comma-separated (e.g., iTeller or iTeller,LOAN,MORE)")
    parser.add_argument("--model", "-m", default=None,
                        help=f"Model to use (default: {MODEL})")
    parser.add_argument("--provider", "-p", default=None,
                        choices=["openai", "openai_chat", "groq", "openrouter"],
                        help=f"Model provider (default: auto-detect or {MODEL_PROVIDER})")
    parser.add_argument("--device", default=DEVICE_ID,
                        help=f"Device ID (default: {DEVICE_ID})")
    parser.add_argument("--package", default=PACKAGE_NAME,
                        help=f"Package name (default: {PACKAGE_NAME})")
    parser.add_argument("--app", default=APP_NAME,
                        help=f"App name (default: {APP_NAME})")

    args = parser.parse_args()

    # Resolve model and provider
    model = args.model or MODEL
    if args.provider:
        provider = args.provider
    elif args.model:
        provider = MODEL_PROVIDER_DEFAULTS.get(model, "openai_chat")
    else:
        provider = MODEL_PROVIDER

    # Parse screen names (comma-separated)
    screen_names = [s.strip() for s in (args.screen or "").split(",") if s.strip()]

    if len(screen_names) > 1:
        # Multi-screen run
        asyncio.run(run_multi_screen(
            device_id=args.device,
            package_name=args.package,
            app_name=args.app,
            mode=args.mode,
            screen_names=screen_names,
            model=model,
            provider=provider,
        ))
    else:
        # Single screen run (backward compatible)
        asyncio.run(run_orchestrated(
            device_id=args.device,
            package_name=args.package,
            app_name=args.app,
            mode=args.mode,
            screen_name=screen_names[0] if screen_names else "",
            model=model,
            provider=provider,
        ))


if __name__ == "__main__":
    main()
