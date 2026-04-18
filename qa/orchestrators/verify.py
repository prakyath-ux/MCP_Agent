# qa/orchestrators/verify.py — Chain-of-Verification (CoVe) wrapper.
#
# After an LLM sub-task produces a claim (dropdown options, field list,
# observed error text), this module verifies each claim-field against a
# source snapshot BEFORE we commit it to the KB. Unsupported facts get
# dropped. This kills hallucinations at the point they'd enter permanent
# storage.
#
# Two tiers, designed to be cheap-first:
#
# Tier 1 — DETERMINISTIC (free, always run)
#   Pure Python string / substring presence checks. Catches ~80% of
#   hallucinations (LLM invented an option that's not in the snapshot).
#   No LLM calls. Zero cost. Fast.
#
# Tier 2 — LLM-BASED (cheap, opt-in)
#   Used when deterministic is too strict — e.g. the claim rewords text
#   ("Communication Method" ↔ "Preferred Method of communication"), or
#   needs semantic judgement. One narrow LLM call per batch.
#
# Design goals:
#   • Never silently accept unsupported facts. Always record what was dropped.
#   • Bounded — guardrails prevent spiral on Tier 2.
#   • Graceful — on verify failure (malformed input, empty snapshot), return
#     the original claim with confidence=0.5 rather than hard-failing.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from qa.engine.guardrails import GuardrailContext


@dataclass
class VerifyResult:
    """Result of a verification pass. Contains the cleaned claim plus
    metadata about what was dropped and a confidence score."""
    claim: dict[str, Any] = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)
    kept: int = 0
    checked: int = 0
    method: str = "deterministic"   # or "llm" or "mixed"

    @property
    def confidence(self) -> float:
        """Ratio of kept-to-checked items. 1.0 = everything verified,
        0.0 = everything dropped. Callers can use this for KB tagging."""
        if self.checked == 0:
            return 1.0  # nothing to verify = nothing to doubt
        return self.kept / self.checked


# ──────────────────────────────────────────────────────────────────────
# Deterministic verification (Tier 1)
# ──────────────────────────────────────────────────────────────────────

def _tokens_in(snap: str) -> set[str]:
    """Lowercase alphanumeric tokens from a snapshot. Used for fuzzy
    presence checks when full substring match would be too strict."""
    return {t for t in re.findall(r"[a-z0-9]+", snap.lower()) if len(t) > 1}


def _claim_supported(value: str, snapshot_lower: str, snap_tokens: set[str]) -> bool:
    """Is a claimed string supported by the snapshot?
    Supports:
      • exact substring match (case-insensitive) — strictest
      • all-tokens-present fallback — handles minor reword/whitespace
    """
    if not value or not snapshot_lower:
        return False
    v = value.strip().lower()
    if not v:
        return False
    # Exact substring first
    if v in snapshot_lower:
        return True
    # Token-presence fallback: every alphanumeric token of the value must
    # appear somewhere in the snapshot's token set.
    value_tokens = {t for t in re.findall(r"[a-z0-9]+", v) if len(t) > 1}
    if not value_tokens:
        return False
    return value_tokens.issubset(snap_tokens)


def verify_list_field(
    claim: dict[str, Any],
    field_name: str,
    snapshot: str,
    *,
    label: str = "verify",
    log: bool = True,
) -> VerifyResult:
    """Verify a list-valued claim field against a snapshot.

    Drops entries that aren't literally present (exact or token-subset).
    Use for: dropdown options, extracted field names, observed button labels.

    Returns VerifyResult with cleaned claim and dropped-item metadata.
    The input `claim` is NOT mutated.
    """
    cleaned = dict(claim)
    result = VerifyResult(method="deterministic")

    items = claim.get(field_name)
    if items is None:
        # Field not in claim — nothing to verify, nothing to add.
        result.claim = cleaned
        return result
    if not isinstance(items, list):
        # Non-list field — skip; caller should use verify_string_fields.
        result.claim = cleaned
        return result

    snap_lower = (snapshot or "").lower()
    snap_tokens = _tokens_in(snapshot or "")

    verified: list = []
    for item in items:
        result.checked += 1
        if not isinstance(item, str):
            # Non-string items can't be source-verified; keep them with a
            # flag rather than drop — caller decides.
            verified.append(item)
            result.kept += 1
            continue
        if _claim_supported(item, snap_lower, snap_tokens):
            verified.append(item)
            result.kept += 1
        else:
            result.dropped.append({"field": field_name, "value": item})

    cleaned[field_name] = verified
    result.claim = cleaned

    if log and result.dropped:
        print(
            f"    [verify {label}] {field_name}: kept {result.kept}/{result.checked}, "
            f"dropped {len(result.dropped)} unsupported → {[d['value'] for d in result.dropped]}"
        )
    return result


def verify_string_fields(
    claim: dict[str, Any],
    field_names: list[str],
    snapshot: str,
    *,
    label: str = "verify",
    log: bool = True,
    on_unsupported: str = "flag",   # "flag" | "drop"
) -> VerifyResult:
    """Verify string-valued claim fields against a snapshot.

    Behavior on unsupported value:
      • "flag" (default): keep value, append to dropped list for caller inspection.
      • "drop": clear value to empty string, append to dropped list.

    Use for: field labels, observed error text, status strings.
    """
    cleaned = dict(claim)
    result = VerifyResult(method="deterministic")

    snap_lower = (snapshot or "").lower()
    snap_tokens = _tokens_in(snapshot or "")

    for name in field_names:
        val = claim.get(name)
        if not val or not isinstance(val, str):
            continue
        result.checked += 1
        if _claim_supported(val, snap_lower, snap_tokens):
            result.kept += 1
        else:
            result.dropped.append({"field": name, "value": val})
            if on_unsupported == "drop":
                cleaned[name] = ""

    result.claim = cleaned

    if log and result.dropped:
        print(
            f"    [verify {label}] unsupported strings: "
            f"{[(d['field'], d['value']) for d in result.dropped]} "
            f"(action={on_unsupported})"
        )
    return result


# ──────────────────────────────────────────────────────────────────────
# LLM-based verification (Tier 2 — opt in)
# ──────────────────────────────────────────────────────────────────────


async def verify_list_via_llm(
    claim: dict[str, Any],
    field_name: str,
    snapshot: str,
    *,
    label: str = "verify_llm",
    guardrails: GuardrailContext | None = None,
    model: str = "gpt-5.1",
) -> VerifyResult:
    """LLM-based verification for list-valued claims. Use when deterministic
    verification (verify_list_field) is too strict — e.g. the claim uses
    semantically equivalent but differently-worded text.

    Passes the list + snapshot to the LLM and asks which items are actually
    supported. Guardrails enforce bounded cost.
    """
    # Local imports to keep verify.py importable without OpenAI at module scope.
    from qa.orchestrators.llm_subtask import llm_classify
    from qa.orchestrators.sub_prompts import (
        VERIFY_CLAIM_PROMPT,
        VERIFY_CLAIM_SCHEMA,
    )

    cleaned = dict(claim)
    result = VerifyResult(method="llm")

    items = claim.get(field_name, [])
    if not isinstance(items, list) or not items:
        result.claim = cleaned
        return result

    # Build a compact user message: list of items + snapshot
    items_block = "\n".join(f"  - {item}" for item in items if isinstance(item, str))
    user_content = (
        f"SNAPSHOT:\n{(snapshot or '')[:10000]}\n\n"
        f"CLAIMED ITEMS for field '{field_name}':\n{items_block}\n"
    )

    try:
        response = await llm_classify(
            VERIFY_CLAIM_PROMPT,
            user_content,
            VERIFY_CLAIM_SCHEMA,
            model=model,
            label=label,
            guardrails=guardrails,
        )
    except Exception as e:
        # On any failure (guardrail hit, parse fail, network), keep the
        # original claim with confidence 0.5 rather than hard-failing.
        print(f"    [verify {label}] LLM verify failed: {e} — keeping original claim")
        result.claim = cleaned
        result.checked = len(items)
        result.kept = len(items)
        result.method = "llm_failed_kept_all"
        return result

    verified = response.get("verified_items", []) or []
    unsupported = response.get("unsupported_items", []) or []
    verified_set = {v.strip().lower() for v in verified if isinstance(v, str)}

    final_list: list = []
    for item in items:
        result.checked += 1
        if isinstance(item, str) and item.strip().lower() in verified_set:
            final_list.append(item)
            result.kept += 1
        else:
            result.dropped.append({"field": field_name, "value": item})

    cleaned[field_name] = final_list
    result.claim = cleaned

    if result.dropped:
        print(
            f"    [verify {label}] LLM dropped {len(result.dropped)} "
            f"unsupported: {[d['value'] for d in result.dropped]} "
            f"(reasoning: {response.get('reasoning', '')[:80]})"
        )
    return result


# ──────────────────────────────────────────────────────────────────────
# Combined strategy — recommended entry point
# ──────────────────────────────────────────────────────────────────────

async def verify_list_cascaded(
    claim: dict[str, Any],
    field_name: str,
    snapshot: str,
    *,
    label: str = "verify",
    guardrails: GuardrailContext | None = None,
    llm_escalate_threshold: float = 0.5,
    log: bool = True,
) -> VerifyResult:
    """Cheap-first cascaded verification:

      1. Run deterministic Tier 1 (free).
      2. If confidence < threshold, escalate to LLM (Tier 2) to re-examine
         the dropped items — deterministic may have been too strict.

    Most of the time Tier 1 is enough and we never spend on Tier 2.
    """
    tier1 = verify_list_field(claim, field_name, snapshot, label=label, log=log)

    # If most items passed deterministic, trust it and return.
    if tier1.confidence >= llm_escalate_threshold or not tier1.dropped:
        return tier1

    # Otherwise try Tier 2 on the dropped items to rescue semantic matches.
    # Build a sub-claim with only the dropped items.
    rescue_claim = {field_name: [d["value"] for d in tier1.dropped]}
    tier2 = await verify_list_via_llm(
        rescue_claim, field_name, snapshot,
        label=f"{label}_rescue", guardrails=guardrails,
    )

    # Merge: items that survived either tier are kept.
    kept_deterministic = tier1.claim.get(field_name, [])
    kept_rescued = tier2.claim.get(field_name, [])
    final_list = list(kept_deterministic) + list(kept_rescued)

    merged = dict(claim)
    merged[field_name] = final_list

    return VerifyResult(
        claim=merged,
        dropped=tier2.dropped,   # only tier2-drops survive as "dropped"
        kept=len(final_list),
        checked=tier1.checked,
        method="mixed",
    )
