import asyncio
import json 
from agents.mcp import MCPServerStdio

async def main():
    server = MCPServerStdio(
        name = "mobile-mcp",
        params={
            "command": "npx",
            "args": ["-y", "@mobilenext/mobile-mcp@latest"],
        },
    )

    async with server:
        #1. List available tools
        tools = await server.list_tools()
        print("=== AVAILABLE TOOLS ===")
        for t in tools:
            print(f"  {t.name}")
        print(f"\nTotal: {len(tools)} tools\n")

        #2. List connected devices
        print("=== DEVICES ===")
        result = await server.call_tool("mobile_list_available_devices", {})
        print(result)
        print()

        #3. List Elements on screen (the main one we need)
        print("=== ELEMENTS ON SCREEN ===")
        result = await server.call_tool("mobile_list_elements_on_screen", {
            "device": "RZCXA21GV9P"
        })
        print(result)
        print()

        # 4. Tap the "Enter Details" field (center of coordinates)
        # From our data: x=141, y=1422, width=885, height=136
        # Center: x = 141 + 885/2 = 583, y = 1422 + 136/2 = 1490
        print("=== TAP: Enter Details field ===")
        result = await server.call_tool("mobile_click_on_screen_at_coordinates", {
            "device": "RZCXA21GV9P",
            "x": 583,
            "y": 1490
        })
        print(result)
        print()

        # Wait for keyboard to appear
        await asyncio.sleep(1)

        # 5. Type text into the focused field
        print("=== TYPE: Entering test text ===")
        result = await server.call_tool("mobile_type_keys", {
            "device": "RZCXA21GV9P",
            "text": "TestAgent123",
            "submit": False
        })
        print(result)
        print()

        # 6. List elements again to verify text was entered
        print("=== VERIFY: Elements after typing ===")
        result = await server.call_tool("mobile_list_elements_on_screen", {
            "device": "RZCXA21GV9P"
        })
        print(result)

if __name__ == "__main__":
    asyncio.run(main())