# qa/engine/guardrails.py — Bounded scopes for LLM-driven operations.
#
# Every LLM sub-task (extract, classify, verify) runs inside a
# GuardrailContext that tracks calls / time / cost. When a hard cap is hit,
# GuardrailExit is raised — orchestrators catch it, save partial state,
# and move to the next item instead of burning budget on a loop or spiral.
#
# Usage pattern:
#
#     page = per_page_scope()
#     for trigger in triggers:
#         element = page.child("element", hard_max_calls=3, hard_max_cost=0.015)
#         try:
#             result = await llm_classify(..., guardrails=element)
#         except GuardrailExit as e:
#             print(f"skipping {trigger}: {e}")
#             continue
#
# Caps are SOFT (warn + continue) and HARD (raise GuardrailExit).
# Nested scopes propagate call + cost counts to parents, so a per-run
# scope sees the sum of everything its children spent.

from __future__ import annotations

import time
from dataclasses import dataclass, field


class GuardrailExit(Exception):
    """Raised when a GuardrailContext hits any of its hard caps.

    Callers are expected to catch this, save whatever partial state they
    have, and move on to the next unit of work (next test case, next
    element, etc). Never let this propagate past an orchestrator boundary —
    that would abort the whole run.
    """
    def __init__(self, scope: str, reason: str, details: dict) -> None:
        self.scope = scope
        self.reason = reason
        self.details = details
        super().__init__(f"[{scope}] {reason} — {details}")


# Effectively-infinite defaults — presets override selectively.
_INF_INT = 10**9
_INF_FLOAT = 10**9.0


@dataclass
class GuardrailContext:
    """Bounded scope for a single unit of LLM work.

    Attach via `guardrails=` to llm_classify. Before each LLM call the
    context's `check()` runs and raises GuardrailExit if we've hit any
    hard cap. After the call, `record()` increments counters (and
    propagates up to any parent scope).
    """
    scope: str = "unnamed"

    # Soft caps — warn once, keep going
    soft_max_calls: int = _INF_INT
    soft_max_cost: float = _INF_FLOAT
    soft_max_sec: float = _INF_FLOAT

    # Hard caps — raise GuardrailExit on hit
    hard_max_calls: int = _INF_INT
    hard_max_cost: float = _INF_FLOAT
    hard_max_sec: float = _INF_FLOAT

    # Runtime state
    calls: int = 0
    cost: float = 0.0
    start_time: float = field(default_factory=time.monotonic)
    parent: "GuardrailContext | None" = None

    # Warning dedupe flags
    _warned_calls: bool = field(default=False, repr=False)
    _warned_cost: bool = field(default=False, repr=False)
    _warned_sec: bool = field(default=False, repr=False)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def check(self) -> None:
        """Call before each LLM request. Raises GuardrailExit on hard cap.
        Prints a single soft-cap warning per breach type."""
        # Hard caps (highest priority)
        if self.calls >= self.hard_max_calls:
            self._exit(
                "max_calls_exceeded",
                {"limit": self.hard_max_calls, "observed": self.calls},
            )
        if self.cost >= self.hard_max_cost:
            self._exit(
                "max_cost_exceeded",
                {"limit": self.hard_max_cost, "observed": round(self.cost, 4)},
            )
        elapsed = self.elapsed
        if elapsed >= self.hard_max_sec:
            self._exit(
                "max_time_exceeded",
                {"limit": self.hard_max_sec, "observed": round(elapsed, 1)},
            )

        # Soft caps — print once per breach type
        if not self._warned_calls and self.calls >= self.soft_max_calls:
            print(
                f"  [guardrail] {self.scope}: ⚠ soft cap — "
                f"{self.calls} calls (cap {self.soft_max_calls})"
            )
            self._warned_calls = True
        if not self._warned_cost and self.cost >= self.soft_max_cost:
            print(
                f"  [guardrail] {self.scope}: ⚠ soft cap — "
                f"${self.cost:.4f} (cap ${self.soft_max_cost:.4f})"
            )
            self._warned_cost = True
        if not self._warned_sec and elapsed >= self.soft_max_sec:
            print(
                f"  [guardrail] {self.scope}: ⚠ soft cap — "
                f"{elapsed:.1f}s (cap {self.soft_max_sec:.0f}s)"
            )
            self._warned_sec = True

    def record(self, cost: float = 0.0) -> None:
        """Call after each LLM request completes. Increments counters
        and propagates to the parent scope so per-run totals stay accurate."""
        self.calls += 1
        self.cost += max(cost, 0.0)
        if self.parent is not None:
            self.parent.record(cost=cost)

    def child(
        self,
        scope: str,
        *,
        soft_max_calls: int = _INF_INT,
        soft_max_cost: float = _INF_FLOAT,
        soft_max_sec: float = _INF_FLOAT,
        hard_max_calls: int = _INF_INT,
        hard_max_cost: float = _INF_FLOAT,
        hard_max_sec: float = _INF_FLOAT,
    ) -> "GuardrailContext":
        """Spawn a child scope. Child runs its own caps independently,
        but every call + cost it records also propagates up to self."""
        return GuardrailContext(
            scope=scope,
            parent=self,
            soft_max_calls=soft_max_calls,
            soft_max_cost=soft_max_cost,
            soft_max_sec=soft_max_sec,
            hard_max_calls=hard_max_calls,
            hard_max_cost=hard_max_cost,
            hard_max_sec=hard_max_sec,
        )

    def summary(self) -> str:
        return (
            f"{self.scope}: {self.calls} calls, "
            f"${self.cost:.4f}, {self.elapsed:.1f}s"
        )

    def _exit(self, reason: str, details: dict) -> None:
        print(f"  [guardrail] {self.scope}: ✗ HARD CAP HIT — {reason} {details}")
        raise GuardrailExit(self.scope, reason, details)


# ── Preset factories ────────────────────────────────────────────────
# Caps sourced from plan.md § Operational Guardrails. These match measured
# baselines: ~$0.07 per page extract today → soft 0.15 / hard 0.30 gives
# 2-4× headroom for verification overhead without inviting runaway.


def per_element_scope(parent: GuardrailContext | None = None) -> GuardrailContext:
    """Single element extraction: dropdown options, post-OCR fields, one test case field."""
    if parent is not None:
        return parent.child(
            "element",
            soft_max_calls=2, soft_max_cost=0.008, soft_max_sec=10,
            hard_max_calls=3, hard_max_cost=0.015, hard_max_sec=20,
        )
    return GuardrailContext(
        scope="element",
        soft_max_calls=2, soft_max_cost=0.008, soft_max_sec=10,
        hard_max_calls=3, hard_max_cost=0.015, hard_max_sec=20,
    )


def per_dropdown_scope(parent: GuardrailContext | None = None) -> GuardrailContext:
    """One dropdown's open + extract + optional re-verify cycle."""
    if parent is not None:
        return parent.child(
            "dropdown",
            soft_max_calls=1, soft_max_cost=0.010,
            hard_max_calls=2, hard_max_cost=0.020,
        )
    return GuardrailContext(
        scope="dropdown",
        soft_max_calls=1, soft_max_cost=0.010,
        hard_max_calls=2, hard_max_cost=0.020,
    )


def per_page_scope(parent: GuardrailContext | None = None) -> GuardrailContext:
    """Whole-page extraction — ~$0.07 baseline, 2-4× headroom for verify."""
    if parent is not None:
        return parent.child(
            "page",
            soft_max_calls=40, soft_max_cost=0.15, soft_max_sec=300,
            hard_max_calls=80, hard_max_cost=0.30, hard_max_sec=720,
        )
    return GuardrailContext(
        scope="page",
        soft_max_calls=40, soft_max_cost=0.15, soft_max_sec=300,
        hard_max_calls=80, hard_max_cost=0.30, hard_max_sec=720,
    )


def per_test_scope(parent: GuardrailContext | None = None) -> GuardrailContext:
    """Single test case execution on one field."""
    if parent is not None:
        return parent.child(
            "test",
            soft_max_calls=2, soft_max_cost=0.03, soft_max_sec=30,
            hard_max_calls=3, hard_max_cost=0.05, hard_max_sec=90,
        )
    return GuardrailContext(
        scope="test",
        soft_max_calls=2, soft_max_cost=0.03, soft_max_sec=30,
        hard_max_calls=3, hard_max_cost=0.05, hard_max_sec=90,
    )


def per_run_scope(parent: GuardrailContext | None = None) -> GuardrailContext:
    """Whole pipeline invocation (explore or execute)."""
    if parent is not None:
        return parent.child(
            "run",
            soft_max_calls=100, soft_max_cost=0.75, soft_max_sec=900,
            hard_max_calls=200, hard_max_cost=1.50, hard_max_sec=1800,
        )
    return GuardrailContext(
        scope="run",
        soft_max_calls=100, soft_max_cost=0.75, soft_max_sec=900,
        hard_max_calls=200, hard_max_cost=1.50, hard_max_sec=1800,
    )


def per_verify_scope(parent: GuardrailContext | None = None) -> GuardrailContext:
    """CoVe verification wrapper — one verify + optional revise."""
    if parent is not None:
        return parent.child(
            "verify",
            soft_max_calls=1, soft_max_cost=0.003,
            hard_max_calls=2, hard_max_cost=0.008,
        )
    return GuardrailContext(
        scope="verify",
        soft_max_calls=1, soft_max_cost=0.003,
        hard_max_calls=2, hard_max_cost=0.008,
    )
