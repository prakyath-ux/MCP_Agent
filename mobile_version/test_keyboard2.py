# Quick test: ADB keyevent for keyboard dismiss
# Run: python test_keyboard2.py

import asyncio
import json
import subprocess
from agents.mcp import MCPServerStdio

DEVICE = "RZCXA21GV9P"


async def call(server, tool, args):
    args["device"] = DEVICE
    result = await server.call_tool(tool, args)
    return result.content[0].text if result.content else ""


def check_keyboard(raw: str) -> bool:
    """Check content frame height — < 2150 means keyboard is up."""
    try:
        elements = json.loads(raw[raw.index("["):])
        for e in elements:
            if e.get("identifier") == "android:id/content":
                h = e["coordinates"]["height"]
                print(f"    content height: {h} → {'KEYBOARD UP' if h < 2150 else 'KEYBOARD DOWN'}")
                return h < 2150
    except (ValueError, json.JSONDecodeError):
        pass
    return False


def adb_keyevent(code: int):
    """Send ADB keyevent directly."""
    subprocess.run(["adb", "-s", DEVICE, "shell", "input", "keyevent", str(code)],
                   capture_output=True, timeout=5)


async def test_method(server, name: str, dismiss_fn):
    # Open keyboard by tapping Enter Details
    await call(server, "mobile_click_on_screen_at_coordinates", {"x": 583, "y": 1548})
    await asyncio.sleep(1.0)

    raw = await call(server, "mobile_list_elements_on_screen", {})
    kb_up = check_keyboard(raw)
    print(f"\n  [{name}]")
    if not kb_up:
        print(f"    SKIP — keyboard didn't open")
        return

    # Try dismiss
    await dismiss_fn() if asyncio.iscoroutinefunction(dismiss_fn) else dismiss_fn()
    await asyncio.sleep(0.8)

    raw = await call(server, "mobile_list_elements_on_screen", {})
    kb_after = check_keyboard(raw)
    print(f"    Result: {'FAILED' if kb_after else 'SUCCESS'}")


async def main():
    print("=== Keyboard Dismiss Test v2 (ADB keyevents) ===\n")

    async with MCPServerStdio(
        name="mobile-mcp",
        params={"command": "npx", "args": ["-y", "@mobilenext/mobile-mcp@latest"]},
        cache_tools_list=True,
        client_session_timeout_seconds=30.0,
    ) as server:

        # Method 1: KEYCODE_ESCAPE (111) — hides keyboard on most Android
        await test_method(server, "ADB keyevent 111 (ESCAPE)", lambda: adb_keyevent(111))

        # Method 2: KEYCODE_BACK (4) via ADB — same as BACK but system level
        await test_method(server, "ADB keyevent 4 (BACK)", lambda: adb_keyevent(4))

        # Method 3: Toggle soft input via ADB
        await test_method(server, "ADB toggle soft input",
            lambda: subprocess.run(
                ["adb", "-s", DEVICE, "shell", "cmd", "input_method", "hide"],
                capture_output=True, timeout=5))

        # Method 4: ENTER key (66) — submits and may dismiss
        await test_method(server, "ADB keyevent 66 (ENTER)", lambda: adb_keyevent(66))

        print("\n=== Done ===")


asyncio.run(main())
