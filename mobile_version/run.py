# mobile_version/run.py — CLI entrypoint for mobile testing agent
# Usage:
#   python run.py                              → safe test on default device + app
#   python run.py poc                          → POC on default device + app (default screen)
#   python run.py poc iTeller                  → POC on iTeller screen
#   python run.py poc LOAN                     → POC on LOAN screen
#   python run.py testcase iTeller             → Testcase on iTeller screen
#   python run.py testcase LOAN               → Testcase on LOAN screen

import sys
import asyncio

from config import DEVICE_ID, PACKAGE_NAME, APP_NAME, TURN_LIMITS
from orchestrator import run_orchestrated


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "safe_test"

    if mode not in TURN_LIMITS:
        print(f"Unknown mode: {mode}")
        print(f"Available: {', '.join(TURN_LIMITS.keys())}")
        sys.exit(1)

    screen_name = sys.argv[2] if len(sys.argv) > 2 else ""
    device_id = sys.argv[3] if len(sys.argv) > 3 else DEVICE_ID
    package_name = sys.argv[4] if len(sys.argv) > 4 else PACKAGE_NAME
    app_name = sys.argv[5] if len(sys.argv) > 5 else APP_NAME

    asyncio.run(run_orchestrated(device_id, package_name, app_name, mode, screen_name))
