# version_2/run.py — CLI entrypoint for v2 orchestrated agent (POC — FROZEN)
# DEPRECATED: Use `python -m qa.cli` for the new pipeline architecture.
# This file is kept for backward compatibility. Do not modify.
# Usage:
#   python version_2/run.py                                    → safe test on TECU
#   python version_2/run.py poc                                → POC on TECU (Pass 1: explore + fill + knowledge)
#   python version_2/run.py testcase                           → Test cases on TECU (Pass 2: uses Pass 1 knowledge)
#   python version_2/run.py safe_test https://example.com      → safe test on custom URL
#   python version_2/run.py poc https://example.com "My App"   → POC on custom URL

import sys
import asyncio

from config import TARGET_URL, APP_NAME, TURN_LIMITS
from orchestrator import run_orchestrated


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "safe_test"

    if mode not in TURN_LIMITS:
        print(f"Unknown mode: {mode}")
        print(f"Available: {', '.join(TURN_LIMITS.keys())}")
        sys.exit(1)

    url = sys.argv[2] if len(sys.argv) > 2 else TARGET_URL
    app_name = sys.argv[3] if len(sys.argv) > 3 else APP_NAME

    asyncio.run(run_orchestrated(url, app_name, mode))
