# AI QA Testing Agent — Architecture Flow

```
╔══════════════════════════════════════════════════════════════════════╗
║                    AI QA TESTING AGENT — FULL FLOW                  ║
║                    Status: Waiting for API Key                      ║
╚══════════════════════════════════════════════════════════════════════╝


  USER
   │
   │  Provides: URL + app name + test files
   │  "Test https://qa-tq-awp.impactodigifin.xyz/newapplication"
   │
   ▼
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  agent.py — THE HOST (orchestrator)                    ✅ BUILT  │
│                                                                  │
│  Responsibilities:                                               │
│  ├── Load system prompt (personality)                            │
│  ├── Configure SDK + MCP connection                              │
│  ├── Start the state tracker                                     │
│  ├── Launch the agent via query()                                │
│  ├── Monitor messages from Claude                                │
│  ├── Enforce guardrails (max turns, budget, loops)               │
│  └── Report results at the end                                   │
│                                                                  │
│  Key settings:                                                   │
│  ├── max_turns = 600                                             │
│  ├── max_budget_usd = 15.0                                       │
│  └── model = sonnet-4.6 ($3/$15 per MTok)                       │
│                                                                  │
└──────────┬───────────────────────────────┬───────────────────────┘
           │                               │
           │ loads                          │ initializes
           ▼                               ▼
┌─────────────────────┐      ┌──────────────────────────────┐
│                     │      │                              │
│  system_prompt.py   │      │  state/tracker.py            │
│  ✅ BUILT           │      │  ✅ BUILT + TESTED           │
│                     │      │                              │
│  4 Techniques:      │      │  Warm Memory Layer:          │
│  ├── ReAct          │      │  ├── RunState (run metadata) │
│  │   Thought→       │      │  ├── SectionRecord (pages)   │
│  │   Action→        │      │  ├── FieldRecord (inputs)    │
│  │   Observation    │      │  ├── FailureRecord (errors)  │
│  │                  │      │  │                            │
│  ├── Few-Shot CoT   │      │  Methods:                    │
│  │   5 examples:    │      │  ├── start_run()             │
│  │   1. Text field  │      │  ├── discover_section()      │
│  │   2. API dropdown│      │  ├── discover_fields()       │
│  │   3. File upload │      │  ├── update_field()          │
│  │   4. API failure │      │  ├── log_failure()           │
│  │   5. Page nav    │      │  ├── check_for_loop()        │
│  │                  │      │  ├── get_summary()           │
│  ├── Reflexion      │      │  ├── save() / load()         │
│  │   Learn from     │      │  └── export() / restore()   │
│  │   failures,      │      │                              │
│  │   adapt strategy │      │  Saves to:                   │
│  │                  │      │  state/runs/run_<id>.json    │
│  └── Self-          │      │                              │
│      Consistency    │      │  Loop detection:             │
│      Multiple       │      │  deque(maxlen=5)             │
│      interpretations│      │  ≤2 unique → loop!           │
│      before acting  │      │                              │
│                     │      │                              │
│  ~2,829 tokens      │      │                              │
│  ~11,316 chars      │      │                              │
│                     │      │                              │
└─────────────────────┘      └──────────────────────────────┘

           │
           │ agent.py calls query() ──── ⏳ NEEDS API KEY
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  Claude Agent SDK (CLIENT)                         ✅ INSTALLED  │
│  pip: claude-agent-sdk v0.1.44                                   │
│                                                                  │
│  What it does:                                                   │
│  ├── Sends system prompt + user message to Claude API            │
│  ├── Receives Claude's response (text + tool calls)              │
│  ├── Executes tool calls on the MCP server                       │
│  ├── Sends tool results back to Claude                           │
│  ├── Repeats until max_turns or Claude says "done"               │
│  └── Streams messages back to agent.py                           │
│                                                                  │
│  You don't build this — Anthropic built it                       │
│                                                                  │
└──────────────────────┬───────────────────────────────────────────┘
                       │
                       │ stdio transport (subprocess pipes)
                       │ "type": "stdio" in config
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  Chrome DevTools MCP (SERVER)                      ✅ INSTALLED  │
│  npm: chrome-devtools-mcp v0.18.1                                │
│                                                                  │
│  26 Tools exposed:                                               │
│                                                                  │
│  Navigation          Observation          Interaction            │
│  ├── navigate_page   ├── take_snapshot    ├── click              │
│  ├── go_back         ├── take_screenshot  ├── type               │
│  ├── go_forward      ├── evaluate_script  ├── fill               │
│  └── wait_for        │                    ├── select_option      │
│                      │                    ├── check / uncheck    │
│  Monitoring          │                    ├── upload_file        │
│  ├── list_network_   │                    ├── press_key          │
│  │   requests        │                    └── handle_dialog      │
│  ├── get_network_    │                                           │
│  │   request         Browser                                     │
│  ├── list_console_   ├── emulate                                 │
│  │   messages        └── close_page                              │
│  └── get_console_                                                │
│      message         You don't build this — Google built it      │
│                                                                  │
└──────────────────────┬───────────────────────────────────────────┘
                       │
                       │ Chrome DevTools Protocol (CDP)
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  Google Chrome Browser                             ✅ INSTALLED  │
│                                                                  │
│  The actual web app being tested:                                │
│  https://qa-tq-awp.impactodigifin.xyz/newapplication             │
│                                                                  │
│  TECU Credit Union — Loan Application Form                       │
│  6 pages, 153 steps, ~104 clicks, ~47 inputs                    │
│                                                                  │
│  Page 1: Contact Info                                            │
│  Page 2: Documents                                               │
│  Page 3: Additional Details                                      │
│  Page 4: Other Products                                          │
│  Page 5: PEP/FATCA                                               │
│  Page 6: PDF/Other Details                                       │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

## One Action Cycle

This repeats 350-500 times per full run.

```
  Claude (via SDK)                MCP Server              Chrome
  ────────────────                ──────────              ──────

  THOUGHT: "I see a
  form field labeled
  First Name"
       │
       │ OBSERVE
       ├──── take_snapshot() ────►├── reads DOM ─────────►│
       │◄─── accessibility tree ──┤◄── elements, uids ────┤
       │
  THOUGHT: "This is a
  text field, I need
  to enter a name"
       │
       │ ACT
       ├──── fill(uid, "Roman") ─►├── types in field ────►│
       │◄─── success ─────────────┤◄── field updated ─────┤
       │
       │ VERIFY
       ├──── take_snapshot() ────►├── reads DOM again ───►│
       │◄─── field shows "Roman" ─┤◄── verified ──────────┤
       │
  THOUGHT: "Field filled
  successfully. Next
  field..."                      ┌─────────────────────┐
       │                         │                     │
       ├── update_field() ──────►│  state/tracker.py   │
       │   "First Name=filled"   │  saves to JSON      │
       │                         │                     │
       │                         └─────────────────────┘
       │
       ▼
  (next action cycle...)
```

## Failure Cycle (when something breaks)

```
  Claude                          MCP Server              Chrome
  ──────                          ──────────              ──────

  ACTION: click(dropdown)
       ├──── click(uid) ─────────►├── clicks ────────────►│
       │◄─── nothing happened ────┤◄── dropdown empty ────┤
       │
  REFLECTION:                     │
  "What I tried: click dropdown"  │
  "Expected: options appear"      │
  "Got: empty list"               │
       │                          │
       │ DIAGNOSE                 │
       ├── list_network_requests()►├── checks network ───►│
       │◄── GET /api returned 500 ┤◄── server error ──────┤
       │                          │
       ├── list_console_messages()►├── checks console ───►│
       │◄── "Error: fetch failed" ┤◄── JS error found ────┤
       │
  THOUGHT: "Backend API
  returned 500. This is         ┌─────────────────────┐
  a server bug, not UI"        │                     │
       │                        │  state/tracker.py   │
       ├── log_failure() ──────►│  records:           │
       │   error + diagnosis    │  - error message    │
       │   + evidence           │  - diagnosis        │
       │                        │  - network evidence │
       │                        │  - console evidence │
       │                        │  saves to JSON      │
       ├── update_field() ─────►│  "Country Code"     │
       │   status="failed"      │  status=failed      │
       │                        └─────────────────────┘
       │
  THOUGHT: "Skip this field,
  continue with remaining..."
       │
       ▼
  (next action cycle...)
```

## Guardrails (safety nets)

```
  ┌─────────────────────────────────────────────────────────┐
  │  GUARDRAIL              TRIGGER           ACTION        │
  │  ──────────             ───────           ──────        │
  │                                                         │
  │  Max turns              turn > 600        Stop run      │
  │  Max budget             cost > $15        Stop run      │
  │  Loop detection         ≤2 unique in      Skip element, │
  │                         last 5 actions    move on       │
  │  Max retries            3 fails on        Log + skip    │
  │                         same element                    │
  │  Consecutive failures   5 in a row        Stop section  │
  │  Unexpected page        CAPTCHA, 404,     Stop + report │
  │                         login screen                    │
  │  Browser crash          Chrome dies       Save state,   │
  │                                           try restart   │
  └─────────────────────────────────────────────────────────┘
```

## Three-Tier Memory

```
  ┌────────────────┐  ┌──────────────────┐  ┌────────────────┐
  │  HOT MEMORY    │  │  WARM MEMORY     │  │  COLD MEMORY   │
  │  (context      │  │  (JSON tracker)  │  │  (disk files)  │
  │   window)      │  │                  │  │                │
  │                │  │  ✅ BUILT        │  │  Future:       │
  │  Current page  │  │                  │  │  screenshots,  │
  │  snapshot,     │  │  state/runs/     │  │  network logs, │
  │  last few      │  │  run_<id>.json   │  │  full reports  │
  │  actions,      │  │                  │  │                │
  │  Claude's      │  │  Tracks:         │  │                │
  │  reasoning     │  │  sections,       │  │                │
  │                │  │  fields,         │  │                │
  │  ~200K tokens  │  │  failures,       │  │                │
  │  limit         │  │  progress        │  │                │
  │                │  │                  │  │                │
  │  Managed by    │  │  Managed by      │  │  Managed by    │
  │  SDK (auto)    │  │  tracker.py      │  │  agent.py      │
  └────────────────┘  └──────────────────┘  └────────────────┘
```
