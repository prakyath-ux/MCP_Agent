# mobile_version/config.py — All constants for mobile testing agent

# ----- Model --------
MODEL = "gpt-5"
MODEL_MINI = "gpt-5-mini"

# ------ Target Application -----------
DEVICE_ID = "RZCXA21GV9P"              # From `adb devices`
PACKAGE_NAME = "net.impacto.B2U"        # Bank app package name
APP_NAME = "Bank App (B2U)"

# ------- Run Modes ---------------
TURN_LIMITS = {
    "safe_test": 5,         # Just list elements, no interaction
    "recon": 20,            # Explore screen, list all elements
    "poc_short": 40,        # Fill fields on one screen
    "poc": 150,             # Full exploration + fill + knowledge
    "testcase": 60,         # Plan + execute test cases (3 turns per test vs 1 in web)
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
}

# ------- Output -----------------------
RUNS_DIR = "mobile_version/runs"
PASS1_RUNS_DIR = "mobile_version/runs/pass1"
PASS2_RUNS_DIR = "mobile_version/runs/pass2"
