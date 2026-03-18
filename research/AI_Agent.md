# AI-Powered Regression Testing Agent — Plan

**Date:** 2026-02-20
**Status:** Research & Planning (Frozen)
**Project:** Separate from deployed app — new standalone project

---

## What We're Building

A dynamic AI agent (Claude + Chrome DevTools MCP) that behaves like a real QA tester — not a script follower. Given a URL and context, it explores the app, understands what it's looking at, and decides how to test it on its own. No hardcoded steps, no manual recording, no CSV hand-editing.

---

## Current Workflow vs Proposed Workflow

```
 CURRENT (Manual, 3 Streamlit Pages)
 ====================================

 Human records      Human validates     Human clicks       Human reads
 clicks in browser  CSV + generates     "Run Now"          reports
       │             test data               │                 │
       ▼                 ▼                   ▼                 ▼
 ┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐
 │ RECORD   │───>│  GENERATE    │───>│   EXECUTE    │───>│  REPORT  │
 │ (Page 1) │    │  (Page 2)    │    │  (Page 3)    │    │          │
 └──────────┘    └──────────────┘    └──────────────┘    └──────────┘
  30-60 min         10-20 min           5-10 min           Manual


 PROPOSED (Dynamic AI Agent)
 ============================

 User gives initialization document (URL + context)
       │
       ▼
 ┌─────────────────────────────────────────────────────────┐
 │                   AI AGENT                               │
 │                                                          │
 │  Agent explores, understands, decides, acts, adapts      │
 │  No fixed pipeline — it figures out what to do           │
 │                                                          │
 │  Like a real QA tester on their first day:               │
 │  look around → understand the app → test intelligently   │
 └─────────────────────────────────────────────────────────┘
  Fully autonomous, dynamic, self-healing
```

---

## Linear vs Dynamic — Why Dynamic

```
 LINEAR (recipe):               DYNAMIC (chef):
 ────────────────               ───────────────

 Step 1 → Step 2 → Step 3      Agent looks at the situation
                                and DECIDES what to do next
 Hardcoded order.
 If something unexpected        Can go back, skip ahead,
 happens, it breaks.            try something new, adapt.

 You tell it WHAT to do.        You tell it the PURPOSE.
 It follows instructions.       It figures out the HOW.
```

The 4 capabilities (Discover, Plan, Execute, Report) still exist, but they are
**tools the agent can use anytime**, not a fixed sequence:

```
 Agent is exploring a new section...
   → uses DISCOVER capability

 Agent understands enough to start testing...
   → uses EXECUTE capability

 Agent hits a problem...
   → uses DIAGNOSE capability

 Agent finds a new section it hasn't seen...
   → uses DISCOVER again

 Agent is done with everything...
   → uses REPORT capability
```

---

## Architecture

```
                        ┌─────────────────────┐
                        │       USER          │
                        │                     │
                        │  Provides:          │
                        │  Initialization Doc │
                        │  (URL + context)    │
                        └─────────┬───────────┘
                                  │
                                  ▼
                        ┌─────────────────────┐
                        │    ORCHESTRATOR     │
                        │                     │
                        │  Manages agent      │
                        │  lifecycle, state,  │
                        │  tool routing       │
                        └────┬───────────┬────┘
                             │           │
                    ┌────────┘           └────────┐
                    ▼                             ▼
          ┌─────────────────┐           ┌─────────────────┐
          │   CLAUDE LLM    │           │  CHROME DEVTOOLS │
          │   (The Brain)   │           │  MCP SERVER      │
          │                 │           │  (The Hands)     │
          │  Decides what   │           │                  │
          │  to do next,    │◄─────────►│  Controls Chrome │
          │  analyzes DOM,  │   MCP     │  Reads DOM       │
          │  generates data,│  Protocol │  Network logs    │
          │  verifies       │           │  Console msgs    │
          │  results        │           │  Screenshots     │
          └─────────────────┘           └────────┬─────────┘
                                                 │
                                                 ▼
                                        ┌─────────────────┐
                                        │    CHROME       │
                                        │    BROWSER      │
                                        │                 │
                                        │  The web app    │
                                        │  being tested   │
                                        └─────────────────┘
```

**How it connects:**
- User provides an initialization document (not a checklist — just context)
- Orchestrator gives the agent the context and lets it loose
- Claude decides what to do, when, and how
- Chrome DevTools MCP is how Claude interacts with the browser
- Agent loops until it decides it's done

---

## The 3 Dynamic Stages

Unlike a linear pipeline, these are **mental stages** the agent moves through
naturally — just like a human would.

### Stage 1: EXPLORE — "What am I looking at?"

The agent opens the URL and looks around. It clicks, scrolls, reads — building
a picture of what the app is.

```
 Agent                          MCP                         Chrome
 ─────                          ───                         ──────
   │                              │                            │
   │── navigate to URL ──────────►│────── loads page ─────────►│
   │                              │                            │
   │── take snapshot ───────────►│────── reads page state ────►│
   │                              │                            │
   │◄── "I see a form with       │◄── elements, text, ────────│
   │     tabs, inputs, buttons"   │    layout, labels          │
   │                              │                            │
   │── click around, scroll ────►│────── explores sections ───►│
   │                              │                            │
   │◄── "There are 3 sections:   │◄── more elements found ────│
   │     Personal, Work, Docs"    │                            │
   │                              │                            │
   │── check network activity ──►│────── what APIs fire? ─────►│
   │                              │                            │
   │◄── "Dropdowns load from    │◄── API request log ─────────│
   │     /drop-downs endpoint"    │                            │
```

The agent is NOT following instructions here. It's **being curious**, like a
person opening a website for the first time.

---

### Stage 2: REALIZE — "Now I understand this app"

This is the key stage. After exploring, the agent forms its own **mental model**
of the application. Nobody tells it what the app is — it figures it out.

```
 What happens in the agent's "mind":
 ────────────────────────────────────

 "Okay, I've looked around. Here's what I understand:

  This is a LOAN APPLICATION FORM for TECU Credit Union.

  It has 3 sections:
  ├── Personal Details (name, email, phone, address, photo)
  ├── Work Details (employer, income, employment type)
  └── Documents (ID upload, pay stubs)

  Each section has a Next button to move forward.
  Some dropdowns depend on API calls.
  The phone field needs a country code first.
  Profile picture upload is a file input.

  NOW I KNOW what I'm testing and what matters."
```

**Why this matters:**

```
 WITHOUT realization:              WITH realization:
 ────────────────────              ──────────────────

 Agent sees: <input id="f47">     Agent sees: <input id="f47"
 Agent thinks: "A text field.      placeholder="Enter first name">
   I'll type random text."        Agent thinks: "This is the
 Types: "xk29df"                    FIRST NAME field. It's in
                                    Personal Details. I should
 Technically works.                 type a realistic name."
 But it's garbage.                 Types: "Roman"
```

Realization = the agent understands the **purpose** of what it sees, not just
the HTML structure.

---

### Stage 3: ACT DYNAMICALLY — "I'll test this my way"

Now the agent tests — but it decides what to do based on its understanding.
It doesn't follow a checklist. It makes decisions in real-time.

```
 Agent's internal thought process:
 ─────────────────────────────────

 "I'll start with Personal Details since it's the first tab.

  Let me fill first name... done, it accepted 'Roman'.
  Now last name... done.
  Email... done.

  Phone number — wait, I need to select country code first.
  Let me click the country dropdown...
  I see options loading from an API. Let me pick India (+91).
  Now I'll enter the phone number.

  Hmm, the Next button is still greyed out.
  Let me scroll down — oh, there's a required field I missed.
  Date of birth. Let me fill that.

  Now Next works. Moving to Work Details section.

  This is a new section — let me look at what's here before
  I start filling..."              ← GOES BACK TO EXPLORE

 The agent moved from ACTING back to EXPLORING naturally.
 No one told it to. It decided to because it's a new section.
```

**How it handles failures (self-healing):**

```
 Agent clicks a dropdown. Nothing happens.

 Linear agent: "Step failed." → stops or moves on blindly

 Dynamic agent thinks:
 "The dropdown didn't open. Let me check why.
  ├── Console messages? → No JS errors
  ├── Network requests? → GET /drop-downs returned 500
  └── Diagnosis: The API is down, not a UI bug.

  I'll log this as a BACKEND ISSUE with evidence
  and continue testing the other fields."
```

---

## The Agent Decision Loop

This runs continuously — it's not a phase, it's how the agent **thinks**:

```
 ┌──────────────────────────────────────────────┐
 │              AGENT LOOP                       │
 │                                               │
 │  ┌─────────┐                                  │
 │  │ OBSERVE │◄─────────────────────────┐       │
 │  │         │                          │       │
 │  │ Look at │                          │       │
 │  │ current │                          │       │
 │  │ state   │                          │       │
 │  └────┬────┘                          │       │
 │       │                               │       │
 │       ▼                               │       │
 │  ┌─────────┐                          │       │
 │  │  THINK  │                          │       │
 │  │         │                          │       │
 │  │ What    │                          │       │
 │  │ should  │                          │       │
 │  │ I do    │                          │       │
 │  │ next?   │                          │       │
 │  └────┬────┘                          │       │
 │       │                               │       │
 │       ▼                               │       │
 │  ┌─────────┐                          │       │
 │  │   ACT   │                          │       │
 │  │         │                          │       │
 │  │ Do it   │                          │       │
 │  │ via MCP │                          │       │
 │  └────┬────┘                          │       │
 │       │                               │       │
 │       ▼                               │       │
 │  ┌─────────┐                          │       │
 │  │ VERIFY  │── Worked ──────────────►│       │
 │  │         │                                  │
 │  │ Did it  │── Failed ──►┌──────────┐        │
 │  │ work?   │              │ DIAGNOSE │        │
 │  └─────────┘              │          │        │
 │                           │ Why?     │        │
 │                           │ Console? │        │
 │                           │ Network? │        │
 │                           │ DOM?     │        │
 │                           │          │        │
 │                           │ Log it   │────────┘
 │                           │ + adapt  │
 │                           └──────────┘
 └───────────────────────────────────────────────┘

 This loop runs for EVERY action. The agent is always
 observing, thinking, acting, and verifying.
 It never blindly moves to the next step.
```

---

## The Initialization Document

Instead of giving the agent a checklist, you give it **context and purpose**.
This is what makes it dynamic — you describe the WHAT and WHY, the agent
figures out the HOW.

```
 What the initialization document looks like:
 ─────────────────────────────────────────────

 URL:         qa-tq-awp.impactodigifin.xyz
 App:         TECU Credit Union - Loan Application
 Credentials: (if login needed)
 Test Files:  pp.jpg (profile pic), id_doc.pdf (ID upload)

 Purpose:     Fill the entire loan application form with
              valid data and verify every step works.

 Notes:       The form has multiple sections.
              Some dropdowns load from an API.
              The phone field needs country code first.
```

That's it. No XPaths. No step-by-step instructions. No "click this then
click that." The agent reads this, opens the URL, and figures it out.

---

## Real Example: How the Dynamic Agent Would Test Our App

```
 Initialization: "Test qa-tq-awp.impactodigifin.xyz, loan application"
                                  │
                                  ▼
 EXPLORE ─── Agent opens URL, looks at the page
              "I see a form. Tabs at top: Personal, Work, Documents."
              "Personal tab is active. I see: first name, last name,
               email, phone, date of birth, address fields, photo upload."
              "There's a Next button at the bottom."
                                  │
                                  ▼
 REALIZE ─── Agent understands the app
              "This is a multi-step loan form. 3 sections.
               I need to fill all fields in each section
               and click Next to proceed."
                                  │
                                  ▼
 ACT ──────── Agent starts filling Personal Details
              Fills name → verifies ✓
              Fills email → verifies ✓
              Picks country code → verifies ✓
              Fills phone → verifies ✓
              Uploads photo → verifies ✓
              Clicks Next
                                  │
                                  ▼
 EXPLORE ─── New section appeared, agent looks around again
              "Work Details section. I see: employer name,
               employment type dropdown, income field."
                                  │
                                  ▼
 REALIZE ─── "Employment type loads from API. Income is a number field."
                                  │
                                  ▼
 ACT ──────── Fills employer → verifies ✓
              Clicks employment type dropdown → NOTHING HAPPENS
                                  │
                                  ▼
 DIAGNOSE ── "Dropdown didn't open. Let me check..."
              Console: no errors
              Network: GET /drop-downs?type=employment → 500
              "API returned 500. This is a backend bug, not UI."
              Logs the failure with evidence, moves on.
                                  │
                                  ▼
 ACT ──────── Continues with remaining fields...
              Fills income → verifies ✓
              Clicks Next → moves to Documents
                                  │
                                  ▼
 EXPLORE ─── Looks at Documents section...
              ... and so on until done
                                  │
                                  ▼
 REPORT ──── Agent decides it's done, compiles everything
```

Notice how it **moved between explore, realize, and act naturally** — not
in a fixed order. It went back to exploring when it hit a new section.
It diagnosed when something broke. It adapted and moved on.

---

## Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| User Interface | CLI first, Streamlit later | User provides initialization document |
| Agent Brain | Claude API + Claude Agent SDK | Decision-making, understanding, adapting |
| Browser Control | Chrome DevTools MCP | 26 tools for navigation, interaction, inspection |
| Browser | Google Chrome | The web app under test |

**Requirements:**
- Python 3.10+
- Node.js v20.19+ (for Chrome DevTools MCP)
- Google Chrome (stable)
- Claude API key

---

## Development Roadmap

| Phase | What | Outcome |
|-------|------|---------|
| **MVP** | Single page: agent explores + understands + fills + verifies | Proof that the dynamic loop works |
| **Multi-page** | Agent navigates multi-section flows, re-explores each section | Handles real apps end-to-end |
| **Smart Data** | Agent generates contextual test data based on what it understands | Better than random/hardcoded data |
| **Reporting** | Screenshots per step, failure diagnosis, evidence collection | Production-quality output |
| **Personas** | Agent takes on different roles (QA, smoke, security, accessibility) | Different testing perspectives |
| **UI + Scheduling** | Streamlit interface, scheduled runs, result comparison | Ready for team use |

---

## How This Compares to the Current Suite

| Aspect | Current Suite | Dynamic AI Agent |
|--------|--------------|------------------|
| Setup | Human records clicks, builds CSV | Agent gets URL + context, figures it out |
| Understanding | None — follows script blindly | Understands what the app IS and what fields MEAN |
| Test data | LLM/Faker generated from CSV columns | Agent generates data based on field purpose |
| Execution | Linear script replay | Dynamic — adapts, re-explores, self-heals |
| Failure handling | Reports FAIL, human debugs | Diagnoses WHY, checks console/network, gives evidence |
| Maintenance | Update CSV/locators when UI changes | Agent re-explores and adapts automatically |
| Speed | Fast (no LLM calls during execution) | Slower (LLM thinks at every step) |
| Cost | Free per run | LLM API cost per run |
| Reliability | High (same script, same result) | Variable (agent may take different paths) |

**They serve different purposes:**
- Current suite = **daily regression runs** (fast, cheap, deterministic)
- Dynamic AI agent = **intelligent testing** (adaptive, discovers issues humans miss)

---

## MCP Tools the Agent Will Use

| MCP Tool | What the agent uses it for |
|----------|---------------------------|
| `navigate_page` | Go to URL, move between pages |
| `take_snapshot` | Look at the page (the agent's "eyes") |
| `evaluate_script` | Run JavaScript to extract details, count elements |
| `click` | Click anything — buttons, dropdowns, checkboxes, links |
| `fill` | Type into fields |
| `upload_file` | Upload test files |
| `list_network_requests` | Check if API calls worked or failed |
| `list_console_messages` | Read JavaScript errors and warnings |
| `take_screenshot` | Capture visual evidence |
| `wait_for` | Wait for dynamic content to load |
| `handle_dialog` | Handle browser popups and alerts |
| `emulate` | Test different screen sizes, network speeds |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| LLM misunderstands the app | Tests wrong things | Realization stage reviews understanding before acting |
| High token cost per run | Expensive at scale | Cache page understanding, minimize redundant snapshots |
| Non-deterministic paths | Different results each run | Log every decision for reproducibility |
| Chrome DevTools MCP is public preview | Possible breaking changes | Version pinning, graceful error handling |
| Complex dynamic UIs | Elements not found | Agent adapts — waits, scrolls, retries |
| Agent gets stuck in a loop | Infinite exploration | Set max actions limit, timeout per section |

---

## Research Roadmap — 56 Topics Before Development

We research ALL of this before writing a single line of code.

```
 ┌─────────────────────────────────────────────────┐
 │  A. THE BRAIN — Claude + Agent SDK              │
 │  B. THE HANDS — Chrome DevTools MCP             │
 │  C. THE CONNECTION — MCP Protocol + Tool Calls  │
 │  D. THE PERSONALITY — Prompts + Persona         │
 │  E. THE MEMORY — State + Context Management     │
 │  F. THE SKILLS — What the Agent Needs To Do     │
 │  G. THE GUARDRAILS — Limits + Error Handling    │
 │  H. THE OUTPUT — Reporting + Evidence           │
 │  I. THE COST — Tokens + Performance             │
 │  J. THE SETUP — Installation + Deployment       │
 └─────────────────────────────────────────────────┘
```

### A. THE BRAIN — Claude + Agent SDK

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| A1 | Claude Agent SDK | What is it, how does it work, how do we install it | |
| A2 | Agent loop mechanics | How does observe → think → act → verify work in code | |
| A3 | Tool calling from Claude | How does Claude decide which tool to call, how are results returned | |
| A4 | Multi-turn conversation | How does the agent maintain a conversation with itself across many actions | |
| A5 | Model selection | Which Claude model — Opus/Sonnet/Haiku — best for this use case | |

### B. THE HANDS — Chrome DevTools MCP

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| B1 | All 26 MCP tools | Full list with exact signatures, inputs, outputs | |
| B2 | DOM snapshots | What format does it return, accessibility tree vs raw HTML | |
| B3 | JavaScript execution | Can we run custom JS? Limits? Return values? | |
| B4 | Network monitoring | How to capture API calls, request/response bodies, status codes | |
| B5 | Console messages | How to read JS errors, warnings in real-time | |
| B6 | Screenshots | Format, resolution, when to capture, storage | |
| B7 | File uploads | How does MCP handle file input elements | |
| B8 | Dialogs/popups | Alerts, confirmations, permission prompts | |
| B9 | Waiting/timing | How to wait for dynamic content, lazy loading, SPAs | |
| B10 | Browser config | Headless vs headed, viewport, user agent, proxy | |
| B11 | Limitations | What CAN'T it do, known bugs, edge cases | |

### C. THE CONNECTION — MCP Protocol + Tool Calls

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| C1 | MCP Protocol basics | Client-server model, how messages flow, transport layer | |
| C2 | Tool call chaining | How one tool result feeds into the next tool call | |
| C3 | Parallel tool calls | Can we call multiple MCP tools at once | |
| C4 | Error handling in MCP | What happens when a tool fails, timeouts, retries | |
| C5 | MCP server lifecycle | Starting, stopping, reconnecting the Chrome DevTools MCP server | |

### D. THE PERSONALITY — Prompts + Persona

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| D1 | Initialization document design | What goes in, how much context, what format | |
| D2 | System prompt engineering | How to make Claude behave as a QA tester consistently | |
| D3 | Persona switching | How to change behavior (QA tester vs smoke tester vs security tester) | |
| D4 | Instructions vs freedom | How much do we instruct vs let it decide — finding the balance | |
| D5 | Few-shot examples | Should we show the agent example test runs to learn from | |

### E. THE MEMORY — State + Context Management

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| E1 | Context window limits | How many tokens can the agent use, what happens when it fills up | |
| E2 | State tracking | How the agent remembers what it already tested, what sections it visited | |
| E3 | Element map storage | Where to store discovered elements — in context or externally | |
| E4 | Cross-page memory | When navigating between sections, how to not lose track | |
| E5 | Session persistence | Can the agent pause and resume a test later | |
| E6 | Caching strategies | Cache page understanding to avoid re-exploring same page | |

### F. THE SKILLS — What the Agent Needs To Do

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| F1 | Page exploration | How to systematically discover all elements on a page | |
| F2 | Element classification | How to tell text field from email from phone from dropdown | |
| F3 | XPath generation | How to build robust XPaths from DOM snapshots | |
| F4 | Test data generation | How Claude generates contextual data (names, emails, dates) | |
| F5 | Form filling | Text inputs, dropdowns, checkboxes, radio buttons, date pickers | |
| F6 | File uploads | Providing test files (images, PDFs) to upload fields | |
| F7 | Multi-page navigation | Handling Next buttons, tabs, wizards, accordions | |
| F8 | Authentication/login | Logging in before testing if the app requires it | |
| F9 | Dynamic content | Handling elements that appear after API calls, lazy loading, spinners | |
| F10 | Dependent fields | Country → State → City cascading dropdowns | |
| F11 | Validation testing | Testing required fields, format validation, edge cases | |
| F12 | Negative testing | Invalid data, empty fields, boundary values | |

### G. THE GUARDRAILS — Limits + Error Handling

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| G1 | Max actions per run | How to prevent infinite loops | |
| G2 | Timeout per action | What if a page never loads, dropdown never opens | |
| G3 | Agent stuck detection | How to detect when the agent is going in circles | |
| G4 | Browser crash recovery | What if Chrome dies mid-test | |
| G5 | API failure handling | What if Claude API has an outage mid-run | |
| G6 | Graceful shutdown | Saving partial results if something goes wrong | |
| G7 | Human intervention point | When should the agent stop and ask a human | |

### H. THE OUTPUT — Reporting + Evidence

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| H1 | Report format | HTML, Excel, JSON — what works best | |
| H2 | Step-by-step logging | Recording every action, decision, and result | |
| H3 | Screenshot evidence | Screenshot per step or only on failure | |
| H4 | Network log capture | Saving API calls as evidence for failures | |
| H5 | Console log capture | Saving JS errors tied to specific steps | |
| H6 | Failure diagnosis | How the agent explains WHY something failed | |
| H7 | Comparison reports | Comparing run results over time (regression detection) | |

### I. THE COST — Tokens + Performance

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| I1 | Tokens per action | How many tokens does one observe-think-act cycle use | |
| I2 | Total cost per test run | Estimated cost for testing a full multi-page form | |
| I3 | Optimization strategies | Reducing token usage without losing intelligence | |
| I4 | Speed benchmarks | How long does one full test run take | |
| I5 | Model tradeoffs | Opus (smart, slow, expensive) vs Sonnet (balanced) vs Haiku (fast, cheap) | |

### J. THE SETUP — Installation + Deployment

| # | Topic | What we need to know | Status |
|---|-------|---------------------|--------|
| J1 | Chrome DevTools MCP install | npm package, config, how to start the server | |
| J2 | Claude Agent SDK install | pip package, API key setup, basic wiring | |
| J3 | Project structure | Folder layout, how to organize the codebase | |
| J4 | Local development | Running and testing on your machine | |
| J5 | Server deployment | Running on .55 or .146 (headless Chrome, Xvfb) | |
| J6 | Dependencies | Full list of everything needed (Python, Node, Chrome, etc.) | |

---

## Next Steps

1. Research topics A through J — one by one, thorough analysis
2. Update status column as each topic is completed
3. After all research is done, finalize the architecture
4. Then start development
