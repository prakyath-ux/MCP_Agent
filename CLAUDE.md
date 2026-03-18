# AI-Powered Regression Testing Agent

## Project Identity
Building a dynamic AI agent that replaces CSV-driven Playwright regression tests with an intelligent, self-healing QA tester. Given a URL and context, the agent explores, understands, and tests web applications autonomously — no hardcoded steps, no manual recording.

## Current Phase
**Research & Planning** — Do NOT write application code unless explicitly instructed. Focus on research, architecture decisions, and planning.

## Tech Stack
- **Language:** Python 3.10+
- **Agent Framework:** Claude Agent SDK (`claude-agent-sdk`, Alpha v0.1.39) — pending Claude vs Gemini decision
- **Browser Control:** Chrome DevTools MCP (Google) — 26 tools for navigation, interaction, inspection
- **Browser:** Google Chrome (stable)
- **Runtime:** Node.js v20.19+ (for Chrome DevTools MCP server)
- **Alternative under evaluation:** Google ADK + Gemini (larger context, cheaper, natural Chrome pairing)

## Architecture
Dynamic agent with 3 mental stages (not a fixed pipeline):
1. **Explore** — Open URL, look around, discover elements and structure
2. **Realize** — Form a mental model of what the app is and what fields mean
3. **Act** — Fill, click, navigate, verify — adapting in real-time

Core loop runs continuously: **Observe → Think → Act → Verify → (Diagnose if failed)**

## Key Decisions Made
| Decision | Choice | Rationale |
|----------|--------|-----------|
| MCP Server | Chrome DevTools MCP (Google) | Deep network inspection, console stack traces, performance tracing — diagnostic power |
| Memory Architecture | Three-tier (Hot/Warm/Cold) | Hot = context window, Warm = external JSON state tracker, Cold = disk files |
| Primary Model | Sonnet 4.6 ($3/$15 per MTok) | Beats Opus on Finance Agent benchmark (63.3% vs 60.1%), 60% cost |
| Verification Model | Haiku 4.5 ($1/$5 per MTok) | Simple pass/fail checks, 5x cheaper |
| Diagnosis Model | Opus 4.6 ($5/$25 per MTok) | Strongest reasoning for complex failure analysis |
| Safety Limits | max_turns=600, max_budget_usd=15 | Double safety net |

## Pending Decision
**Claude vs Gemini as LLM** — Gemini offers 1M context (fits all 153 steps), cheaper tokens ($1.25/$5 Flash 2.0), native Chrome DevTools pairing. Claude offers more proven agentic capabilities (Claude Code lineage). This decision affects SDK choice (Claude Agent SDK vs Google ADK).

## Key Numbers
| Metric | Value |
|--------|-------|
| E2E test steps (test_1.csv) | 153 across 6 pages |
| MCP calls per full run | ~350-500 |
| Cost per run | $5-12 (mixed models) |
| Time per run | ~25-125 min |
| Context limit | ~120 actions before 200K ceiling |
| Current system speed | 30-60 sec, $0 (no LLM) |

## Research Status
- **Done (26/69):** A1-A5 (SDK, loop, tools, memory, models), B1-B6 (MCP tools, DOM, JS, network, console, screenshots), MCP alternatives, memory architecture, guardrails, cost estimates, Gemini comparison
- **Remaining (27):** B7-B11 (file uploads, dialogs, waiting, browser config, limitations), C1-C5 (MCP protocol), D1-D5 (prompting/persona), F1-F12 (agent skills)
- **Deferred (14):** H (reporting), J (deployment)

## Key Project Files
| File | Purpose |
|------|---------|
| `PROJECT_ARCHITECTURE.md` | Existing Playwright framework — full stack documentation |
| `AI_Agent.md` | Agent plan — dynamic approach, architecture, research roadmap |
| `plan.md` | Research findings — verified technical details on all answered topics |

## Target Application
- **URL:** `https://qa-tq-awp.impactodigifin.xyz/newapplication`
- **App:** TECU Credit Union — Loan Application Form
- **Structure:** 6-page wizard (Contact Info → Documents → Additional Details → Other Products → PEP/FATCA → PDF/Other Details)
- **153 steps:** ~104 clicks, ~47 inputs, 2 keyboard inputs

## Infrastructure
| Server | Role | Path |
|--------|------|------|
| 172.16.0.146 (main) | Production — CLIENT TESTING, never touch | `/home/dev/project/reg_test_v2/regression-playwright/` |
| 172.16.0.146 (dev) | Dev/testing features | `/home/dev/project/testing_features_QA/regression-playwright/` |
| 172.16.0.55 | Local development | Same stack, default ports |

## Constraints
- Never modify the production server (.146 main app on port 8501)
- Chrome DevTools MCP is public preview — version pin, handle breaking changes gracefully
- The existing Playwright suite continues for daily regression (fast, free, deterministic)
- The AI agent is for intelligent testing (adaptive, discovers issues humans miss)
- Both systems coexist — different purposes, not a full replacement

## Custom Skills
- `/research [topic-id]` — Continue researching unanswered roadmap topics
- `/status` — Project progress dashboard
- `/architecture [area]` — Review architectural decisions and trade-offs
- `/implement [component]` — Build a component following project patterns (future use)

## Custom Agents
- `researcher` — Deep technical research (read-only, web search enabled)
- `code-reviewer` — Code review for agentic patterns (read-only)
- `prompt-engineer` — System prompt and persona design (Opus model)
