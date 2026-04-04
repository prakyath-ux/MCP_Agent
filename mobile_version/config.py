# mobile_version/config.py — All constants for mobile testing agent

# ----- Model --------
MODEL = "gpt-5"                      # OpenAI hosted — $1.25/$10 per MTok
# MODEL = "openai/gpt-oss-120b"      # Cheap via OpenRouter — $0.039/$0.19 per MTok
MODEL_MINI = "gpt-5-mini"

# ----- Model Provider --------
# "openai" = OpenAI Responses API, "openai_chat" = OpenAI Chat Completions
# "groq" = Groq Cloud (free, daily limit), "openrouter" = OpenRouter (cheap, no limit)
MODEL_PROVIDER = "openai_chat"

# ------ Target Application -----------
DEVICE_ID = "RZCXA21GV9P"              # From `adb devices`
PACKAGE_NAME = "net.impacto.B2U"        # Bank app package name
APP_NAME = "Bank App (B2U)"

# ------- Run Modes ---------------
TURN_LIMITS = {
    "safe_test": 5,         # Just list elements, no interaction
    "recon": 20,            # Explore screen, list all elements
    "poc_short": 40,        # Fill fields on one screen
    "poc": 40,              # Full exploration + fill + knowledge
    "testcase": 15,         # Plan + execute test cases
    "full": 600,
}

# ------- Knowledge Storage (Pass 1 → Pass 2 bridge) -------
KNOWLEDGE_DIR = "mobile_version/knowledge"

ELEMENT_NUDGE_BEFORE_END = 3    # Inject "extract elements now" N turns before end

# -------- Safety Limits -------------
MAX_BUDGET = 2.0
BUDGET_WARNING_PCT = 0.5
MAX_RETRIES_PER_FIELD = 3
LOOP_WINDOW = 6
LOOP_THRESHOLD = 3
COMPACT_AFTER_TURNS = 5
KEEP_LAST_N_TURNS = 2
PER_TEST_BUDGET = 0.05
RUNAWAY_MULTIPLIER = 2.0

# -------- Pass 2 Phase Detection ----------
TESTCASE_PLAN_MARKER = "## TEST PLAN"

# ------- Pricing (per token) -------------
PRICING = {
    "gpt-5": {
        "input": 1.25 / 1_000_000,
        "input_cached": 0.125 / 1_000_000,
        "output": 10.0 / 1_000_000,
    },
    "openai/gpt-oss-120b": {
        "input": 0.039 / 1_000_000,      # OpenRouter pricing
        "input_cached": 0.039 / 1_000_000,
        "output": 0.19 / 1_000_000,
    },
}

# ------- Output -----------------------
RUNS_DIR = "mobile_version/runs"
PASS1_RUNS_DIR = "mobile_version/runs/pass1"
PASS2_RUNS_DIR = "mobile_version/runs/pass2"
