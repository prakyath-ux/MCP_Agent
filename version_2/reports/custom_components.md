# Custom-Built Components — AI QA Testing Agent

Everything we built on top of the LLM and the Chrome DevTools MCP tools. The LLM (GPT-5) provides the intelligence. Chrome DevTools MCP (Google) provides 26 browser control tools. Everything listed below is our custom engineering that sits between them.

---

## 1. Orchestrated Turn Loop

**File:** `version_2/orchestrator.py`
**What it does:** Instead of letting the LLM run freely (which causes costs to spiral), we run the LLM one turn at a time in a managed loop. Between each turn, we can inspect what happened, compress history, check budget, detect problems, and inject instructions.

**Why it matters:** This is the core of v2. Without it, a single run costs $0.25-0.43. With it, runs cost $0.13-0.19. It gives us full control over what the LLM sees and does at every step.

**How it works:**
- Calls `Runner.run(max_turns=1)` inside a `for` loop
- After each turn: extract results → update summary → check budget → compact history → feed back to LLM
- The LLM doesn't know it's being managed — it thinks it's having one continuous conversation

---

## 2. History Compactor

**File:** `version_2/compactor.py`
**What it does:** After every turn, takes the full conversation history and compresses it. Keeps the last 1-2 turns raw (so the LLM remembers what just happened) and replaces everything before that with a short summary.

**Why it matters:** Without compaction, the conversation grows every turn — 53K tokens by turn 46 in v1. With compaction, it stays flat at ~9-12K tokens. Fewer tokens = lower cost.

**Key features:**
- `compact_history()` — replaces old turns with a rolling summary
- `update_summary()` — builds a one-line description of each action
- `_get_last_n_turns()` — keeps recent turns intact, handles GPT-5 reasoning item pairs
- Failure-aware summaries (Fix B) — records WHY actions failed, not just that they happened

**Before Fix B:** `"Clicked 1_33. Clicked 1_33. Clicked 1_33."` (LLM retries endlessly)
**After Fix B:** `"click(1_33) — FAILED: element not interactable, overlay blocking."` (LLM knows to skip)

---

## 3. Snapshot Filter

**File:** `version_2/snapshot_filter.py`
**What it does:** When the LLM takes a page snapshot (accessibility tree), this filter runs on the result before sending it back. It strips out decorative elements (icons, spacers, dividers) and collapses long dropdown lists (190 countries → 5 + "...collapsed").

**Why it matters:** A raw snapshot of a page with a country dropdown is ~19K tokens. After filtering, it's ~2K tokens. This directly reduces input cost.

**Key features:**
- `filter_snapshot()` — main function, runs all filter rules
- Removes elements with decorative roles (separator, presentation, img without alt)
- Collapses dropdown options beyond `MAX_DROPDOWN_OPTIONS` (default: 5)
- Preserves all interactive elements (inputs, buttons, links)

---

## 4. Loop Detection (Fix A)

**File:** `version_2/orchestrator.py` → `_detect_loop()`
**What it does:** Tracks the last 6 actions the LLM takes. If it tries the same element 3+ times, injects a warning message telling it to stop retrying and move on.

**Why it matters:** When history is compacted, the LLM forgets it already tried something and retries it. In one run, it tried a dropdown 22 times — wasting $0.15. Loop detection stops this after 3 attempts.

**How it works:**
- Maintains a window of last `LOOP_WINDOW` (6) turns
- Counts how many times each target element appears
- If count >= `LOOP_THRESHOLD` (3), injects a user message: "LOOP DETECTED: Skip this element, mark as failed, move on"
- Ignores observation tools (take_snapshot, evaluate_script) — only counts interactive actions (click, fill)

---

## 5. Enriched Failure Summaries (Fix B)

**File:** `version_2/compactor.py` → `_is_failure()`, `_describe_action()`
**What it does:** When building the rolling summary, detects if a tool call failed and includes the failure reason in the summary.

**Why it matters:** Without this, the summary says "Clicked 1_33" — the LLM doesn't know it failed. With this, it says "click(1_33) — FAILED: element not interactable" — the LLM knows not to retry.

**Failure signals detected:** error, fail, not found, unable, cannot, timeout, not visible, not interactable, blocked, overlay

---

## 6. Phase Switcher (Pass 2a → 2b)

**File:** `version_2/orchestrator.py`
**What it does:** In testcase mode, the LLM first generates a test plan (Phase 2a). The orchestrator detects the `## TEST PLAN` marker in the output, captures the plan, rebuilds the agent with a different prompt (execution mode), and feeds the plan as input for Phase 2b.

**Why it matters:** Separates planning (expensive reasoning, one-time) from execution (cheap, repetitive). The LLM plans what to test once, then executes mechanically without re-thinking each test.

**How it works:**
- Detects `TESTCASE_PLAN_MARKER` ("## TEST PLAN") in the agent's output
- Captures the test plan text
- Creates a new Agent instance with `TESTCASE_EXEC_PROMPT`
- Feeds the plan as input: "Here is your test plan. Execute ALL test cases now."
- Resets rolling summary, continues the turn loop

---

## 7. Knowledge Storage (Pass 1 → Pass 2 Bridge)

**File:** `version_2/orchestrator.py` → `_save_knowledge()`, `_load_knowledge()`
**What it does:** After Pass 1 (exploration), extracts the `## KNOWLEDGE` JSON block from the agent's output and saves it to `version_2/knowledge/<url>.json`. Before Pass 2, loads this JSON and injects it into the test planning prompt.

**Why it matters:** This is how the agent "remembers" what it learned across separate runs. Pass 1 knowledge includes field names, XPaths, types, behaviors, issues, dropdown options — everything Pass 2 needs to generate and execute test cases.

**Storage format:** JSON file per URL, with metadata (timestamp, app name, source)

---

## 8. Budget & Runaway Protection

**File:** `version_2/orchestrator.py`, `version_2/config.py`
**What it does:** Multiple layers of cost protection:

| Guard | What It Does | Threshold |
|-------|-------------|-----------|
| Total budget cap | Kills the run if total cost exceeds limit | $1.00 |
| Budget warning | Prints warning when approaching limit | 50% of budget |
| Runaway detection | Warns if any single turn costs 2x the average | 2x multiplier |
| Per-test budget | Kills individual test if too expensive | $0.05 per test |

**Why it matters:** LLM costs are unpredictable. A single bad turn (huge snapshot, long output) could blow the budget. These guards prevent runaway spending.

---

## 9. Live Turn Logger

**File:** `version_2/orchestrator.py` → `LiveTurnLogger` class
**What it does:** Hooks into the OpenAI Agents SDK's lifecycle events. After every LLM response, extracts token counts (input, output, cached) and the action taken, then prints a live table to the terminal.

**Output format:**
```
Turn  Action                 Target              Input  Cached    Out  Cache%
----- ---------------------- -------------------- ------- ------- ------ -------
1     navigate_page          https://qa-tq-awp.im   5,264   5,120    408     97%
2     take_snapshot                                 5,711   5,632     18     99%
3     fill                   1_17                   8,952   5,248    400     59%
```

**Why it matters:** Real-time visibility into what the agent is doing, how much it's costing, and whether caching is working. Essential for debugging and cost monitoring.

---

## 10. End-of-Run Nudge

**File:** `version_2/orchestrator.py`
**What it does:** When the agent has only 3 turns left before hitting the turn limit, injects a message telling it to stop exploring and produce its final report immediately.

**Why it matters:** Without the nudge, the agent might run out of turns mid-action and produce no report — wasting the entire run. The nudge ensures we always get structured output (results table, XPaths, bugs found).

**Mode-aware:** Different nudge messages for exploration mode ("extract XPaths now") vs testcase mode ("produce final report now").

---

## 11. Multi-Mode CLI

**File:** `version_2/run.py`, `version_2/config.py`
**What it does:** Single entry point that supports multiple run modes with different turn limits and behaviors:

| Mode | Turns | Purpose |
|------|-------|---------|
| `safe_test` | 3 | Quick smoke test — navigate and snapshot only |
| `recon` | 15 | Extract XPaths without filling any fields |
| `poc_short` | 30 | Short proof of concept with data entry |
| `poc` | 120 | Full page exploration + fill + knowledge dump |
| `testcase` | 25 | Phase 2a planning + Phase 2b execution |
| `full` | 600 | Full multi-page application test |

**Usage:** `python run.py <mode> [url] [app_name]`
Defaults to TECU if no URL provided.

---

## 12. System Prompt Engineering

**File:** `version_2/prompts.py`
**What it does:** Three carefully engineered prompts that control the LLM's behavior:

| Prompt | Purpose | Key Instructions |
|--------|---------|-----------------|
| `SYSTEM_PROMPT` | Pass 1 exploration | ReAct reasoning, 3 mental stages, fast-paths for known issues, XPath extraction, knowledge JSON output |
| `TESTCASE_PLAN_PROMPT` | Phase 2a planning | Read knowledge JSON, generate numbered test plan with CSS selectors, priority ordering |
| `TESTCASE_EXEC_PROMPT` | Phase 2b execution | Execute tests via evaluate_script only, no snapshots, batch tests, report format |

**Optimization levers built in:**
- Lever 1: Skip elements with correct defaults, fast-paths for known issues
- Lever 2: Concise reasoning for simple actions, verbose only on failures
- Lever 3: Close dropdowns before snapshots, use JS for large lists
- Multi-form handling: Ignore login forms, prioritize URL-matching form

---

## 13. Usage Tracker

**File:** `state/usage_tracker.py`
**What it does:** Logs every run's token usage, cost, duration, and cache efficiency to an Excel file. Calculates real cost (with caching discount), no-cache cost, and savings.

**Why it matters:** Persistent cost tracking across all runs. Feeds into the Streamlit dashboard for trend analysis.

---

## 14. State Tracker

**File:** `state/tracker.py`
**What it does:** Tracks the current run's state — which URL is being tested, start/end time, completion status. Used for logging and debugging.

---

## Summary

| # | Component | File | Lines of Code | Purpose |
|---|-----------|------|---------------|---------|
| 1 | Orchestrated Turn Loop | orchestrator.py | ~120 | Managed LLM execution with control between turns |
| 2 | History Compactor | compactor.py | ~230 | Compress conversation history to reduce costs |
| 3 | Snapshot Filter | snapshot_filter.py | ~150 | Trim page snapshots before sending to LLM |
| 4 | Loop Detection | orchestrator.py | ~40 | Stop the LLM from retrying failed actions |
| 5 | Failure Summaries | compactor.py | ~30 | Include failure reasons in history summaries |
| 6 | Phase Switcher | orchestrator.py | ~25 | Switch from test planning to test execution |
| 7 | Knowledge Storage | orchestrator.py | ~80 | Save/load page knowledge between passes |
| 8 | Budget Protection | orchestrator.py | ~20 | Multi-layer cost guards |
| 9 | Live Turn Logger | orchestrator.py | ~60 | Real-time terminal output with token stats |
| 10 | End-of-Run Nudge | orchestrator.py | ~15 | Force report generation before turn limit |
| 11 | Multi-Mode CLI | run.py + config.py | ~30 | 6 run modes with different behaviors |
| 12 | Prompt Engineering | prompts.py | ~360 | 3 prompts with optimization levers |
| 13 | Usage Tracker | usage_tracker.py | ~80 | Persistent cost logging to Excel |
| 14 | State Tracker | tracker.py | ~60 | Run state management |
| | **Total** | | **~1,300** | |
