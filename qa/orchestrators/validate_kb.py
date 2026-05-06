# qa/orchestrators/validate_kb.py — Deterministic KB-vs-page validator.
#
# Takes a saved knowledge base + a live URL and verifies, locator-by-locator,
# whether each element the KB claims to know about is actually reachable on
# the page right now. Pure JS scan + Python orchestration. No LLM.
#
# Why: extract sometimes saves elements with empty/broken locators (e.g.
# placeholder-only textareas), and execute's smart-fallback can silently
# match the wrong DOM node. A single round-trip "is this still there?"
# pass between extract and execute prevents both classes of failure.
#
# Per-element verdicts:
#   REACHABLE          — at least one locator gives a unique, visible match
#   HIDDEN             — locator matches uniquely but element is not visible
#   DUPLICATE          — every locator returns >1 match (ambiguous)
#   BROKEN             — locators present but none match anything on page
#   NO_LOCATORS        — KB stored the element with empty CSS + XPath
#   LABEL_ONLY         — only a label-text fallback is left (low confidence)
#
# Usage:
#   python -m qa.orchestrators.validate_kb <app-name> [--url <url>] [--wait]
#
# If --url is omitted, the KB's stored screen_url is used.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from qa.adapters import make_adapter
from qa.knowledge.store import KnowledgeStore
from qa.models import KnowledgeBase, Platform, TargetApp
from qa.tools.web_tools import _safe_parse


# ── JS scanner ───────────────────────────────────────────────────────────────
#
# Receives a list of {element_id, locators: [{strategy, value}, ...]} entries
# embedded as a JSON literal. For each locator it records:
#   match_count    — how many DOM nodes match
#   visible_count  — of those, how many are actually rendered (offsetParent
#                    non-null + non-zero rect)
#   status         — unique | duplicate | no_match | invalid_syntax | unsupported
#   sample         — short label of the first match (helps spot wrong-element
#                    fallback bugs)
#
# Single round-trip; the entire L1 list goes in, the entire result comes out.

VALIDATE_KB_JS_TEMPLATE = r"""
() => {
  const ENTRIES = __ENTRIES__;

  function isVisible(el) {
    if (!el) return false;
    if (el.offsetParent === null) {
      // <body>, <html>, fixed-position elements have no offsetParent — fall
      // back to bounding rect.
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    }
    const r = el.getBoundingClientRect();
    return (r.width > 0 && r.height > 0);
  }

  function describe(el) {
    if (!el) return '';
    const tag = el.tagName ? el.tagName.toLowerCase() : '?';
    const id = el.id ? ('#' + el.id) : '';
    const name = el.getAttribute && el.getAttribute('name')
      ? ('[name=' + el.getAttribute('name') + ']') : '';
    const role = el.getAttribute && el.getAttribute('role')
      ? ('[role=' + el.getAttribute('role') + ']') : '';
    const aria = el.getAttribute && el.getAttribute('aria-label')
      ? ('[aria-label="' + (el.getAttribute('aria-label') || '').slice(0, 30) + '"]')
      : '';
    const txt = (el.textContent || '').trim().slice(0, 40);
    const place = el.getAttribute && el.getAttribute('placeholder')
      ? ('[placeholder="' + el.getAttribute('placeholder').slice(0, 30) + '"]')
      : '';
    return tag + id + name + role + aria + place + (txt ? (' "' + txt + '"') : '');
  }

  function evalCss(sel) {
    try {
      const all = document.querySelectorAll(sel);
      let visible = 0;
      let firstVisible = null;
      for (const el of all) {
        if (isVisible(el)) {
          visible++;
          if (!firstVisible) firstVisible = el;
        }
      }
      const sample = firstVisible || all[0] || null;
      return {
        status: all.length === 0 ? 'no_match'
              : all.length === 1 ? 'unique'
              : 'duplicate',
        match_count: all.length,
        visible_count: visible,
        sample: describe(sample),
      };
    } catch (e) {
      return { status: 'invalid_syntax', match_count: 0, visible_count: 0,
               sample: '', error: String(e).slice(0, 120) };
    }
  }

  function evalXpath(xp) {
    try {
      const r = document.evaluate(xp, document, null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      let visible = 0;
      let firstVisible = null;
      for (let i = 0; i < r.snapshotLength; i++) {
        const el = r.snapshotItem(i);
        if (isVisible(el)) {
          visible++;
          if (!firstVisible) firstVisible = el;
        }
      }
      const sample = firstVisible || (r.snapshotLength ? r.snapshotItem(0) : null);
      return {
        status: r.snapshotLength === 0 ? 'no_match'
              : r.snapshotLength === 1 ? 'unique'
              : 'duplicate',
        match_count: r.snapshotLength,
        visible_count: visible,
        sample: describe(sample),
      };
    } catch (e) {
      return { status: 'invalid_syntax', match_count: 0, visible_count: 0,
               sample: '', error: String(e).slice(0, 120) };
    }
  }

  function evalLabel(text) {
    // Low-confidence fallback used when extract had nothing else. Counts how
    // many interactive elements have the label-text inside them.
    const needle = (text || '').trim().toLowerCase();
    if (!needle) {
      return { status: 'no_match', match_count: 0, visible_count: 0, sample: '' };
    }
    const candidates = document.querySelectorAll(
      'button, a[href], [role="button"], label, input[placeholder], textarea[placeholder]'
    );
    let count = 0;
    let visible = 0;
    let firstVisible = null;
    for (const el of candidates) {
      const haystack = (
        (el.textContent || '') + ' ' +
        (el.getAttribute('placeholder') || '') + ' ' +
        (el.getAttribute('aria-label') || '')
      ).toLowerCase();
      if (haystack.includes(needle)) {
        count++;
        if (isVisible(el)) {
          visible++;
          if (!firstVisible) firstVisible = el;
        }
      }
    }
    return {
      status: count === 0 ? 'no_match'
            : count === 1 ? 'unique'
            : 'duplicate',
      match_count: count,
      visible_count: visible,
      sample: describe(firstVisible),
    };
  }

  const out = [];
  for (const entry of ENTRIES) {
    const eid = entry.element_id || '';
    const locResults = [];
    let bestStatus = null;

    for (const loc of (entry.locators || [])) {
      const strategy = (loc.strategy || '').toLowerCase();
      const value = loc.value || '';
      let r;
      if (!value) {
        r = { status: 'unsupported', match_count: 0, visible_count: 0,
              sample: '', error: 'empty value' };
      } else if (strategy === 'css') {
        r = evalCss(value);
      } else if (strategy === 'xpath') {
        r = evalXpath(value);
      } else if (strategy === 'label') {
        r = evalLabel(value);
      } else {
        r = { status: 'unsupported', match_count: 0, visible_count: 0,
              sample: '', error: 'strategy not validatable on web' };
      }
      r.strategy = strategy;
      r.value = value;
      r.confidence = (typeof loc.confidence === 'number') ? loc.confidence : 0;
      locResults.push(r);
    }

    out.push({
      element_id: eid,
      locator_count: locResults.length,
      locators: locResults,
    });
  }

  return JSON.stringify({
    page_url: window.location.href,
    page_title: document.title || '',
    total_entries: ENTRIES.length,
    results: out,
  });
}
"""


# ── Verdict logic (Python) ───────────────────────────────────────────────────
#
# Promotion order: a single REACHABLE locator wins; otherwise we report the
# best-available evidence. NO_LOCATORS / LABEL_ONLY are extract-side
# observations independent of the live page.

VERDICT_REACHABLE = "REACHABLE"
VERDICT_HIDDEN = "HIDDEN"
VERDICT_DUPLICATE = "DUPLICATE"
VERDICT_BROKEN = "BROKEN"
VERDICT_NO_LOCATORS = "NO_LOCATORS"
VERDICT_LABEL_ONLY = "LABEL_ONLY"


def _verdict_for(entry: dict) -> tuple[str, dict]:
    """Compute per-element verdict. Returns (verdict, best_locator_dict|{})."""
    locators = entry.get("locators") or []
    if not locators:
        return VERDICT_NO_LOCATORS, {}

    non_label = [l for l in locators if l.get("strategy") in ("css", "xpath")]
    if not non_label and all(l.get("strategy") == "label" for l in locators):
        # Extract had nothing concrete to give us. Even if the label scan
        # finds something, this is still low-confidence and execute will
        # have to fight to use it.
        for l in locators:
            if l.get("status") == "unique" and l.get("visible_count", 0) > 0:
                return VERDICT_LABEL_ONLY, l
        return VERDICT_NO_LOCATORS, {}

    # Pass 1 — any unique + visible CSS/XPath locator?
    for l in non_label:
        if l.get("status") == "unique" and l.get("visible_count", 0) > 0:
            return VERDICT_REACHABLE, l

    # Pass 2 — unique but hidden (extract saw it once, but it's not on
    # screen now: collapsed accordion, lazy-rendered tab, etc.)
    for l in non_label:
        if l.get("status") == "unique" and l.get("visible_count", 0) == 0:
            return VERDICT_HIDDEN, l

    # Pass 3 — every locator matches >1 node. Smart-fallback would pick
    # the wrong one; flag for the operator before execute hits it.
    if non_label and all(l.get("status") == "duplicate" for l in non_label):
        return VERDICT_DUPLICATE, non_label[0]

    # Pass 4 — any duplicate (ambiguous) is more useful than no match.
    for l in non_label:
        if l.get("status") == "duplicate":
            return VERDICT_DUPLICATE, l

    return VERDICT_BROKEN, non_label[0] if non_label else {}


def _safe_app_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "app").lower()).strip("_") or "app"


def _resolve_kb(app_name: str) -> KnowledgeBase:
    store = KnowledgeStore()
    kb = store.load_by_name(app_name, platform="web")
    if kb is None:
        # Fall back: try the path-formed app slug as a TargetApp lookup.
        app = TargetApp(platform=Platform.WEB, app_name=app_name)
        kb = store.load(app)
    if kb is None:
        raise SystemExit(
            f"No KB found for app_name='{app_name}' under artifacts/knowledge/web/. "
            "Run an extract first (qa.cli explore or qa.orchestrators.exhaustive_extract)."
        )
    return kb


def _flatten_l1(kb: KnowledgeBase, screen_name: str = "") -> list[dict]:
    """Return [{element_id, locators: [{strategy, value, confidence}, ...]}, ...]
    for the requested screen (or all screens, if none specified)."""
    entries: list[dict] = []
    for screen in kb.screens:
        if screen_name and screen.screen_name != screen_name:
            continue
        for l1 in screen.l1:
            entries.append({
                "element_id": l1.element_id,
                "screen_name": screen.screen_name,
                "locators": [
                    {
                        "strategy": loc.strategy,
                        "value": loc.value,
                        "confidence": loc.confidence,
                    }
                    for loc in l1.locators
                ],
            })
    return entries


def _l0_lookup(kb: KnowledgeBase) -> dict[str, str]:
    """element_id → human label, for the report."""
    out: dict[str, str] = {}
    for screen in kb.screens:
        for el in screen.l0:
            out[el.element_id] = el.name
    return out


# ── Run ──────────────────────────────────────────────────────────────────────


async def run_validate_kb(
    app_name: str,
    *,
    url: str = "",
    screen_name: str = "",
    wait: bool = False,
) -> dict:
    kb = _resolve_kb(app_name)
    entries = _flatten_l1(kb, screen_name=screen_name)
    if not entries:
        raise SystemExit(
            f"KB for '{app_name}' has no L1 entries"
            + (f" for screen '{screen_name}'" if screen_name else "")
            + ". Nothing to validate."
        )

    target_url = url or (kb.screens[0].screen_url if kb.screens else "") or kb.app.url or ""
    if not target_url:
        raise SystemExit(
            "No URL to validate against. Pass --url, or save a screen_url in the KB."
        )

    app = TargetApp(platform=Platform.WEB, url=target_url, app_name=app_name)
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)
    try:
        if wait:
            print()
            print("=" * 60)
            print("  Browser open. Login + navigate to the page you want to validate.")
            print("  When the page is fully visible, press Enter to scan.")
            print("=" * 60)
            try:
                input("  >>> Press Enter... ")
            except EOFError:
                pass
        else:
            await asyncio.sleep(2.5)

        # Inject the L1 list as a JSON literal so the JS side doesn't have
        # to deserialize anything. Single round-trip, no per-element calls.
        entries_json = json.dumps(entries)
        js = VALIDATE_KB_JS_TEMPLATE.replace("__ENTRIES__", entries_json)
        raw = await adapter._call("evaluate_script", {"function": js})
        payload = _safe_parse(raw)
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"validator returned unparseable output: {str(raw)[:500]}"
            )
        return {"kb_app_name": app_name, "kb": kb, "scan": payload, "entries": entries}
    finally:
        await adapter.close()


# ── Report ───────────────────────────────────────────────────────────────────


def _build_report(
    kb: KnowledgeBase,
    scan: dict,
    entries: list[dict],
) -> tuple[dict, str]:
    """Return (json_payload, text_report)."""
    name_by_id = _l0_lookup(kb)
    screen_by_id = {e["element_id"]: e.get("screen_name", "") for e in entries}

    per_element: list[dict] = []
    counters: dict[str, int] = {
        VERDICT_REACHABLE: 0,
        VERDICT_HIDDEN: 0,
        VERDICT_DUPLICATE: 0,
        VERDICT_BROKEN: 0,
        VERDICT_NO_LOCATORS: 0,
        VERDICT_LABEL_ONLY: 0,
    }

    for entry in scan.get("results", []):
        verdict, best = _verdict_for(entry)
        counters[verdict] = counters.get(verdict, 0) + 1
        eid = entry.get("element_id", "")
        per_element.append({
            "element_id": eid,
            "name": name_by_id.get(eid, ""),
            "screen": screen_by_id.get(eid, ""),
            "verdict": verdict,
            "best_locator": {
                "strategy": best.get("strategy", ""),
                "value": best.get("value", ""),
                "match_count": best.get("match_count", 0),
                "visible_count": best.get("visible_count", 0),
                "sample": best.get("sample", ""),
            } if best else {},
            "locators": entry.get("locators", []),
        })

    total = len(per_element)
    payload = {
        "page_url": scan.get("page_url", ""),
        "page_title": scan.get("page_title", ""),
        "kb_app_name": kb.app.app_name,
        "kb_screens": [s.screen_name for s in kb.screens],
        "summary": {
            "total": total,
            "reachable": counters[VERDICT_REACHABLE],
            "hidden": counters[VERDICT_HIDDEN],
            "duplicate": counters[VERDICT_DUPLICATE],
            "broken": counters[VERDICT_BROKEN],
            "no_locators": counters[VERDICT_NO_LOCATORS],
            "label_only": counters[VERDICT_LABEL_ONLY],
            "reachable_pct": round(100 * counters[VERDICT_REACHABLE] / total, 1) if total else 0.0,
        },
        "elements": per_element,
    }

    # Text report
    lines: list[str] = [
        "KB Validation Report",
        "=" * 60,
        f"App:       {kb.app.app_name}",
        f"URL:       {scan.get('page_url', '')}",
        f"Title:     {scan.get('page_title', '')}",
        f"KB screens: {', '.join(s.screen_name for s in kb.screens)}",
        "",
        f"Total elements:   {total}",
        f"  REACHABLE       {counters[VERDICT_REACHABLE]:>4}   ({payload['summary']['reachable_pct']}%)",
        f"  HIDDEN          {counters[VERDICT_HIDDEN]:>4}",
        f"  DUPLICATE       {counters[VERDICT_DUPLICATE]:>4}",
        f"  BROKEN          {counters[VERDICT_BROKEN]:>4}",
        f"  NO_LOCATORS     {counters[VERDICT_NO_LOCATORS]:>4}",
        f"  LABEL_ONLY      {counters[VERDICT_LABEL_ONLY]:>4}",
    ]

    def _section(verdict: str, header: str) -> None:
        items = [e for e in per_element if e["verdict"] == verdict]
        if not items:
            return
        lines.append("")
        lines.append(f"── {header} ({len(items)}) " + "─" * (44 - len(header)))
        for el in items:
            label = el["name"] or "(unlabeled)"
            best = el.get("best_locator") or {}
            mc = best.get("match_count", 0)
            vc = best.get("visible_count", 0)
            sample = best.get("sample", "")
            head = f"  • {el['element_id']}"
            lines.append(head)
            lines.append(f"      label:   {label}")
            if best.get("strategy"):
                lines.append(
                    f"      via:     {best['strategy']}={best['value'][:80]}  "
                    f"(matches={mc}, visible={vc})"
                )
            if sample:
                lines.append(f"      sample:  {sample[:120]}")

    # Always show non-green sections so issues stand out.
    _section(VERDICT_BROKEN, "BROKEN — locators present but match nothing")
    _section(VERDICT_DUPLICATE, "DUPLICATE — every locator matches multiple nodes")
    _section(VERDICT_HIDDEN, "HIDDEN — found uniquely but not visible")
    _section(VERDICT_NO_LOCATORS, "NO_LOCATORS — KB stored empty CSS + XPath")
    _section(VERDICT_LABEL_ONLY, "LABEL_ONLY — only label fallback available")

    if counters[VERDICT_REACHABLE] == total:
        lines.append("")
        lines.append("✓ All KB elements are reachable on this page.")

    return payload, "\n".join(lines) + "\n"


def _save_report(app_name: str, payload: dict, report_text: str) -> tuple[Path, Path]:
    now = datetime.now()
    day_dir = Path("artifacts/results") / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d_%H%M%S")
    safe = _safe_app_name(app_name)
    json_path = day_dir / f"{safe}_kb_validation_{ts}.json"
    txt_path = day_dir / f"{safe}_kb_validation_{ts}.txt"
    json_path.write_text(json.dumps(payload, indent=2))
    txt_path.write_text(report_text)
    return json_path, txt_path


# ── CLI ──────────────────────────────────────────────────────────────────────


async def _amain() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "KB validator — for each L1 locator in the saved knowledge base, "
            "verify it still resolves on the live page. No LLM."
        ),
    )
    ap.add_argument("app_name", help="App name (matches artifacts/knowledge/web/<app>.json)")
    ap.add_argument(
        "--url",
        default="",
        help="Override URL (default: KB's saved screen_url)",
    )
    ap.add_argument(
        "--screen",
        default="",
        help="Restrict validation to a single screen by name",
    )
    ap.add_argument(
        "--wait",
        action="store_true",
        help="Pause after launch so you can login / navigate before scanning",
    )
    args = ap.parse_args()

    result = await run_validate_kb(
        args.app_name,
        url=args.url,
        screen_name=args.screen,
        wait=args.wait,
    )
    payload, report_text = _build_report(result["kb"], result["scan"], result["entries"])

    print()
    print(report_text)

    json_path, txt_path = _save_report(args.app_name, payload, report_text)
    print(f"Report saved: {txt_path}")
    print(f"JSON saved:   {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
