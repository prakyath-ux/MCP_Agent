import os
from dotenv import load_dotenv

load_dotenv()

# Verify API key exists
api_key = os.getenv("ANTHROPIC API KEY")
if not api_key:
    print("WARNING: ANTHROPIC_API_KEY not set in .env")
else:
    print(f"API key loaded: {api_key[:2]}...{api_key[-2:]}")


# SDK + promtp import
from claude_agent_sdk import query, ClaudeAgentOptions
from prompts.system_prompt import SYSTEM_PROMPT

print("claude-agent-sdk imported successfully")
print(f"System prompt loaded: {len(SYSTEM_PROMPT)} chars (~{len(SYSTEM_PROMPT) // 4} tokens)")


# Agent configuration(No API calls here - just a config object)
options = ClaudeAgentOptions(
    system_prompt = SYSTEM_PROMPT,
    max_turns = 1,
    mcp_servers = {
        "chrome-devtools": {
            "type": "stdio",
            "commmand": "npx",
            "args": ["chrome-devtools-mcp@latest"],
        }
    },
)

print("Agent configured:")
print(f" MCP Server: chrome-devtools-mcp")
print(f" Max turns: {options.max_turns}")
print()
print("Everything wired. no API calls made. 0$ spent")
print("Next: run the agent with your approval.")