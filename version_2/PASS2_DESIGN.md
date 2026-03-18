# Pass 2 — Test Case Execution: Design Document

## Overview

Pass 2 is the second phase of the AI QA agent. After Pass 1 explores the page, fills fields, and dumps knowledge (XPaths, field types, behaviors), Pass 2 uses that knowledge to run regression test cases — verifying validation, error handling, and edge cases.

This document captures the problems discovered during three Pass 2 test runs, the solutions designed, and the feature priority roadmap.

---

## Test Run History

| Run | Date | Approach | Turns | Cost | Tests Done | Tests Working | Outcome |
|-----|------|----------|-------|------|------------|---------------|---------|
| 1 | Mar 17 | No restrictions | 23 | $0.42 | 4/4 | 4/4 | Worked but expensive — wasted turns on unnecessary snapshots |
| 2 | Mar 17 | No snapshots allowed | 6 | $0.09 | 2/9 | 2/9 | Blocked — MCP `fill` requires UIDs from snapshot |
| 3 | Mar 17 | One snapshot per load | 23 | $0.32 | 5/9 | 5/9 | Agent forgot progress, repeated empty-field tests, ran out of turns |

**Key takeaway:** Run 1 produced the best output but at 4x the target cost. Each optimization attempt introduced new problems. A fundamental redesign is needed.

---

## 6 Core Problems

### Problem 1: UID Staleness
- **What:** Chrome DevTools MCP tools (`fill`, `click`) require UIDs from a fresh `take_snapshot`. UIDs from Pass 1 are invalid in Pass 2 — they change on every page load.
- **Impact:** Cannot skip snapshots entirely. Every page load requires at least one snapshot to get valid UIDs.
- **Evidence:** Run 2 — agent said "BLOCKED: Could not fill input (tool requires snapshot)."
- **Cost impact:** Each snapshot adds ~200-300 input tokens + 1 full turn.

### Problem 2: Compaction Wipes Agent Memory
- **What:** With `keep_last_n=0`, the agent lost track of which test cases it already ran. With `keep_last_n=2`, context grows too large.
- **Impact:** Agent repeated empty-field tests 3 times and never progressed to invalid-format tests.
- **Evidence:** Run 3 — agent ran 5 "empty required" tests, never tested invalid email format, ran out at turn 23.
- **Cost impact:** ~$0.10 wasted on repeated identical tests.

### Problem 3: One Test Per Page Load
- **What:** Agent navigates fresh + takes snapshot for every single test case. 7 test cases = 7 navigations + 7 snapshots.
- **Impact:** ~4 turns per test case (navigate + snapshot + fill + evaluate) instead of ~2.
- **Evidence:** Run 3 turn log — `navigate_page` appears 7 times in 23 turns.
- **Cost impact:** ~$0.05 wasted on unnecessary page loads.

### Problem 4: Agent Decides Test Cases (Expensive Reasoning)
- **What:** The agent spends output tokens deciding which tests to run, reasoning about test design, choosing input values.
- **Impact:** Output tokens are 8x more expensive than input ($10/MTok vs $1.25/MTok). Agent reasoning about test planning is the most expensive part of the run.
- **Evidence:** Run 1 — 8,536 output tokens, ~$0.08 just in output.
- **Cost impact:** ~30-40% of total cost is agent reasoning, not test execution.

### Problem 5: No Per-Test-Case Cost Tracking
- **What:** We know total run cost but not cost per individual test case. Can't identify which tests are expensive vs cheap.
- **Impact:** Can't optimize. Can't set per-test budgets. Can't detect anomalies.
- **Evidence:** All runs — only total cost in summary, no breakdown.

### Problem 6: Cache Hit Keeps Dropping
- **What:** Cache hit ratio drops from ~60% to ~33% as the run progresses. Each turn adds new uncached content (snapshots, tool results) that pushes the cached prefix further away.
- **Impact:** Paying full input token price for most of the run.
- **Evidence:** Run 3 — started at 58% cache, ended at 33%. Total savings only 29%.
- **Cost impact:** ~$0.05-0.10 more than optimal.

---

## Solutions

### Solution 1: Use evaluate_script Instead of UIDs (Solves Problem 1 + 6)
Instead of `take_snapshot` → `fill(uid)`, use `evaluate_script` with XPaths from Pass 1:
```javascript
// Set value
document.querySelector('input[aria-label="First Name*"]').value = "";
document.querySelector('input[aria-label="First Name*"]').dispatchEvent(new Event('input', {bubbles: true}));
document.querySelector('input[aria-label="First Name*"]').dispatchEvent(new Event('blur', {bubbles: true}));
```
- **Eliminates:** All snapshots (0 instead of 7)
- **Eliminates:** UID staleness problem entirely
- **Improves:** Cache hit — no large snapshot blobs polluting context
- **Risk:** Some fields may not respond to JS-triggered events (framework-specific event handling). Need to test.

### Solution 2: Orchestrator-Generated Test Plan (Solves Problem 2 + 4)
The orchestrator (Python code, free) generates the exact test plan from the knowledge JSON before the run starts. Agent receives a numbered checklist — no reasoning needed.

```
Test Plan:
1. Navigate to page
2. Set firstName to "" → trigger blur → check for error → record result
3. Set firstName to "123!@#" → trigger blur → check for error → record result
4. Set email to "" → trigger blur → check for error → record result
5. Set email to "notanemail" → trigger blur → check for error → record result
...
```

- **Eliminates:** Agent deciding what to test (expensive output tokens)
- **Eliminates:** Agent forgetting what it already tested (no memory needed — it follows the list)
- **Reduces:** Output tokens per turn (agent just reports "PASS" or "FAIL", no reasoning)
- **Cost:** Zero — Python generates the plan, not the LLM

### Solution 3: Batch Tests Per Page Load (Solves Problem 3)
Group tests that can share a page load:
```
Load 1: Test all empty-required fields (firstName, lastName, email, mobile)
Load 2: Test all invalid-format fields (email format, mobile letters)
Load 3: Test optional fields (middleName empty)
```
3 page loads instead of 7+. Between tests on the same load, clear the field via JS and test the next one.

- **Reduces:** Navigations from 7 to 2-3
- **Reduces:** Snapshots from 7 to 0 (with Solution 1) or 2-3
- **Saves:** ~$0.03-0.05 per run

### Solution 4: Per-Test-Case Cost Tracking (Solves Problem 5)
Orchestrator tracks which turns belong to which test case. At the end of the run, output includes:
```
| Test # | Field | Turns | Input Tokens | Output Tokens | Cost |
|--------|-------|-------|--------------|---------------|------|
| 1 | firstName empty | 2 | 12,400 | 45 | $0.016 |
| 2 | firstName invalid | 1 | 6,200 | 38 | $0.008 |
...
```
This enables anomaly detection, per-test budgets, and optimization targeting.

---

## Feature Priority Roadmap

### Tier 1 — Build Now (Directly Solves Current Problems)

| # | Feature | Solves | Expected Impact | Effort |
|---|---------|--------|-----------------|--------|
| 1 | evaluate_script instead of UIDs | Problem 1, 6 | Eliminates snapshots, better cache | Medium |
| 2 | Orchestrator-generated test plan | Problem 2, 4 | Eliminates reasoning cost + memory loss | Medium |
| 3 | Batch tests per page load | Problem 3 | Fewer navigations | Low |
| 4 | Per-test-case cost tracking | Problem 5 | Visibility into cost breakdown | Low |
| 5 | Per-test-case budget quota ($0.05 max) | Problem 5 | Kill expensive tests early | Low |
| 6 | Runaway detection (cost-per-turn > 2x avg → warn) | All | Safety net | Low |
| 7 | Priority ordering (required fields first, optional last) | Problem 3 | Test important fields even if budget runs out | Low |

### Tier 2 — Build After Pass 2 Works

| # | Feature | Purpose |
|---|---------|---------|
| 8 | Anomaly alerts (test X is 5x more expensive than average) | Cost optimization |
| 9 | Human-in-the-loop scoring (1-5 rating stored in output) | Quality tracking across runs |
| 10 | Cumulative cost dashboard across all runs | Trend analysis |
| 11 | Domain evals (custom metrics for form-testing accuracy) | When 50+ runs available |

### Tier 3 — Build in Production Phase

| # | Feature | Purpose |
|---|---------|---------|
| 12 | Grafana + Prometheus monitoring | Real-time dashboards for scheduled runs |
| 13 | Request queuing | Multiple test runs in sequence |
| 14 | Priority task scheduling | Critical tests first in queue |

### Not Needed

| Feature | Why Not |
|---------|---------|
| BLEU / ROUGE scoring | These measure text generation quality (translations, summaries). Our agent produces structured pass/fail tables — not free text. Wrong tool for the job. |
| Request queuing (now) | Running one test at a time manually. Queuing is for CI/production. |

---

## Expected Cost After Fixes

| Phase | Current Cost | Expected After Fixes | Savings |
|-------|-------------|---------------------|---------|
| Pass 1 (explore + fill + knowledge) | $0.19-0.29 | $0.15-0.20 (unchanged, already optimized) | — |
| Pass 2 (test cases, 7 fields) | $0.32-0.42 | $0.08-0.15 (evaluate_script, no snapshots, pre-built plan) | 60-75% |
| **Total per page** | **$0.51-0.71** | **$0.23-0.35** | **50-65%** |
| **Full 6-page run** | **$3.00-4.25** | **$1.40-2.10** | **50-65%** |

---

## Implementation Checklist

- [ ] **Tier 1.1:** Test evaluate_script approach — can we fill fields + trigger events + check errors via JS only?
- [ ] **Tier 1.2:** Build test plan generator in orchestrator (Python, reads knowledge JSON, outputs numbered test list)
- [ ] **Tier 1.3:** Write Pass 2 v2 prompt (executor mode — follow the plan, no reasoning)
- [ ] **Tier 1.4:** Implement batching logic (group tests by type, minimize page loads)
- [ ] **Tier 1.5:** Add per-test-case cost tracking to turn log
- [ ] **Tier 1.6:** Add per-test budget quota + runaway detection
- [ ] **Tier 1.7:** Add priority ordering (required fields first)
- [ ] **Tier 1.8:** Run Pass 2 v2 on TECU — validate cost + coverage
- [ ] **Tier 2.1:** Anomaly detection
- [ ] **Tier 2.2:** Human-in-the-loop scoring
- [ ] **Tier 2.3:** Cumulative cost dashboard
- [ ] **Tier 3.1:** Grafana + Prometheus (when in production)

---

## Architecture Decision: Why Not BLEU/ROUGE?

BLEU and ROUGE are **text similarity metrics** designed for:
- Machine translation (compare translated text to reference translation)
- Text summarization (compare generated summary to reference summary)

Our agent produces **structured data**:
```
| Field | Test Case | Status |
| firstName | empty | PASS |
| email | invalid | FAIL |
```

This is a **classification task** (pass/fail), not text generation. The right metrics are:
- **Accuracy:** % of test cases with correct pass/fail judgment
- **Coverage:** % of fields tested out of total fields
- **Cost efficiency:** $ per correctly identified bug
- **False positive rate:** % of "bugs" that are actually correct behavior

These domain-specific evals (Tier 2, item 11) are more valuable than generic text metrics.
