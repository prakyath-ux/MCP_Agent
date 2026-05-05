# qa/engine/model_factory.py — Build model configs for OpenAI/Groq/OpenRouter

import os

from agents import ModelSettings


# Model → provider auto-mapping
PROVIDER_DEFAULTS = {
    "gpt-5": "openai_chat",
    "gpt-5.1": "openai_chat",
    "gpt-5.2": "openai_chat",
    "gpt-5.2-pro": "openai_chat",
    "gpt-5.4": "openai_chat",
    "gpt-5.4-mini": "openai_chat",
    "gpt-5.4-nano": "openai_chat",
    "gpt-5.4-pro": "openai_chat",
    "gpt-5.5": "openai_chat",
    "gpt-5.5-pro": "openai_chat",
    "openai/gpt-oss-120b": "openrouter",
}

# Pricing per token (USD per token; OpenAI lists per 1M tokens, divided here).
# Pro variants do not support cached input — cached rate matches the uncached
# input rate so the bill stays accurate even if usage reports cached_tokens.
PRICING = {
    "gpt-5":          {"input": 1.25 / 1_000_000, "input_cached": 0.125 / 1_000_000, "output": 10.0  / 1_000_000},
    "gpt-5.1":        {"input": 1.25 / 1_000_000, "input_cached": 0.125 / 1_000_000, "output": 10.0  / 1_000_000},
    "gpt-5.2":        {"input": 1.75 / 1_000_000, "input_cached": 0.175 / 1_000_000, "output": 14.0  / 1_000_000},
    "gpt-5.2-pro":    {"input": 21.0 / 1_000_000, "input_cached": 21.0  / 1_000_000, "output": 168.0 / 1_000_000},
    "gpt-5.4":        {"input": 2.50 / 1_000_000, "input_cached": 0.25  / 1_000_000, "output": 15.0  / 1_000_000},
    "gpt-5.4-mini":   {"input": 0.75 / 1_000_000, "input_cached": 0.075 / 1_000_000, "output": 4.50  / 1_000_000},
    "gpt-5.4-nano":   {"input": 0.20 / 1_000_000, "input_cached": 0.02  / 1_000_000, "output": 1.25  / 1_000_000},
    "gpt-5.4-pro":    {"input": 30.0 / 1_000_000, "input_cached": 30.0  / 1_000_000, "output": 180.0 / 1_000_000},
    "gpt-5.5":        {"input": 5.00 / 1_000_000, "input_cached": 0.50  / 1_000_000, "output": 30.0  / 1_000_000},
    "gpt-5.5-pro":    {"input": 30.0 / 1_000_000, "input_cached": 30.0  / 1_000_000, "output": 180.0 / 1_000_000},
    "openai/gpt-oss-120b": {"input": 0.039 / 1_000_000, "input_cached": 0.039 / 1_000_000, "output": 0.19 / 1_000_000},
}


def build_model_config(model: str, provider: str = ""):
    """Build the model config object based on provider.

    Returns: (model_config, provider_label)
    """
    provider = provider or PROVIDER_DEFAULTS.get(model, "openai_chat")

    if provider == "groq":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        client = AsyncOpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
        )
        return OpenAIChatCompletionsModel(model=model, openai_client=client), "Groq Cloud (free)"

    elif provider == "openai_chat":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
        return OpenAIChatCompletionsModel(model=model, openai_client=client), "OpenAI (Chat Completions API)"

    elif provider == "openrouter":
        from openai import AsyncOpenAI
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        client = AsyncOpenAI(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
        )
        return OpenAIChatCompletionsModel(model=model, openai_client=client), "OpenRouter (cheap, no daily limit)"

    else:
        return model, "OpenAI (Responses API)"


def build_model_settings(model: str) -> ModelSettings:
    """Build model-specific settings."""
    # GPT-5.1 needs parallel_tool_calls disabled
    if "gpt-5.1" in model:
        return ModelSettings(parallel_tool_calls=False)
    return ModelSettings()


def get_pricing(model: str) -> dict:
    """Get pricing dict for a model. Falls back to gpt-5 pricing."""
    return PRICING.get(model, PRICING["gpt-5"])


def compute_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Compute real cost for a run."""
    prices = get_pricing(model)
    uncached = input_tokens - cached_tokens
    return (
        uncached * prices["input"]
        + cached_tokens * prices["input_cached"]
        + output_tokens * prices["output"]
    )
