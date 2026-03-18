# AI Agent Research — Key Findings

**Decision: Chrome DevTools MCP (Google) as our MCP server**

---

## A. THE BRAIN — Claude + Agent SDK

### A1: Claude Agent SDK
- **Package:** `pip install claude-agent-sdk` (Python, Alpha, v0.1.39)
- It's "Claude Code as a library" — gives us the autonomous agent loop
- Wraps Claude Code CLI as a subprocess, handles tool execution internally
- Two modes: `query()` for one-off tasks, `ClaudeSDKClient` for multi-turn (we use this)
- Connects to MCP servers via config — just point it to Chrome DevTools MCP and it works
- Requires Anthropic API key (per-token pricing, not Pro/Max subscription)
- Has `max_turns` and `max_budget_usd` for safety limits

### A2: Agent Loop Mechanics
- The loop: Claude decides → SDK executes tool → result goes back → Claude decides again → repeat
- Claude responds with `stop_reason="tool_use"` when it wants a tool, `"end_turn"` when done
- SDK handles everything: executing tools, sending results back, managing history, error handling
- We just consume a stream of messages — SystemMessage, AssistantMessage, ResultMessage
- Without the SDK, we'd code the entire while-loop, tool execution, history management ourselves

### A3: Tool Calls for Our Agent
- **16 total tool types** the agent will use
- MCP tools (13): navigate, click, fill, select_option, upload_file, take_snapshot, take_screenshot, evaluate_script, list_network_requests, list_console_messages, wait_for, handle_dialog, emulate
- SDK tools (3): Read (reference files), Write (save reports), Task (subagents)
- Per test run of 153 CSV steps: ~350-500 individual MCP calls (each step needs action + verification, some need multiple calls)

### A4: Multi-Turn Conversation & Memory
- Claude has NO memory — entire conversation history is re-sent every API call
- Context window: 200K tokens standard, 1M beta
- At ~1,500 tokens per action, we hit 200K limit around action 120 — our 153-step E2E flow won't fit in one go
- **Solutions (best to worst):**
  1. Tool result clearing — removes old DOM snapshots, keeps action log (savings depend on workload — no published %)
  2. External state tracker — JSON file tracking completed fields, injected each turn
  3. Break into sessions per form page — 200K is plenty for one section
  4. 1M token context beta — fits ~500 actions but costs 2x beyond 200K
- Source: [Context Editing Docs](https://platform.claude.com/docs/en/build-with-claude/context-editing)

### A5: Model Selection
- **Sonnet 4.6** beats Opus on the Finance Agent benchmark (63.3% vs 60.1%) and nearly matches it on SWE-bench (79.6% vs 80.8%) — at 60% cost, our primary model
- **Haiku 4.5** for simple tasks (verification, data generation) — 5x cheaper
- **Opus 4.6** for failure diagnosis — stronger reasoning on complex problems
- Source: [anthropic.com/news/claude-sonnet-4-6](https://www.anthropic.com/news/claude-sonnet-4-6)

| Task | Model | Input/Output per MTok |
|------|-------|-----------------------|
| Main loop / execution | Sonnet 4.6 | $3 / $15 |
| Simple verification | Haiku 4.5 | $1 / $5 |
| Failure diagnosis | Opus 4.6 | $5 / $25 |

- Estimated cost per full test run (~350-500 MCP calls for 153 CSV steps): $5.50 - $12 with mixed models
- Start with Sonnet for everything (MVP), add Haiku/Opus later

---

## B. THE HANDS — Chrome DevTools MCP

### B1: All 26 Tools

| Category | Tools |
|----------|-------|
| Input (8) | click, fill, fill_form, hover, drag, press_key, upload_file, handle_dialog |
| Navigation (6) | navigate_page, new_page, select_page, list_pages, close_page, wait_for |
| Debugging (5) | take_snapshot, take_screenshot, evaluate_script, list_console_messages, get_console_message |
| Network (2) | list_network_requests, get_network_request |
| Emulation (2) | emulate, resize_page |
| Performance (3) | performance_start_trace, performance_stop_trace, performance_analyze_insight |

### B2: DOM Snapshots
- Returns **accessibility tree** (not raw HTML) — much smaller, more meaningful
- Format: `uid=1_6 textbox "First Name" focusable`
- Every element gets a unique `uid` — agent references this to click/fill
- Includes roles, names, states (checked, disabled, focused, expanded)
- `verbose: true` for full tree, `false` for filtered (interactive elements)

### B3: JavaScript Execution
- `evaluate_script` runs **any JavaScript** on the page
- Full DOM access, can modify elements, supports async
- Return values must be JSON-serializable
- Use for: extracting XPaths, counting elements, detecting field types beyond a11y tree

### B4: Network Monitoring
- Automatic — all requests captured from page load, no setup needed
- `list_network_requests`: shows method, URL, status code per request
- `get_network_request`: full headers + request body + response body
- Can filter by resource type (XHR, Fetch, Image, etc.)
- Key for diagnosing: "dropdown failed because API returned 500"

### B5: Console Messages
- Automatic — captured from page load
- `list_console_messages`: type (log/error/warning), message text
- `get_console_message`: full stack traces with source-mapped file locations
- Also captures browser issues (CORS, accessibility warnings)

### B6: Screenshots
- PNG (default), JPEG, or WebP
- Full viewport, full scrollable page, or specific element by uid
- ~1,000-1,600 tokens per typical screenshot
- Auto-saves to file if over 2MB

---

## C: MCP Alternatives — Why Chrome DevTools

### Options evaluated

| MCP Server | Builder | Browsers | Tools | Verdict |
|-----------|---------|----------|-------|---------|
| **Chrome DevTools MCP** | Google | Chrome | 26 | **Our choice** |
| Playwright MCP | Microsoft | Chrome, Firefox, WebKit | 26 | Strong alternative, cross-browser |
| Selenium MCP | Community | Chrome, Firefox, Edge, Safari | 20 | Slower (WebDriver), smaller community |
| BrowserTools MCP | AgentDesk | Chrome | 12 | Debugging only, not for automation |
| Browserbase MCP | Browserbase | Cloud Chrome | 8 | Paid ($39+/mo) |
| Puppeteer MCP | Anthropic | Chrome | 7 | Archived, do not use |
| Bright Data MCP | Bright Data | Cloud Chrome | 30 | Scraping tool, not QA |
| Skyvern MCP | Skyvern AI | Chrome | ~5 | CV-based, overkill for known apps |

### Why Chrome DevTools MCP over Playwright MCP

| | Chrome DevTools MCP | Playwright MCP |
|--|-------------------|---------------|
| Network inspection | Deep (headers + bodies) | Basic |
| Console monitoring | Full stack traces | Yes |
| Performance tracing | Yes (Core Web Vitals) | No |
| Failure diagnosis | Excellent | Good |
| Cross-browser | No (Chrome only) | Yes (3 engines) |
| Test generation | No | Yes (native) |

We chose Chrome DevTools because our primary goal is **intelligent testing with diagnosis** — understanding WHY things fail, not just that they failed. The deep network inspection, console monitoring with stack traces, and performance tracing give our agent the diagnostic power that other MCPs lack.Sirma@123


Playwright MCP remains a strong option if cross-browser testing becomes a requirement later.

---

## D. THE MEMORY — State + Context Management

### Best architecture: Three-tier memory

| Tier | What | Where | Token Cost |
|------|------|-------|------------|
| Hot context | Current page DOM + current action | In the 200K context window | ~10-20K per cycle |
| Warm state | Test progress tracker (which fields done) | External JSON file via MCP tool | ~200 tokens per read |
| Cold storage | Full results, screenshots, logs | Disk files, read on demand | 0 until needed |

### State tracking: External JSON tracker (not in-context)
- 153 steps in-context = ~2,900-4,600 tokens re-sent EVERY cycle (calculated: 153 steps × ~96 chars/step ÷ ~3.2 chars/token)
- Better: custom MCP tool (`update_test_state`, `get_progress`) that reads/writes a JSON file
- Agent calls it like any other tool — SDK handles it
- Returns only a summary ("Progress: 47/153 steps, Page 2, 2 failures") not full state

### Cross-page memory
- Before leaving a page: agent saves checkpoint ("Page 1: 47/47 done, 2 failures")
- Arriving on new page: agent reads summary only (~200 tokens, not full history)
- Full field details only pulled on demand for debugging

### Session persistence
- SDK gives every session a `session_id`
- Resume later with `resume="session-xyz"` — full history restored
- Can fork sessions (`fork_session=True`) to retry without losing original
- Combined with JSON state tracker: agent picks up from exact field where it stopped

---

## E. THE GUARDRAILS — Limits + Error Handling

### Safety limits

| Parameter | Setting | Purpose |
|-----------|---------|---------|
| `max_turns` | 600 | Hard ceiling on loop cycles |
| `max_budget_usd` | 15.0 | Hard ceiling on cost |
| Both together | Always | Double safety net |

### Timeouts
- MCP has no built-in per-tool timeout
- Solution: `PreToolUse` / `PostToolUse` hooks that track elapsed time
- If action takes >30 seconds, inject message: "Element not responsive, try different approach"

### Stuck/loop detection (we build this ourselves)
- Track last 5 actions — if only 1-2 unique actions in last 5, loop detected
- Use `PreToolUse` hook to deny the repeated action
- Inject message: "Loop detected. Skip this field, move to next"
- SDK's `max_turns` alone does NOT catch tight loops

### Browser crash recovery
- If Chrome dies: MCP connection drops, agent gets tool error
- Recovery: save state to JSON → restart Chrome → navigate back → resume from saved state
- Warning: do NOT open Chrome DevTools manually during a test run (crashes MCP Chrome — known bug)

### Graceful shutdown
- `Stop` hook fires when agent stops (any reason)
- Save final state snapshot to JSON with session_id and stop reason
- `ResultMessage` includes: `is_error`, `num_turns`, `total_cost_usd`, `duration_ms`

### Human intervention triggers (built into system prompt)
- After 3 consecutive failures on same element → stop and report
- Unexpected page (CAPTCHA, auth popup, error page) → stop and report
- Budget > 80% used → wrap up and report what's done

---

## F. THE COST — Tokens + Performance

### Tokens per action cycle

| Component | Tokens |
|-----------|--------|
| DOM snapshot | 8,000 - 15,000 (medium form page) |
| System prompt + history | 2,000 - 8,000 |
| Agent reasoning + tool call | 200 - 800 |
| Tool result | 100 - 2,000 |
| **Total per cycle** | **~10,000 - 20,000** |

Warning: Pages with large dropdowns (195 options) balloon snapshots to 15K+ tokens. Every dropdown option appears in the accessibility tree.

### Actual E2E test breakdown (from E2E_test_1.csv)

| Metric | Value |
|--------|-------|
| **Total CSV steps** | **153** |
| Across pages | 6 (Contact Info → Documents → Additional Details → Other Products → PEP/FATCA → PDF/Other Details) |
| click actions | ~104 (dropdown opens, option selects, toggles, buttons, "Save & Continue") |
| Input actions | ~47 (text fields, file paths, OTP digits) |
| keyboardinput actions | 2 (date picker year entries) |
| **Estimated MCP calls per run** | **~350-500** (each step = action + verification snapshot, plus diagnosis on failures) |

Note: 153 steps ≠ 153 fields. Many steps are navigation (Save & Continue), dropdown interactions (click open + click option = 2 steps), OTP entry (7 steps for 6 digits + verify), file uploads (click label + input file = 2 steps), and toggle clicks (PEP/FATCA has 17 yes/no clicks).

### Speed per action

| Operation | Time |
|-----------|------|
| take_snapshot | 1-5 seconds |
| click/fill action | 0.5-2 seconds |
| Claude API response | 2-8 seconds |
| **Total per cycle** | **~4-15 seconds** |


### Optimization priority

| Strategy | Savings | Effort |
|----------|---------|--------|
| Prompt caching | 90% on cached input | Just enable it |
| Token-efficient tool use | 14% on output | Free (Claude 4 default) |
| External state tracker | 3-5K per cycle | Build custom MCP tool |
| Context editing (tool result clearing) | Significant (workload-dependent) | Enable in SDK config |
| Mixed models (Haiku for simple) | 40-60% on top | Configure subagents |

### vs Current Playwright Suite

| | Current Suite | AI Agent |
|--|--------------|----------|
| Cost per run | $0 | $5-30 |
| Maintenance cost | Developer hours updating tests | Near zero (adapts automatically) |
| Break-even | — | If team spends >2-4 hrs/month maintaining tests |

---

## G. LLM Alternatives — Gemini vs Claude

| | Claude (Anthropic) | Gemini (Google) |
|--|-------------------|-----------------|
| Agent SDK | Claude Agent SDK | Google ADK (Agent Development Kit) |
| MCP support | Yes (native) | Yes (native — Google co-built MCP) |
| Chrome DevTools MCP | Works via SDK config | Works natively (same company) |
| Context window | 200K (1M beta) | 1M (Gemini 2.0) / 2M (Gemini 1.5 Pro) |
| Cost (comparable tier) | $3/$15 per MTok (Sonnet) | $1.25/$5 per MTok (Flash 2.0) |
| Agentic maturity | More proven (Claude Code) | Newer to agentic workflows |
| Best for | Complex reasoning, diagnosis | Cheaper runs, larger context |

Chrome DevTools MCP is built by Google — Gemini is their natural pairing. Larger context window (1M standard) would fit our entire 153-step E2E test without compaction. Significantly cheaper per token.

