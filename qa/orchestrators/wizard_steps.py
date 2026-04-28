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
DEFAULT_NAV_LABELS = (
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

    by_type: dict[str, list] = {
        "dropdown": [],
        "text_input": [],
        "file_upload": [],
        "other": [],
    }
    for el in screen.l0:
        tname = el.type.value if hasattr(el.type, "value") else str(el.type)
        if tname in by_type:
            by_type[tname].append(el)
        else:
            by_type["other"].append(el)

    ordered = (
        by_type["dropdown"] + by_type["text_input"]
        + by_type["file_upload"] + by_type["other"]
    )

    for el in ordered:
        tname = el.type.value if hasattr(el.type, "value") else str(el.type)
        if tname in ("date_picker", "button"):
            skipped.append((el.name, f"{tname} — wizard skips"))
            continue

        value = defaults.get(el.name, section=el.screen_name)
        if not value:
            skipped.append((el.name, "no default declared"))
            continue

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
