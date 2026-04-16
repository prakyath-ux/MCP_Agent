# qa/orchestrators/llm_subtask.py — narrow one-shot LLM calls with JSON output
#
# The single-agent explore approach loses work because every turn drags the
# whole conversation along. Path B avoids that: each sub-task is a fresh,
# stateless call — system prompt + one user message + structured JSON out.
# No tools, no history, no reasoning chain to unwind.

import json
import os

from openai import AsyncOpenAI

from qa.engine.budget import BudgetTracker
from qa.engine.model_factory import compute_cost


async def llm_classify(
    system_prompt: str,
    user_content: str,
    schema: dict,
    *,
    model: str = "gpt-5.1",
    budget: BudgetTracker | None = None,
    label: str = "subtask",
) -> dict:
    """Run ONE stateless LLM call with JSON schema output.

    Args:
        system_prompt: instructions for the sub-task (what to look for).
        user_content: the actual input (usually a snapshot or snippet).
        schema: a JSON Schema dict — passed as response_format's json_schema.
        model: override default model.
        budget: if provided, tokens/cost are added to the shared run tracker.
        label: short tag shown in per-call console output.

    Returns:
        Parsed JSON matching `schema`. Returns {} on parse failure — caller
        decides whether that's fatal.
    """
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": schema,
        },
    )

    usage = response.usage
    input_toks = getattr(usage, "prompt_tokens", 0) or 0
    output_toks = getattr(usage, "completion_tokens", 0) or 0
    cached_toks = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details:
        cached_toks = getattr(details, "cached_tokens", 0) or 0

    cost = compute_cost(model, input_toks, cached_toks, output_toks)

    if budget is not None:
        budget.record_turn(input_toks, output_toks, cached_toks)

    print(
        f"    [llm {label}] in={input_toks} out={output_toks} "
        f"cached={cached_toks} ${cost:.4f}"
    )

    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"    [llm {label}] ⚠ JSON parse failed — raw: {raw[:200]!r}")
        return {}
