# qa/chat/intent.py — Parse natural language user input into structured pipeline commands

import json
import os
from enum import Enum
from typing import Literal

from dotenv import load_dotenv
load_dotenv()

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from qa.models.knowledge import KnowledgeBase


class ChatAction(str, Enum):
    EXECUTE = "execute"           # Run tests on the app
    EXPLORE = "explore"           # Discover screens/elements
    LIST_SCREENS = "list_screens" # Show what we know about the app
    ANSWER = "answer"             # Answer a general question (no pipeline call)
    CLARIFY = "clarify"           # User input is unclear, ask for more info


class ChatIntent(BaseModel):
    """Structured representation of what the user wants to do."""
    action: ChatAction = Field(description="What pipeline action to take")
    screens: list[str] = Field(default_factory=list, description="Screen names mentioned")
    element_filter: str = Field(default="", description="Element type or name to scope to (e.g. 'dropdown', 'Transaction Type')")
    test_values: list[str] = Field(default_factory=list, description="Specific values to test with (e.g., ['Cash Withdrawal', 'Driver Permit']). Empty = let plan pick.")
    use_different_values: bool = Field(default=False, description="True if user explicitly wants DIFFERENT values from previous run")
    response_text: str = Field(default="", description="Natural language response to show the user before/after action")
    clarification_question: str = Field(default="", description="If unclear, what to ask the user")


INTENT_SYSTEM_PROMPT = """You are the intent parser for a QA testing suite.

Your job: convert the user's natural language request into a structured command.

# Available Actions
- **execute**: Run tests on the app's elements. Fills fields with test values,
  observes validation, records PASS/FAIL. Triggered by: "test", "run tests",
  "validate", "check behavior of".
- **explore**: Discover / re-discover what's on the app's screens. Scans the
  live page, opens dropdowns to capture options, updates the knowledge base.
  Does NOT fill fields with test values — only reads structure. Triggered
  by: "explore", "extract", "re-extract", "scan", "re-scan", "map", "capture",
  "find elements", "update knowledge", "refresh KB", or when no knowledge
  exists yet.
- **list_screens**: Show the user what screens/elements we know about
- **answer**: General Q&A — explain capabilities, answer about past results, no pipeline call
- **clarify**: Set this when the request is ambiguous — fill in clarification_question

# Rules
1. If user says "test X on Y screen" → action=execute, screens=[Y], element_filter=X
2. If user says "test the dropdowns" → action=execute, element_filter="dropdown"
3. If user says "what screens do we have" → action=list_screens
4. If user says ANY of these → action=explore:
   - "explore", "extract", "re-extract", "scan", "re-scan"
   - "map the page", "capture elements", "find elements"
   - "update the knowledge base", "refresh KB"
   - "extract data from X" — this is about capturing element structure,
     NOT running tests to gather result data
   - App has no knowledge yet (KB empty)
5. Disambiguation: "extract data" / "get data about" / "list fields on" →
   EXPLORE (read structure), not EXECUTE (run tests). Only route to EXECUTE
   when the user explicitly says "test", "validate", "run", or "check".
6. If user is unclear (e.g., "do something") → action=clarify with a specific question
7. Map element types: "dropdown" → "dropdown", "input"/"field"/"text" → "text_input",
   "calendar"/"date" → "date_picker", "button" → "button"
8. NEVER invent screen names — only use the screens listed in the context
9. If user mentions specific values (e.g., "test with Cash Withdrawal", "use Member ID"),
   extract them into test_values list — use EXACT names from the dropdown options in context.
10. If user says "different values" / "different data" / "new data" / "other options" without specifying,
    set use_different_values=true. The plan will pick options not used recently.
11. If user says "test with X, Y, Z" → test_values=["X", "Y", "Z"]

# Output
Always include a brief response_text explaining what you're about to do (or why you're asking).
"""


async def parse_intent(
    user_message: str,
    knowledge: KnowledgeBase | None,
    chat_history: list[dict] | None = None,
    model: str = "gpt-5.1",
) -> ChatIntent:
    """Parse a user's chat message into a structured ChatIntent.

    Args:
        user_message: The user's natural language input
        knowledge: Current KB (for context — known screens/elements)
        chat_history: Previous messages for context
        model: LLM to use for parsing
    """
    # Build context summary
    context_lines = []
    if knowledge and knowledge.screens:
        context_lines.append(f"App: {knowledge.app.app_name} ({knowledge.app.platform.value})")
        context_lines.append(f"Known screens: {', '.join(knowledge.screen_names())}")
        for screen in knowledge.screens:
            elements_summary = []
            for el in screen.l0:
                if el.type.value not in ("nav_tab", "other") and "label" not in el.name.lower():
                    elements_summary.append(f"{el.name} ({el.type.value})")
            if elements_summary:
                context_lines.append(f"  {screen.screen_name}: {', '.join(elements_summary[:8])}")
    else:
        context_lines.append("No knowledge base loaded yet.")

    context_block = "\n".join(context_lines)

    # Build messages
    messages = [
        {"role": "system", "content": INTENT_SYSTEM_PROMPT + f"\n\n# Current Context\n{context_block}"},
    ]

    # Include recent chat history (last 4 messages) for follow-up handling
    if chat_history:
        for msg in chat_history[-4:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    # Call LLM with structured output
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

    try:
        response = await client.beta.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=ChatIntent,
            max_completion_tokens=500,
        )
        intent = response.choices[0].message.parsed
        if intent is None:
            return ChatIntent(
                action=ChatAction.CLARIFY,
                clarification_question="Sorry, I couldn't understand that. Could you rephrase?",
                response_text="I had trouble parsing your request.",
            )
        return intent
    except Exception as e:
        return ChatIntent(
            action=ChatAction.CLARIFY,
            clarification_question=f"Something went wrong parsing your request: {str(e)[:100]}",
            response_text="Error in intent parsing.",
        )
