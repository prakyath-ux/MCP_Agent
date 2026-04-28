# qa/orchestrators/wizard_steps.py — primitives for the autonomous wizard
# navigator. The wizard loop in form_extract.py composes these to advance
# multi-page forms without manual intervention:
#
#   1. fill_page_from_defaults — set every L0 field to its declared default
#   2. click_save_and_continue — find a "Save & Continue" / "Next" button
#   3. wait_for_page_transition — confirm the click moved us off the page
#
# Helpers are intentionally agnostic of the calling orchestrator. They reuse
# execute_flow primitives (cascade-aware fill via _set_parent_value, locator
# fallback) so we don't reinvent the per-element fill logic.

import asyncio
import json

from qa.adapters.protocol import PlatformAdapter
from qa.config import Defaults
from qa.models import KnowledgeBase
from qa.models.knowledge import ScreenKnowledge
from qa.orchestrators.execute_flow import _set_parent_value
from qa.tools.web_tools import _find_uid_by_text, _safe_parse


# Common labels for "advance the wizard" buttons. Order matters — most
# specific first so we don't click a generic "Submit" when a "Save &
# Continue" is also visible. All matching is case-insensitive substring.
#
# OTP-family labels lead the list because OTP screens often render both
# a "Verify OTP" button AND keep the prior "Save & Continue" visible —
# we want to click Verify first so the OTP submits before any save flow.
DEFAULT_NAV_LABELS = (
    "Verify OTP",
    "Verify Code",
    "Verify",
    "Confirm OTP",
    "Submit OTP",
    "Save & Continue",
    "Save and Continue",
    "Save & Next",
    "Save and Next",
    "Save & Proceed",
    "Continue",
    "Next",
    "Proceed",
    "Submit",
)


# Regex matching a single digit-input slot in a multi-box OTP widget.
# Captures the trailing index so we can sort and fill in order. Catches
# common shapes:
#   • TECU: Otp-Input-0 .. Otp-Input-5
#   • Some libs: otp_digit_1, OtpDigit1, otp-1
#   • Pin code variants: pin_1, code-digit-2
import re
_OTP_SLOT_RE = re.compile(
    r"^\s*(?:otp|pin|verif(?:y|ication))[-_\s]*(?:input|digit|code|box)?[-_\s]*(\d+)\s*$",
    re.IGNORECASE,
)


async def fill_page_from_defaults(
    adapter: PlatformAdapter,
    kb: KnowledgeBase,
    defaults: Defaults,
    screen: ScreenKnowledge,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Walk every L0 element on `screen` and fill from `defaults` if a value
    is declared. Reuses execute_flow._set_parent_value for the per-element
    cascade-aware dispatch (text vs dropdown + locator fall-through).

    File uploads go through _upload_file_for_field_impl directly — the
    web_tools module needs its server/kb pinned first, which we do here.

    Returns (filled, skipped) where each item is (field_name, note).
    Order: dropdowns first (cascade unlocks), then text inputs, then
    file uploads, then everything else."""
    filled: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    # Pin web_tools to this adapter+kb so file uploads work without going
    # through an LLM tool call. Idempotent — safe to re-run per page.
    from qa.tools.web_tools import set_kb, set_server
    set_server(adapter.get_mcp_server())
    set_kb(kb, kb.app.app_name if kb.app else "")

    # ── OTP / multi-box pattern detection ────────────────────────────
    # Detect any cluster of single-digit OTP boxes (Otp-Input-0 ..
    # Otp-Input-5 etc.). When present, look up the OTP code in defaults
    # under common label aliases and fill each box with one digit in
    # index order. Mark the cluster as filled so the per-element loop
    # skips them. Per-box fills go through _set_parent_value as normal.
    otp_slots: list[tuple[int, object]] = []
    for el in screen.l0:
        tname = el.type.value if hasattr(el.type, "value") else str(el.type)
        if tname != "text_input":
            continue
        m = _OTP_SLOT_RE.match(el.name or "")
        if m:
            otp_slots.append((int(m.group(1)), el))

    otp_consumed: set[str] = set()
    if otp_slots:
        otp_slots.sort(key=lambda t: t[0])
        otp_value = ""
        for alias in (
            "OTP", "Enter OTP", "OTP Code", "Verification Code",
            "Enter Verification Code", "Code", "Enter Code",
            "One-Time Password",
        ):
            v = defaults.get(alias)
            if v:
                otp_value = v
                break

        if not otp_value:
            for _idx, el in otp_slots:
                skipped.append((el.name, "no OTP default declared"))
                otp_consumed.add(el.name)
        elif len(otp_value) < len(otp_slots):
            for _idx, el in otp_slots:
                skipped.append((el.name, f"OTP default {otp_value!r} shorter than {len(otp_slots)} boxes"))
                otp_consumed.add(el.name)
        else:
            print(f"  [wizard] detected OTP pattern: {len(otp_slots)} boxes — filling with {otp_value[:len(otp_slots)]!r}")
            for digit_idx, (_idx, el) in enumerate(otp_slots):
                digit = otp_value[digit_idx]
                # Patch defaults to return this digit for THIS slot only.
                # Same trick execute_flow uses for one-off value injection.
                orig_get = defaults.get
                def _patched(label, section="", _expected=el.name, _val=digit):
                    if label == _expected:
                        return _val
                    return orig_get(label, section)
                defaults.get = _patched  # type: ignore[method-assign]
                try:
                    ok, note = await _set_parent_value(
                        adapter, kb, defaults, el.element_id,
                    )
                finally:
                    defaults.get = orig_get  # type: ignore[method-assign]
                (filled if ok else skipped).append((el.name, note if ok else f"otp digit fill failed: {note}"))
                otp_consumed.add(el.name)
                await asyncio.sleep(0.15)

    by_type: dict[str, list] = {
        "dropdown": [],
        "text_input": [],
        "file_upload": [],
        "other": [],
    }
    for el in screen.l0:
        if el.name in otp_consumed:
            continue
        tname = el.type.value if hasattr(el.type, "value") else str(el.type)
        if tname in by_type:
            by_type[tname].append(el)
        else:
            by_type["other"].append(el)

    ordered = (
        by_type["dropdown"] + by_type["text_input"]
        + by_type["file_upload"] + by_type["other"]
    )

    # Autofill-marker patterns. If an L0's `behavior` text contains any
    # of these, the field is populated by the app itself (OCR auto-fill,
    # session-derived value, masked input). The wizard MUST NOT overwrite
    # — doing so corrupts state shared with the next page (e.g. an
    # OCR'd passport number that downstream KYC checks rely on). Same
    # rule the plan-side prompt enforces for VERIFY_ONLY tests in
    # qa/prompts/plan.py.
    AUTOFILL_MARKERS = (
        "auto_filled", "auto-filled", "autofilled",
        "read_only", "read-only", "readonly",
        "masked",
    )

    def _is_autofilled(el) -> bool:
        b = (getattr(el, "behavior", "") or "").lower()
        return any(m in b for m in AUTOFILL_MARKERS)

    for el in ordered:
        tname = el.type.value if hasattr(el.type, "value") else str(el.type)
        if tname == "date_picker":
            skipped.append((el.name, "date_picker — wizard skips"))
            continue

        # Don't touch auto-filled / read-only fields — they're populated
        # by the app and overwriting can break downstream state.
        if _is_autofilled(el):
            skipped.append((el.name, f"auto-filled / read-only ({tname}) — protected"))
            continue

        value = defaults.get(el.name, section=el.screen_name)
        if not value:
            skipped.append((el.name, "no default declared"))
            continue

        # Buttons that have a default entry are treated as upload triggers.
        # Pages like TECU page 1 sometimes lazy-render the hidden
        # input[type=file], so extract sees only the visible "Add profile
        # picture" button. The upload tool handles the trigger click +
        # post-click input injection itself, so all we need is the field
        # name and file. Buttons WITHOUT a default fall through to skip
        # (Save & Continue, Save & Exit, etc. — those have no default).
        if tname == "button":
            tname = "file_upload"  # route to the upload branch below

        if tname == "file_upload":
            from qa.tools.web_tools import _upload_file_for_field_impl
            doc_type, confirm_label = defaults.get_upload_mode(
                el.name, section=el.screen_name,
            )
            try:
                result_raw = await _upload_file_for_field_impl(
                    field_name=el.name,
                    file_name=value,
                    wait_for_ocr=bool(confirm_label),
                    doc_type=doc_type,
                    confirm_label=confirm_label,
                )
                result = _safe_parse(result_raw) or {}
                status = result.get("status") if isinstance(result, dict) else ""
                if status in ("ATTACHED", "PASS"):
                    note = f"uploaded {value}"
                    if doc_type or confirm_label:
                        note += f" (modal: doc_type={doc_type!r} confirm={confirm_label!r})"
                    filled.append((el.name, note))
                else:
                    skipped.append((el.name, f"upload status={status!r}"))
            except Exception as e:
                skipped.append((el.name, f"upload error: {type(e).__name__}: {e}"))
            continue

        # dropdown / text_input — both go through _set_parent_value which
        # picks the right tool internally and falls through L1 locators.
        try:
            ok, note = await _set_parent_value(
                adapter, kb, defaults, el.element_id,
            )
        except Exception as e:
            skipped.append((el.name, f"fill error: {type(e).__name__}: {e}"))
            continue
        (filled if ok else skipped).append((el.name, note))
        # Brief settle — gives React/Vue change handlers time to fire
        # before we move to the next field (especially cascade parents).
        await asyncio.sleep(0.4)

    return filled, skipped


async def click_save_and_continue(
    adapter: PlatformAdapter,
    labels: tuple[str, ...] | list[str] | None = None,
) -> tuple[bool, str]:
    """Search the current snapshot for a navigation button matching one of
    `labels` (most specific first). Click the first match. Returns
    (clicked, label_used).

    Uses _find_uid_by_text for snapshot-based matching, same approach as
    GatedMultiSectionFlow's tab clicker — synthetic JS clicks fail on
    React/MUI buttons, so we hand the uid to MCP's `click` tool which
    drives a real input event."""
    if labels is None:
        labels = DEFAULT_NAV_LABELS

    snap = await adapter.raw_snapshot_text()
    server = adapter.get_mcp_server()

    for label in labels:
        uid = _find_uid_by_text(snap, label)
        if not uid:
            continue
        result = await server.call_tool("click", {"uid": uid})
        text = ""
        if result.content:
            text = result.content[0].text or ""
        if "error" not in text.lower():
            return (True, label)
        # If MCP errored on this label, try the next one — could be a
        # disabled button or a duplicate match on a non-clickable element.
        print(f"    [wizard] click failed on {label!r}: {text[:80]}")

    return (False, "")


async def classify_transition(
    adapter: PlatformAdapter,
    before_snapshot: str,
    budget,
    guardrails=None,
    model: str = "gpt-5.1",
) -> tuple[str, str, str]:
    """Ask the LLM whether the post-click DOM is a NEW_PAGE, the same
    page with an error, or the same page with an expanded section. The
    deterministic wait_for_page_transition can be fooled by content
    reshuffling (e.g. an inline 'user already exists' error makes body
    length change without advancing the wizard). This narrow LLM call
    fixes that.

    Returns (verdict, reasoning, error_text). verdict is one of:
      NEW_PAGE | SAME_PAGE_WITH_ERROR | SAME_PAGE_WITH_EXPANSION

    On LLM failure (timeout, parse error), returns ('NEW_PAGE',
    'classifier failed — defaulting to NEW_PAGE', '') so the wizard
    keeps going. The deterministic transition check already passed at
    this point; the classifier is a quality gate, not a hard stop."""
    from qa.orchestrators.llm_subtask import llm_classify
    from qa.orchestrators.sub_prompts import (
        CLASSIFY_PAGE_TRANSITION_PROMPT,
        CLASSIFY_PAGE_TRANSITION_SCHEMA,
    )

    after_snapshot = await adapter.raw_snapshot_text()
    user = (
        f"BEFORE_SNAPSHOT:\n{(before_snapshot or '')[:6000]}\n\n"
        f"AFTER_SNAPSHOT:\n{(after_snapshot or '')[:6000]}\n"
    )
    try:
        result = await llm_classify(
            CLASSIFY_PAGE_TRANSITION_PROMPT,
            user,
            CLASSIFY_PAGE_TRANSITION_SCHEMA,
            model=model,
            budget=budget,
            label="classify_transition",
            guardrails=guardrails,
        )
    except Exception as e:
        print(f"  [wizard] transition classifier raised {type(e).__name__}: {e} — defaulting to NEW_PAGE")
        return ("NEW_PAGE", "classifier raised exception — defaulting", "")

    verdict = (result.get("verdict") or "").strip() or "NEW_PAGE"
    reasoning = (result.get("reasoning") or "").strip()
    error_text = (result.get("error_text") or "").strip()
    return (verdict, reasoning, error_text)


async def tag_fields_new_vs_carryover(
    adapter: PlatformAdapter,
    current_field_names: list[str],
    prior_field_names: list[str],
    budget,
    guardrails=None,
    model: str = "gpt-5.1",
) -> dict[str, str]:
    """Ask the LLM to tag each current field as NEW or CARRYOVER given
    what's already been captured on prior pages. Returns a dict
    {field_name: 'NEW' | 'CARRYOVER'}. Fields not returned by the LLM
    default to NEW (don't accidentally drop unknowns).

    Used by the wizard before saving a screen to KB so persistent
    header/footer fields and re-rendered carryovers don't pollute the
    per-page L0 list."""
    from qa.orchestrators.llm_subtask import llm_classify
    from qa.orchestrators.sub_prompts import (
        TAG_NEW_VS_CARRYOVER_PROMPT,
        TAG_NEW_VS_CARRYOVER_SCHEMA,
    )

    if not current_field_names:
        return {}
    if not prior_field_names:
        # First page — nothing to compare against, everything is NEW
        return {n: "NEW" for n in current_field_names}

    snapshot = await adapter.raw_snapshot_text()
    user = (
        "CURRENT page fields:\n"
        + "\n".join(f"  - {n}" for n in current_field_names)
        + "\n\nPRIOR pages already captured these fields:\n"
        + "\n".join(f"  - {n}" for n in prior_field_names)
        + f"\n\nCURRENT page snapshot:\n{(snapshot or '')[:6000]}\n"
    )
    try:
        result = await llm_classify(
            TAG_NEW_VS_CARRYOVER_PROMPT,
            user,
            TAG_NEW_VS_CARRYOVER_SCHEMA,
            model=model,
            budget=budget,
            label="tag_new_vs_carryover",
            guardrails=guardrails,
        )
    except Exception as e:
        print(f"  [wizard] field tagger raised {type(e).__name__}: {e} — keeping all as NEW")
        return {n: "NEW" for n in current_field_names}

    tags: dict[str, str] = {}
    for entry in result.get("tags", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("field_name", "")
        tag = entry.get("tag", "NEW")
        if name and tag in ("NEW", "CARRYOVER"):
            tags[name] = tag
    # Anything the LLM didn't return → keep as NEW (safer than dropping)
    for n in current_field_names:
        tags.setdefault(n, "NEW")
    return tags


async def wait_for_content_render(
    adapter: PlatformAdapter,
    min_interactive_elements: int = 3,
    timeout: float = 15.0,
    poll_interval: float = 0.8,
) -> tuple[bool, int, float]:
    """After a confirmed NEW_PAGE transition, wait for the destination
    page to actually finish rendering. SPAs often show a loading
    spinner/progress bar for 2-10 seconds while data loads, and an
    extract during that window picks up zero interactive elements.

    Polls the DOM until we see at least `min_interactive_elements`
    interactable form controls (input/select/textarea/button), or
    `timeout` is reached. Returns (rendered, count, elapsed).

    `min_interactive_elements=3` because most form pages have at least
    that many — too low (1) and a single 'Cancel' button passes; too
    high (10) and minimal pages are misclassified as still loading."""
    elapsed = 0.0
    count = 0
    js = (
        "() => JSON.stringify({n: document.querySelectorAll("
        "'input:not([type=hidden]), select, textarea, button, "
        "[role=button], [role=combobox], [role=textbox]'"
        ").length})"
    )
    while elapsed < timeout:
        try:
            raw = await adapter.evaluate_script(js)
            parsed = _safe_parse(raw) or {}
            count = int(parsed.get("n", 0)) if isinstance(parsed, dict) else 0
        except Exception:
            count = 0
        if count >= min_interactive_elements:
            return (True, count, elapsed)
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return (False, count, elapsed)


async def page_signature(adapter: PlatformAdapter) -> dict | None:
    """Capture a stable-ish signature for the current page. Used by
    wait_for_page_transition to detect SPA navigation that doesn't change
    window.location (route-based pages where the URL hash/path is identical
    across wizard steps). Combines: URL, main heading text, body length."""
    js = (
        "() => {"
        "  const h = document.querySelector('h1, h2, [role=heading]');"
        "  return JSON.stringify({"
        "    heading: h ? (h.textContent||'').trim().slice(0, 200) : '',"
        "    url: window.location.href,"
        "    body_len: (document.body.innerText || '').length"
        "  });"
        "}"
    )
    raw = await adapter.evaluate_script(js)
    parsed = _safe_parse(raw)
    return parsed if isinstance(parsed, dict) else None


async def wait_for_page_transition(
    adapter: PlatformAdapter,
    before: dict | None,
    timeout: float = 10.0,
    poll_interval: float = 0.6,
) -> tuple[bool, str]:
    """Poll until either:
      • window.location.href changes from `before['url']`, OR
      • main heading text changes from `before['heading']`, OR
      • body_len changes by more than ~25% (catches major DOM swaps that
        keep the same heading text — e.g. a wizard that just shuffles
        section content).

    Returns (transitioned, signal) where signal describes which check
    fired ('url', 'heading', 'body', or 'timeout')."""
    if not before:
        return (False, "no_baseline")

    elapsed = 0.0
    before_url = before.get("url", "")
    before_heading = (before.get("heading") or "").strip()
    before_len = int(before.get("body_len") or 0)

    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        cur = await page_signature(adapter)
        if not cur:
            continue
        if cur.get("url") and cur["url"] != before_url:
            return (True, "url")
        cur_heading = (cur.get("heading") or "").strip()
        if cur_heading and cur_heading != before_heading:
            return (True, "heading")
        cur_len = int(cur.get("body_len") or 0)
        if before_len and abs(cur_len - before_len) / max(before_len, 1) > 0.25:
            return (True, "body")

    return (False, "timeout")
