# version_2/config.py All constants for version_2

#----- Model --------
MODEL = "gpt-5"
MODEL_MINI = "gpt-5-mini"   #3 necessary for multi-model on lever 5


# ------ Target Application -----------
TARGET_URL = "https://qa-tq-awp.impactodigifin.xyz/newapplication"
APP_NAME = "TECU Credit Union"


# ------- Run Modes ---------------
TURN_LIMITS = {
    "safe_test": 3,
    "recon": 15,
    "poc_short": 30,
    "poc": 120,
    "testcase": 25,
    "full": 600,
}

# ------- Knowledge Storage (Pass 1 → Pass 2 bridge) -------
KNOWLEDGE_DIR = "version_2/knowledge"   # Pass 1 saves here, Pass 2 reads from here

XPATH_NUDGE_BEFORE_END = 3             # Inject "extract XPaths now" N turns before end


# -------- Safety Limits -------------
MAX_BUDGET = 1.0
BUDGET_WARNING_PCT = 0.5
MAX_RETRIES_PER_FIELD = 3
LOOP_WINDOW = 6                    # Check last N turns for repeated targets
LOOP_THRESHOLD = 3                 # Inject warning after this many attempts on same element
COMPACT_AFTER_TURNS = 5            # Start compacting after this many turns
KEEP_LAST_N_TURNS = 2              # Keep last N turns raw, summarize the rest



#------- Pricing (per token) -------------
PRICING = {
    "gpt-5": {
        "input": 1.25 / 1_000_000,
        "input_cached": 0.125 / 1_000_000,
        "output": 10.0 / 1_000_000,
    },
}



# ------- Snapshot Filter -------------------
MAX_DROPDOWN_OPTIONS = 5           # Collapse List beyond this
REMOVE_DECORATIVE = True           # Strip non-iterative elements



# ------- Output -----------------------
RUNS_DIR = "state/runs"            # shared with v1, same dashboard
V2_RUNS_DIR = "version_2/runs"     # v2 results — separate for comparison
PASS1_RUNS_DIR = "version_2/runs/pass1"   # Exploration outputs (poc, recon, etc.)
PASS2_RUNS_DIR = "version_2/runs/pass2"   # Test case outputs (testcase mode)


# ---------- Test Files -----------------
PROFILE_PICTURE = "documents/Profile_picture/Profile_picture.png"

