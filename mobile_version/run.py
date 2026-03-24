# mobile_version/run.py — CLI entrypoint for mobile testing agent
# Usage:
#   python mobile_version/run.py                                          → safe test on default device + app
#   python mobile_version/run.py poc                                      → POC on default device + app
#   python mobile_version/run.py testcase                                 → Test cases using Pass 1 knowledge
#   python mobile_version/run.py recon DEVICE_ID com.example.app "App"    → Recon on custom device + app

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

    device_id = sys.argv[2] if len(sys.argv) > 2 else DEVICE_ID
    package_name = sys.argv[3] if len(sys.argv) > 3 else PACKAGE_NAME
    app_name = sys.argv[4] if len(sys.argv) > 4 else APP_NAME

    asyncio.run(run_orchestrated(device_id, package_name, app_name, mode))
