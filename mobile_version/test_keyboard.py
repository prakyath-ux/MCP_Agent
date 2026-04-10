# Quick test: what actually dismisses the keyboard on this app?
# Run: python test_keyboard.py

import asyncio
import json
from agents.mcp import MCPServerStdio

DEVICE = "RZCXA21GV9P"


async def call(server, tool, args):
    args["device"] = DEVICE
    result = await server.call_tool(tool, args)
    return result.content[0].text if result.content else ""


async def has_keyboard(server) -> bool:
    """Check if any element is focused (keyboard likely up)."""
    raw = await call(server, "mobile_list_elements_on_screen", {})
    try:
        elements = json.loads(raw[raw.index("["):])
        return any(e.get("focused") for e in elements)
    except (ValueError, json.JSONDecodeError):
        return False


async def test_dismiss(server, method_name: str, action):
    """Test a keyboard dismiss method."""
    # First, tap the Enter Details field to open keyboard
    await call(server, "mobile_click_on_screen_at_coordinates", {"x": 583, "y": 1548})
    await asyncio.sleep(1.0)

    kb_up = await has_keyboard(server)
    print(f"\n  [{method_name}]")
    print(f"    Keyboard after tap: {'UP' if kb_up else 'DOWN'}")

    if not kb_up:
        print(f"    SKIP — keyboard didn't open")
        return

    # Try the dismiss method
    await action()
    await asyncio.sleep(0.8)

    kb_after = await has_keyboard(server)
    print(f"    Keyboard after dismiss: {'UP (FAILED)' if kb_after else 'DOWN (SUCCESS)'}")


async def main():
    print("=== Keyboard Dismiss Test ===\n")
    print("Make sure the app is on iTeller screen with Enter Details visible.\n")

    async with MCPServerStdio(
        name="mobile-mcp",
        params={"command": "npx", "args": ["-y", "@mobilenext/mobile-mcp@latest"]},
        cache_tools_list=True,
        client_session_timeout_seconds=30.0,
    ) as server:

        # Method 1: Tap (540, 100) — original
        await test_dismiss(server, "Tap (540, 100) — old header", lambda:
            call(server, "mobile_click_on_screen_at_coordinates", {"x": 540, "y": 100}))

        # Method 2: Tap (540, 300) — current safe tap
        await test_dismiss(server, "Tap (540, 300) — current safe tap", lambda:
            call(server, "mobile_click_on_screen_at_coordinates", {"x": 540, "y": 300}))

        # Method 3: Tap (540, 500) — further down but above form
        await test_dismiss(server, "Tap (540, 500) — mid area", lambda:
            call(server, "mobile_click_on_screen_at_coordinates", {"x": 540, "y": 500}))

        # Method 4: Tap (540, 800) — title/label area
        await test_dismiss(server, "Tap (540, 800) — title area", lambda:
            call(server, "mobile_click_on_screen_at_coordinates", {"x": 540, "y": 800}))

        # Method 5: Keyboard hide button (bottom-right of keyboard)
        await test_dismiss(server, "Keyboard hide btn (1030, 2270)", lambda:
            call(server, "mobile_click_on_screen_at_coordinates", {"x": 1030, "y": 2270}))

        # Method 5b: Keyboard hide button — slightly different position
        await test_dismiss(server, "Keyboard hide btn (1050, 2250)", lambda:
            call(server, "mobile_click_on_screen_at_coordinates", {"x": 1050, "y": 2250}))

        # Method 6: BACK button
        await test_dismiss(server, "BACK button", lambda:
            call(server, "mobile_press_button", {"button": "BACK"}))

        # Method 6: Swipe down
        await test_dismiss(server, "Swipe DOWN", lambda:
            call(server, "mobile_swipe_on_screen", {"direction": "down"}))

        # Method 7: Tap + BACK combo
        async def tap_then_back():
            await call(server, "mobile_click_on_screen_at_coordinates", {"x": 540, "y": 300})
            await asyncio.sleep(0.3)
            await call(server, "mobile_press_button", {"button": "BACK"})
        await test_dismiss(server, "Tap (540,300) + BACK combo", tap_then_back)

        # Method 9: Find and tap the actual keyboard hide button
        # First, open keyboard and scan for the button
        await call(server, "mobile_click_on_screen_at_coordinates", {"x": 583, "y": 1548})
        await asyncio.sleep(1.0)
        raw = await call(server, "mobile_list_elements_on_screen", {})
        try:
            elements = json.loads(raw[raw.index("["):])
            print("\n  [Elements near bottom of screen when keyboard is up]")
            for e in elements:
                cy = e.get("coordinates", {}).get("y", 0) + e.get("coordinates", {}).get("height", 0) // 2
                if cy > 1800:
                    name = e.get("text", "") or e.get("label", "") or e.get("identifier", "")
                    etype = e.get("type", "").split(".")[-1]
                    cx = e.get("coordinates", {}).get("x", 0) + e.get("coordinates", {}).get("width", 0) // 2
                    print(f"    y={cy} x={cx} type={etype} name='{name}'")
        except (ValueError, json.JSONDecodeError):
            print("  Could not parse elements")

        print("\n=== Done ===")
        print("Use whichever method shows 'DOWN (SUCCESS)'")


asyncio.run(main())
