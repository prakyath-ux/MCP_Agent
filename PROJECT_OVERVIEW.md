# MCP Agent — Project Overview

A high-level guide to what this project is and how to integrate it as a service. Written for someone coming in fresh from another project who needs to consume this as a capability rather than maintain its internals.

---

## What this project is

An AI-powered QA testing system that takes a target app (web or Android), discovers its interactive elements, generates test cases, runs them, and reports results — without hardcoded scripts. The agent **adapts** to whatever app you point it at.

Two surfaces drive it:
- **CLI** — `python -m qa.cli ...` and `python -m qa.orchestrators.<name> ...`
- **Streamlit dashboard** — visual UI for running pipelines, watching live logs, browsing past results

Both surfaces produce the same artifacts in the same locations. The dashboard is just a wrapper around the CLI.

---

## Two sides: Web and Mobile

The project supports two platforms with **separate but parallel** architectures. They share the same conceptual model and artifact layout but use platform-specific tooling.

| Aspect | Web | Mobile |
|---|---|---|
| Browser/runtime | Chrome (via Chrome DevTools MCP, Node.js) | Android device or emulator (via mobile-mcp + ADB) |
| Element discovery | DOM scan via `evaluate_script` | UI tree snapshot via accessibility framework |
| Locators | XPath (primary) + CSS (fallback) | Resource IDs + accessibility IDs + coordinates |
| Targets known to work | Forgenite chatbot edit page; TECU Credit Union loan form (multi-page); DemoQA practice form | Bank App (B2U) — iTeller / Loan / More screens |
| Code lives in | `qa/` (modern), `version_2/` (legacy POC) | `qa/` shares with web; `mobile_version/compound_tools.py` is mobile-specific |

**Why two stacks?** Web and mobile have fundamentally different DOMs and tooling. Forcing one abstraction adds complexity without saving real work. Both stacks plug into the same three-pipeline mental model — see below.

---

## The three-pipeline mental model

Same on both platforms. Each pipeline is independently runnable.

```
                ┌──────────┐     ┌────────┐     ┌─────────┐
   target ───>  │ EXTRACT  │ ──> │  PLAN  │ ──> │ EXECUTE │ ──> report
                └──────────┘     └────────┘     └─────────┘
                     │               │               │
                     ▼               ▼               ▼
                ┌─────────────────────────────────────┐
                │   Knowledge Base (L0 / L1 / L2)     │
                └─────────────────────────────────────┘
```

| Pipeline | Inputs | Outputs | Cost |
|---|---|---|---|
| **Extract** | URL or app package | KB (knowledge base) — every interactive element with locators | Free (web), free (mobile) |
| **Plan** | KB | Test case list (LLM-generated values, deterministic approaches per element type) | ~$0.001 per run |
| **Execute** | KB + test cases (or just KB; auto-plans) | Test report (PASS/FAIL/BLOCKED), console+network evidence | $0.05–0.15 per run |

Each pipeline saves its own artifacts and can be the entry point for downstream consumption.

---

## The web extract pipeline — single-call shortcut

Web has an extra orchestrator that bundles three sub-stages into one Chrome session:

```
EXTRACT_PIPELINE = page_diagnostic ─> exhaustive_extract ─> validate_kb
```

| Sub-stage | What it does |
|---|---|
| **page_diagnostic** | DOM blocker scan. Verdicts: GREEN (fully testable) / YELLOW (testable with workarounds) / RED (out of scope). Catalogs ~15 known patterns: custom dropdowns, iframes, shadow DOM, CAPTCHA, multi-page wizards, etc. |
| **exhaustive_extract** | Full DOM scan + dropdown options expansion (Phase 1) + radio-group conditional discovery (Phase 2). Saves a versioned `KnowledgeBase` JSON. |
| **validate_kb** | Per-locator reachability check on the live page. Verdicts: REACHABLE / HIDDEN / DUPLICATE / BROKEN / NO_LOCATORS / LABEL_ONLY. Catches drift before Execute spends tokens. |

Run it as one CLI call:
```
python -m qa.orchestrators.extract_pipeline <url> --app-name <name> [--wait]
```

Or via the Streamlit dashboard: New Run → `extract_pipeline` (web only).

`--wait` pauses once after Chrome launches — used for apps requiring manual login. Streamlit surfaces a "▶ Resume" button when it sees the prompt.

---

## Knowledge Base architecture

The KB is the central data structure. Three layers, each with a distinct purpose.

| Layer | Content | Who reads it | Token cost |
|---|---|---|---|
| **L0** — Planning Index | element_id, name, type, required, behavior, options, validation rules, depends_on | Plan LLM | ~100 tokens per element |
| **L1** — Execution Details | element_id, locator list (xpath/css/label), confidence, retry strategy, widget type | Compound tools (server-side resolver) | Never sent to LLM |
| **L2** — Evidence / History | runs, change_log, accessibility issues, timestamps | Audit / debugging | Append-only |

KBs live at `artifacts/knowledge/{web|mobile}/<app_name>.json`. Re-extracting with the same `app_name` merges the new screen into the existing KB (replaces matching screen, preserves others).

**Element ID format:**
- 3-part: `{screen}:{label}:{type}` (e.g., `iteller:transaction_type:dropdown`)
- 4-part with section disambiguation: `{screen}:{section}:{label}:{type}` (e.g., `other_products:beneficiary_1_details:first_name:text_input`)

The `element_id` is the primary key. Tools accept it and resolve the L1 locator internally — the LLM never has to invent a CSS selector.

---

## How it works end-to-end (web example)

A typical run against TECU's loan application page 1:

```
1. python -m qa.orchestrators.extract_pipeline \
     'https://qa-tq-awp.impactodigifin.xyz/newapplication' \
     -a tecu_test
   ↓
   • Chrome launches, navigates to URL
   • Diagnostic verdict: YELLOW (custom dropdown + multi-page wizard + placeholder labels)
   • Extract: 10 interactives captured. Phase 1 opens the "Select Branch"
     dropdown, captures 5 branch options (TECU MARABELLA, TECU COUVA, ...)
   • Validate: 10/10 REACHABLE. KB saved to artifacts/knowledge/web/tecu_test.json
   • Synthesis: artifacts/results/2026-05-07/tecu_test_pipeline_<ts>.{txt,json}

2. python -m qa.cli execute -p web -a tecu_test \
     --plan-model gpt-5.4 -m gpt-5.4-mini --max-cases 25 \
     'https://qa-tq-awp.impactodigifin.xyz/newapplication'
   ↓
   • Plan auto-runs (no test cases provided). 22 cases generated in ~9s.
   • Execute LLM calls compound tools, passing element_id from each test case.
   • Tools resolve verified-unique locators from the injected KB, run the
     fill/click/select, observe console + network for errors, return verdicts.
   • Report saved to artifacts/results/<date>/result_tecu_test_<model>_<ts>.txt
```

Mobile flow is the same shape — replace `extract_pipeline` with `qa.cli explore -p mobile`, replace `--app-name` with the package name, and skip the `--wait` flag (mobile doesn't need login workarounds).

---

## Service interfaces

What another project would call to consume this as a service.

### CLI entry points

| Command | Purpose |
|---|---|
| `python -m qa.cli explore <target> -p <web\|mobile> -a <name>` | Pipeline 1 — discover + build KB |
| `python -m qa.cli plan <name> -p <web\|mobile>` | Pipeline 2 — generate test cases |
| `python -m qa.cli execute <target> -p <web\|mobile> -a <name>` | Pipeline 3 — run tests, save report |
| `python -m qa.cli full <target> -p <web\|mobile> -a <name>` | All three in sequence |
| `python -m qa.orchestrators.page_diagnostic <url> -a <name>` | Web-only: pre-flight blocker scan |
| `python -m qa.orchestrators.exhaustive_extract <url> -a <name> [--wait]` | Web-only: deterministic DOM extract with Phase 1+2 |
| `python -m qa.orchestrators.validate_kb <name> [--url <url>] [--wait]` | Web-only: KB reachability check |
| `python -m qa.orchestrators.extract_pipeline <url> -a <name> [--wait]` | Web-only: diagnostic + extract + validate in one Chrome session |

### Stage markers (machine-readable)

`extract_pipeline` emits `STAGE_DONE: <stage>: <json>` lines on stdout. A parent process can parse these for live status updates without reading artifact files:

```
STAGE_DONE: diagnostic: {"verdict": "yellow", "yellow_count": 3, ...}
STAGE_DONE: extract:    {"screen": "newapplication", "element_count": 10, ...}
STAGE_DONE: validate:   {"total": 10, "reachable": 10, "reachable_pct": 100.0, ...}
STAGE_DONE: pipeline:   {"synthesis_path": "artifacts/results/..."}
```

### Programmatic Python API

If invoking from Python rather than via CLI:

```python
from qa.orchestrators.extract_pipeline import run_extract_pipeline
summary = await run_extract_pipeline(url, app_name, wait=False)
# returns {"url", "app_name", "stages": {...}, "synthesis_path"}

from qa.pipelines.execute import run_execute
from qa.models.execute import ExecuteInput
result = await run_execute(ExecuteInput(app=..., knowledge=..., model="gpt-5.4-mini"))
# returns ExecuteOutput with results, bugs, cost, duration
```

---

## Output artifacts

All artifacts are flat-file, no database. Easy to ship between processes.

```
artifacts/
├── knowledge/
│   ├── web/
│   │   └── <app_name>.json        # KB — L0/L1/L2 for an app
│   └── mobile/
│       └── <app_name>.json
├── results/
│   └── <YYYY-MM-DD>/
│       ├── <app>_diagnostic_<ts>.{json,txt}      # Stage 1 output
│       ├── <app>_kb_validation_<ts>.{json,txt}   # Stage 3 output
│       ├── <app>_pipeline_<ts>.{json,txt}        # Synthesis report (web pipeline)
│       └── result_<app>_<model>_<ts>.txt         # Execute test report
└── test_files/                    # Upload payloads (per-app + global pool)
    ├── <app_name>/
    └── global/
```

**Stable contract for a downstream consumer:** the JSON files have stable schemas. The TXT files are human-readable and may change format. Treat JSON as the API.

---

## Constraints & limitations

The diagnostic stage flags these on every run; here's the executive summary for an integrator deciding whether to trust this on their target app.

| Pattern | Status |
|---|---|
| Native HTML form controls | Fully supported |
| Custom dropdowns (popup-on-click) | Supported |
| Native `<input type=radio>` groups | Supported (incl. conditional fields revealed by toggle) |
| File uploads (modal pattern) | Supported (with caveat: PASS is signal-based, not backend-verified) |
| **Cascading dropdowns** (parent → child) | **Not yet** — extract captures empty options[]; manual workaround exists |
| **Card-styled toggles** (button kind, not radio) | **Not yet** — clicked but conditional fields missed |
| **Tabs / accordions / non-upload modals** | **Not yet** — manual extract per state |
| **Iframes (same-origin)** / **Open shadow DOM** | **Not yet** — hard but doable |
| **Closed shadow DOM** | **Hard blocker** — browser API forbids access |
| **CAPTCHA, bot-detection, multi-page wizard auto-advance** | **Out of scope** — by design |
| **Authentication flows** | Manual login via `--wait`; agent does not auto-log-in |

For the full catalog see [research/WEB_AGENT_LIMITATIONS.md](research/WEB_AGENT_LIMITATIONS.md). For the precise frozen state of the codebase, including failure modes per gap, see [FREEZE_2026-05-07.md](FREEZE_2026-05-07.md).

---

## What's deliberately NOT in scope

- **Daily regression scheduling** — there's a separate Playwright suite for that (see `PROJECT_ARCHITECTURE.md`). This project handles intelligent / adaptive testing, not deterministic re-runs of a recorded script.
- **Production app modification** — read-only / black-box testing only.
- **Auto-fixing detected bugs** — the agent reports them; a human or upstream system decides what to do.
- **Cross-page navigation in multi-page wizards** — agent stays on one page; user advances pages manually if needed.
- **Self-learning across apps** — each app's KB is independent. Patterns we encode (e.g., dropdown handling) generalize, but app-specific knowledge does not transfer.

---

## Tech stack snapshot

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| Agent framework | OpenAI Agents SDK |
| Web browser control | Chrome DevTools MCP (Node.js, ~26 tools) |
| Mobile control | mobile-mcp (~21 tools) + ADB |
| Models | GPT-5 family (5.4-mini for execute, 5.4 for plan) — one-tier or two-tier strategies |
| Persistence | Flat JSON in `artifacts/` — no database |
| UI | Streamlit dashboard (`app.py`) |

---

## Recommended integration patterns

If you're consuming this from another project:

**Pattern A — Subprocess + parse stdout markers.** Best for CI / scheduled runs. Spawn `extract_pipeline` or `qa.cli execute`, watch for `STAGE_DONE: <stage>: <json>` lines. Fast feedback, no file polling.

**Pattern B — Subprocess + read artifacts.** Best for batch / async integration. Spawn the orchestrator, wait for exit, read `artifacts/results/<date>/<app>_pipeline_<ts>.json` for the synthesis. Survives crashes (artifacts persist).

**Pattern C — Python import.** Best for tight integration. Import `run_extract_pipeline` / `run_execute` directly. Async-aware. Use when this project is mounted as a library in your venv.

In all cases:
- The KB at `artifacts/knowledge/<platform>/<app_name>.json` is the authoritative state.
- Re-running `extract_pipeline` is idempotent for a given URL — it merges into the existing KB.
- `--wait` flag is the only interactive touch point; everything else is fire-and-forget.

---

## Where to look next

| If you need… | Read |
|---|---|
| Frozen-state inventory + failure modes | [FREEZE_2026-05-07.md](FREEZE_2026-05-07.md) |
| What classes of apps are out of scope | [research/WEB_AGENT_LIMITATIONS.md](research/WEB_AGENT_LIMITATIONS.md) |
| Existing Playwright regression suite (separate project, sister system) | [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md) |
| Original AI-agent design rationale | [AI_Agent.md](AI_Agent.md) |
| Project-level Claude instructions | [CLAUDE.md](CLAUDE.md) |
