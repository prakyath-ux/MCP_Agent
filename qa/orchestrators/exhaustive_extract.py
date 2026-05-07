# qa/orchestrators/exhaustive_extract.py — Comprehensive DOM-based extract.
#
# Principle: extract should be exhaustive. Every interactable element on the
# page lands in the KB. No silent filtering. The plan/execute layers decide
# what's worth testing.
#
# Differences from the LLM-driven `qa.cli explore`:
#   - Pure JS DOM scan, no LLM (free, deterministic, reproducible)
#   - Wider element catalog: input/select/textarea/button/<a> AND role=button,
#     role=combobox, role=radio, role=switch, role=checkbox, role=tab,
#     role=listbox, contenteditable
#   - Saves directly to artifacts/knowledge/web/<app>.json (same shape used
#     by plan + execute pipelines, so they consume it without changes)
#
# Usage:
#   python -m qa.orchestrators.exhaustive_extract <url> --app-name <name> [--wait]

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from qa.adapters import make_adapter
from qa.knowledge.store import KnowledgeStore
from qa.models import KnowledgeBase, Platform, TargetApp
from qa.models.common import ElementType, Locator, make_element_id
from qa.models.knowledge import L0Element, L1Element, L2Element, ScreenKnowledge
from qa.tools.web_tools import EXHAUSTIVE_SCAN_JS, _safe_parse


# ── Phase 1: dropdown options discovery (interactive expansion) ─────────────
#
# Single-pass DOM scan can't see options that only render after a dropdown
# trigger is clicked. After the main scan, this pass re-visits each
# dropdown whose options[] is empty, opens the popup, captures up to N
# visible options, then restores the page state with Escape.
#
# Three sequential MCP round-trips per dropdown:
#   1. Click trigger (resolved via XPath from L1)
#   2. After 0.4s, scan visible popup options with a broad selector union;
#      smart-fallback to leaf-text inside listbox/menu containers when
#      ARIA markup is missing (TECU branch list, etc.)
#   3. Press Escape to close the popup so subsequent scans/expansions
#      start from a clean state
#
# Cap (default 5) keeps L0 token weight bounded — a 200-country list adds
# ~80 tokens, not 4 KB. Plan picks a single value per test case anyway.

_PLACEHOLDER_RE = re.compile(
    r"^(select|choose|please\s+select|--\s*select\s*--|select\s+an\s+option|"
    r"choose\s+an\s+option|select\.\.\.)$",
    re.IGNORECASE,
)


async def _expand_dropdown(adapter, xpath: str, max_options: int = 5) -> list[str]:
    """Open a dropdown via XPath, capture up to N options, close it.

    Strategy: take a pre-click snapshot of every visible element, then
    click, wait, and scan for elements that became visible (or were
    added to the DOM) during that window. Newly-visible leaf-text nodes
    are option candidates. This generalizes across popup styles —
    portals, in-flow blocks, position-absolute overlays, etc.

    Falls back to known role/class popup containers if the diff misses.

    Returns [] on any failure. Failures are silent — extract continues
    with options[] empty and Plan falls back to its FIRST sentinel.
    """
    if not xpath:
        return []
    xp_lit = json.dumps(xpath)

    # All four phases (snapshot, click, wait, diff) run inside a single
    # async JS function. Object-identity checks (Set of element refs)
    # only work in one execution context — splitting across MCP calls
    # would lose the references between snapshots.
    expand_js = f"""
      async () => {{
        const xpath = {xp_lit};
          let trigger = null;
          try {{
            const r = document.evaluate(xpath, document, null,
              XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            trigger = r.singleNodeValue;
          }} catch (e) {{
            return JSON.stringify({{status: 'BAD_XPATH', error: String(e).slice(0, 120)}});
          }}
          if (!trigger) return JSON.stringify({{status: 'NOT_FOUND'}});

          // Cache the trigger's text BEFORE click. After click, the
          // trigger's subtree absorbs the popup options, so its
          // textContent inflates to a concatenation of every option.
          // We use this both to skip the trigger element itself and to
          // drop placeholder options that mirror the trigger label.
          const triggerTextBefore = (trigger.textContent || '').trim();

          // Pre-click snapshot: WeakSet-style identity for visible elements.
          const isVisible = (e) => {{
            if (!e || e.nodeType !== 1) return false;
            if (e.offsetParent !== null) return true;
            const cs = window.getComputedStyle(e);
            return cs.display !== 'none' && cs.visibility !== 'hidden';
          }};
          const preVisible = new Set();
          for (const e of document.querySelectorAll('*')) {{
            if (isVisible(e)) preVisible.add(e);
          }}

          trigger.scrollIntoView({{block: 'center', behavior: 'instant'}});
          trigger.click();
          try {{ trigger.focus({{preventScroll: true}}); }} catch (_) {{}}
          trigger.dispatchEvent(new KeyboardEvent('keydown',
            {{key: 'ArrowDown', code: 'ArrowDown', bubbles: true, cancelable: true}}));

          await new Promise(r => setTimeout(r, 400));

          // Newly-visible elements = popup contents (in any style).
          const newlyVisible = [];
          for (const e of document.querySelectorAll('*')) {{
            if (isVisible(e) && !preVisible.has(e)) newlyVisible.push(e);
          }}

          // Pass 1: standard option markup inside newly-visible nodes.
          const standard = '[role=option], [role=menuitem], li[role=listitem], '
            + '.MuiMenuItem-root, .dropdown-item, '
            + '[class*="select__option"], [class*="dropdown__option"], '
            + '[class*="combobox__option"], [class*="-Option"], [data-option-index]';
          let opts = [];
          for (const root of newlyVisible) {{
            const matches = root.matches && root.matches(standard) ? [root] : [];
            const desc = [...root.querySelectorAll(standard)];
            for (const m of [...matches, ...desc]) {{
              const t = (m.textContent || '').trim();
              if (t) opts.push(t);
            }}
          }}

          // Pass 2: leaf-text inside newly-visible if standard markup absent.
          if (opts.length === 0) {{
            for (const root of newlyVisible) {{
              const t = (root.textContent || '').trim();
              if (!t || t.length > 100) continue;
              // Skip if any child of root has the SAME text — keep leaf-ish only.
              let isLeaf = true;
              for (const c of root.children) {{
                const ct = (c.textContent || '').trim();
                if (ct === t) {{ isLeaf = false; break; }}
              }}
              if (isLeaf) opts.push(t);
            }}
          }}

          // Pass 3: known popup containers by role/class as a final safety net.
          if (opts.length === 0) {{
            const fallback = [...document.querySelectorAll(
              '[role=listbox], [role=menu], [class*="popup"], '
              + '[class*="dropdown"]:not(button), [class*="menu"]:not(button)'
            )].filter(p => isVisible(p));
            for (const popup of fallback) {{
              const leafs = [...popup.querySelectorAll('*')].filter(l => {{
                if (!isVisible(l)) return false;
                const t = (l.textContent || '').trim();
                if (!t || t.length > 100) return false;
                for (const c of l.children) {{
                  const ct = (c.textContent || '').trim();
                  if (ct === t) return false;
                }}
                return true;
              }});
              const texts = leafs.map(l => (l.textContent || '').trim()).filter(t => t);
              if (texts.length > 0) {{ opts = texts; break; }}
            }}
          }}

          // ── Escalating close: try cheap moves first, verify after each,
          // and fall back to clicking the trigger again (toggles popup
          // closed on most React-Select / Headless UI / Radix patterns).
          // Verification uses the same DOM-diff: any element visible NOW
          // that wasn't in `preVisible` is popup-rendered content. If that
          // count is zero, the popup is closed.
          const popupStillOpen = () => {{
            for (const e of document.querySelectorAll('*')) {{
              if (isVisible(e) && !preVisible.has(e)) return true;
            }}
            return false;
          }};

          // Step A: Escape on document and active element + blur
          try {{
            document.dispatchEvent(new KeyboardEvent('keydown',
              {{key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}}));
          }} catch (_) {{}}
          if (document.activeElement && document.activeElement !== document.body) {{
            try {{
              document.activeElement.dispatchEvent(new KeyboardEvent('keydown',
                {{key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}}));
              document.activeElement.blur();
            }} catch (_) {{}}
          }}
          await new Promise(r => setTimeout(r, 120));

          // Step B (Plan B): re-click the trigger to TOGGLE the popup
          // closed. Most app dropdowns (TECU's Branch, Forgenite's
          // Communication Tone) are toggle-style — the same click that
          // opened them closes them. Only run if the popup is still open
          // after Step A, so we don't reopen a successfully-closed popup.
          if (popupStillOpen()) {{
            try {{ trigger.click(); }} catch (_) {{}}
            await new Promise(r => setTimeout(r, 120));
          }}

          // Step C (last resort): click outside on documentElement.
          if (popupStillOpen()) {{
            try {{ document.documentElement.click(); }} catch (_) {{}}
            await new Promise(r => setTimeout(r, 80));
          }}

          // Post-filters:
          //   1. Drop the trigger's pre-click text (placeholder leak).
          //   2. Drop the trigger element's post-click textContent
          //      (concatenation of every option, e.g. "Select XSemi-FormalFriendly…").
          //   3. Drop superset texts that wholly contain a shorter
          //      sibling option — same wrapper-concat shape after step 2.
          opts = opts.filter(t => t && t !== triggerTextBefore && t !== (trigger.textContent || '').trim());
          // Wrapper-concat detection: a wrapper element typically contains
          // EVERY option's text concatenated. Require 2+ other options as
          // substrings before dropping — a single-substring overlap is
          // legitimate (e.g. "Semi-Formal" contains "Formal" but is its
          // own valid option).
          opts = opts.filter((t, i) => {{
            const containedOthers = opts.filter((other, j) =>
              j !== i && other.length >= 2 && t.length > other.length && t.includes(other)
            ).length;
            return containedOthers < 2;
          }});

          return JSON.stringify({{
            status: 'OK',
            options: opts,
            new_elements_count: newlyVisible.length,
          }});
      }}
    """

    raw = await adapter._call("evaluate_script", {"function": expand_js})
    parsed = _safe_parse(raw)
    if not isinstance(parsed, dict):
        # Most likely cause: MCP didn't await the async fn and we got back
        # a Promise serialization. Helpful to see exactly what came out.
        print(f"      [debug] expand returned non-dict: {str(raw)[:160]}")
        return []
    if parsed.get("status") != "OK":
        print(f"      [debug] expand status={parsed.get('status')} "
              f"err={parsed.get('error', '')}")
        return []

    raw_opts = parsed.get("options") or []
    new_count = parsed.get("new_elements_count", "?")
    if not isinstance(raw_opts, list):
        print(f"      [debug] options not a list: {type(raw_opts).__name__}")
        return []
    if not raw_opts:
        print(f"      [debug] OK status, but 0 options found "
              f"(newly-visible elements: {new_count})")

    seen: set[str] = set()
    cleaned: list[str] = []
    for o in raw_opts:
        if not isinstance(o, str):
            continue
        text = o.strip()
        key = text.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if _PLACEHOLDER_RE.match(text):
            continue
        cleaned.append(text)
        if len(cleaned) >= max_options:
            break
    return cleaned


# ── Phase 2: radio-group expansion (conditional flow discovery) ─────────────
#
# Each radio option may toggle the visibility of a different set of fields
# (Voice Bot reveals voice config; Form lead-capture reveals chip toggles).
# After Phase 1, walk every native radio group, click each option, re-scan
# the DOM, and record any newly-visible elements as conditional fields
# tagged with depends_on=[radio_group, option_label]. Aria-radios are
# skipped in v1 — most production apps still use `<input type=radio>`.
#
# State management (in-place, no reload):
#   - For each group, click each option in turn; restore by re-clicking
#     the first option at the end. Best-effort: some apps require a
#     specific default which we may not match exactly.
#   - Cascading hides aren't tracked explicitly; we record additions only.
#     Plan handles depends_on when generating tests.

_RADIO_NAME_RE = re.compile(r"@name\s*=\s*['\"]([^'\"]+)['\"]")


_CLOSE_POPUPS_JS = """
  () => {
    // Multi-strategy popup dismissal — Escape doesn't always reach popup
    // listeners (esp. React portal-mounted menus that listen for outside
    // click on document.body). Try them all; idempotent.
    try {
      document.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}));
    } catch (_) {}
    try {
      document.body.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}));
    } catch (_) {}
    try {
      if (document.activeElement && document.activeElement !== document.body) {
        document.activeElement.dispatchEvent(new KeyboardEvent('keydown',
          {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}));
        document.activeElement.blur();
      }
    } catch (_) {}
    try { document.body.click(); } catch (_) {}
    try { document.documentElement.click(); } catch (_) {}
    return JSON.stringify({status: 'CLOSED'});
  }
"""


async def _close_all_popups(adapter) -> None:
    """Best-effort dismissal of any open popup, modal, or focused widget.
    Idempotent — safe to call repeatedly between phases / rescans."""
    try:
        await adapter._call("evaluate_script", {"function": _CLOSE_POPUPS_JS})
        await asyncio.sleep(0.15)
    except Exception:
        pass


async def _filter_popup_descendants(adapter, candidates: list[dict]) -> tuple[list[dict], int]:
    """Drop any candidate whose XPath resolves to an element living inside
    a popup container (`[role=listbox]`, `[role=menu]`, `[class*=popup]`,
    `[class*=dropdown]:not(button)`, `[class*=menu]:not(button)`).

    This is Layer 2 defense: even if the close logic in Phase 1 fails on
    some unknown app, popup-internal contents never get tagged as
    conditional fields.

    Single batched MCP call for all candidates. Returns (kept, dropped_count).
    """
    if not candidates:
        return [], 0
    xpaths = [c.get("xpath") for c in candidates]
    xpaths_json = json.dumps(xpaths)
    js = f"""
      () => {{
        const xpaths = {xpaths_json};
        const popupSel = '[role=listbox], [role=menu], [class*="popup" i]:not(button), '
          + '[class*="dropdown" i]:not(button), [class*="menu" i]:not(button)';
        return JSON.stringify(xpaths.map(xp => {{
          if (!xp) return false;
          try {{
            const r = document.evaluate(xp, document, null,
              XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            let cur = r.singleNodeValue;
            while (cur && cur.nodeType === 1) {{
              if (cur.matches && cur.matches(popupSel)) return true;
              cur = cur.parentElement;
            }}
            return false;
          }} catch (_) {{ return false; }}
        }}));
      }}
    """
    raw = await adapter._call("evaluate_script", {"function": js})
    parsed = _safe_parse(raw)
    if not isinstance(parsed, list) or len(parsed) != len(candidates):
        # Filter failed — return everything (trust upstream). Better to
        # over-include with depends_on tags than to silently drop fields.
        return candidates, 0
    kept = [c for c, in_popup in zip(candidates, parsed) if not in_popup]
    return kept, len(candidates) - len(kept)


def _extract_name_attr_from_xpath(xpath: str) -> str:
    if not xpath:
        return ""
    m = _RADIO_NAME_RE.search(xpath)
    return m.group(1) if m else ""


def _build_radio_click_js(group_name: str, option_label: str) -> str:
    """Click a native radio by its `name` attribute and visible label.
    Tries label[for=id] → wrapping <label> → aria-label → value, in that
    order, and matches case-insensitively to tolerate small differences."""
    gn = json.dumps(group_name)
    ol = json.dumps(option_label)
    return f"""
      () => {{
        const groupName = {gn};
        const optionLabel = {ol};
        const inputs = [...document.querySelectorAll(
          'input[type="radio"][name="' + groupName + '"]'
        )];
        if (inputs.length === 0) return JSON.stringify({{status: 'NO_GROUP'}});

        const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        const target = norm(optionLabel);

        let pick = null;
        for (const inp of inputs) {{
          let label = '';
          if (inp.id) {{
            const lbl = document.querySelector('label[for="' + inp.id + '"]');
            if (lbl) label = (lbl.textContent || '').trim();
          }}
          if (!label) {{
            const wrap = inp.closest('label');
            if (wrap) label = (wrap.textContent || '').trim();
          }}
          if (!label) label = inp.getAttribute('aria-label') || '';
          if (!label) label = inp.value || '';

          const labNorm = norm(label);
          if (labNorm === target || labNorm.includes(target) || target.includes(labNorm)) {{
            pick = inp;
            break;
          }}
        }}

        if (!pick) return JSON.stringify({{status: 'OPTION_NOT_FOUND'}});
        pick.scrollIntoView({{block: 'center', behavior: 'instant'}});
        pick.click();
        return JSON.stringify({{status: 'CLICKED', value: pick.value || ''}});
      }}
    """


async def _click_radio_by_label(adapter, group_name: str, option_label: str) -> bool:
    if not group_name or not option_label:
        return False
    js = _build_radio_click_js(group_name, option_label)
    raw = await adapter._call("evaluate_script", {"function": js})
    parsed = _safe_parse(raw)
    return isinstance(parsed, dict) and parsed.get("status") == "CLICKED"


async def _expand_radio_groups(adapter, payload: dict) -> dict:
    """Phase 2: iterate native radio groups, click each option, re-scan,
    record newly-visible elements with depends_on tags. Restore by
    clicking the first option after each group."""
    elements = payload.get("elements") or []
    radio_entries = [
        e for e in elements
        if e.get("kind") == "radio"
        and e.get("input_type") != "aria-radio"
        and not e.get("disabled")
        and len(e.get("options") or []) >= 2
        and e.get("xpath")
    ]
    if not radio_entries:
        return {"attempted": 0, "options_explored": 0, "new_elements": 0}

    print(f"  Phase 2: expanding {len(radio_entries)} radio group(s)...")

    initial_xpaths = {e.get("xpath") for e in elements if e.get("xpath")}
    new_elements: list[dict] = []
    explored = 0
    failed_groups = 0

    for radio_el in radio_entries:
        group_attr = _extract_name_attr_from_xpath(radio_el.get("xpath", ""))
        label_short = (radio_el.get("name") or "?")[:50]
        if not group_attr:
            print(f"    ✗ {label_short:<50} → could not derive @name attribute")
            failed_groups += 1
            continue

        options = list(radio_el.get("options") or [])
        group_new = 0
        for opt_label in options:
            # Force any leftover popup closed before EACH option toggle —
            # otherwise rescan picks up popup contents as "new conditional
            # elements" (false positives we hit in v1).
            await _close_all_popups(adapter)
            ok = await _click_radio_by_label(adapter, group_attr, opt_label)
            if not ok:
                continue
            explored += 1
            await asyncio.sleep(0.4)
            # And again before rescan — radio click itself can shift focus
            # and reopen state-bound popups.
            await _close_all_popups(adapter)

            raw = await adapter._call("evaluate_script", {"function": EXHAUSTIVE_SCAN_JS})
            rescan = _safe_parse(raw)
            if not isinstance(rescan, dict):
                continue

            # First pass — xpath-based dedup against initial scan.
            xpath_new = []
            for new_el in rescan.get("elements") or []:
                xp = new_el.get("xpath")
                if not xp or xp in initial_xpaths:
                    continue
                xpath_new.append(new_el)

            # Layer 2 filter — drop any candidate inside a popup container
            # (defense in case the close logic missed an open menu).
            kept, dropped = await _filter_popup_descendants(adapter, xpath_new)
            if dropped:
                print(f"      [filter] dropped {dropped} popup-internal candidate(s)")

            for new_el in kept:
                xp = new_el.get("xpath")
                new_el["depends_on"] = [
                    f"radio:{group_attr}",
                    f"option={opt_label}",
                ]
                initial_xpaths.add(xp)
                new_elements.append(new_el)
                group_new += 1

        # Restore: click first option (best-effort).
        if options:
            await _click_radio_by_label(adapter, group_attr, options[0])
            await asyncio.sleep(0.2)

        marker = "✓" if group_new else "·"
        print(f"    {marker} {label_short:<50} → {group_new} new element(s) across {len(options)} option(s)")

    if new_elements:
        print(f"    discovered {len(new_elements)} conditional element(s) total:")
        for el in new_elements[:8]:
            kind = el.get("kind", "?")
            nm = (el.get("name") or "")[:50]
            dep = el.get("depends_on") or []
            print(f"      + {kind:<12} {nm:<50}  ({', '.join(dep)})")
        if len(new_elements) > 8:
            print(f"      … +{len(new_elements) - 8} more")

    payload["elements"].extend(new_elements)
    return {
        "attempted": len(radio_entries),
        "options_explored": explored,
        "new_elements": len(new_elements),
        "failed_groups": failed_groups,
    }


async def _expand_empty_dropdowns(adapter, payload: dict, max_options: int = 5) -> dict:
    """Walk the elements list, expand each dropdown with empty options[].
    Mutates payload in place. Returns expansion summary."""
    elements = payload.get("elements") or []
    candidates = [
        el for el in elements
        if el.get("kind") == "dropdown"
        and not el.get("options")
        and el.get("xpath")
    ]
    if not candidates:
        return {"attempted": 0, "captured": 0, "failed": 0}

    print(f"  Expanding {len(candidates)} dropdown(s) to capture options...")
    captured = 0
    failed = 0
    for el in candidates:
        name = (el.get("name") or "?")[:50]
        try:
            opts = await _expand_dropdown(adapter, el["xpath"], max_options)
        except Exception as e:
            print(f"    ✗ {name:<50} → exception: {type(e).__name__}: {e}")
            failed += 1
            await _close_all_popups(adapter)
            continue
        # Always force a clean state after each dropdown — the in-JS Escape
        # often misses portal-mounted popups (e.g. Forgenite's combobox).
        await _close_all_popups(adapter)
        if opts:
            el["options"] = opts
            captured += 1
            print(f"    ✓ {name:<50} → {len(opts)} option(s): {', '.join(opts[:3])}{' …' if len(opts) > 3 else ''}")
        else:
            failed += 1
            print(f"    ✗ {name:<50} → no options captured")
    return {"attempted": len(candidates), "captured": captured, "failed": failed}


def _element_type(raw: str) -> ElementType:
    try:
        return ElementType(raw)
    except ValueError:
        return ElementType.OTHER


def _dedupe_id(screen: str, name: str, etype: str, seen: set[str], section: str = "") -> str:
    base = make_element_id(screen, name, etype, section=section)
    if base not in seen:
        seen.add(base)
        return base
    n = 2
    while True:
        eid = make_element_id(screen, f"{name} {n}", etype, section=section)
        if eid not in seen:
            seen.add(eid)
            return eid
        n += 1


def _build_kb(app: TargetApp, payload: dict, screen_name: str) -> KnowledgeBase:
    now = datetime.now().isoformat()
    seen: set[str] = set()
    l0: list[L0Element] = []
    l1: list[L1Element] = []
    l2: list[L2Element] = []

    raw_elements = payload.get("elements") or []
    for i, raw in enumerate(raw_elements):
        name = str(raw.get("name") or "").strip() or "(unlabeled)"
        etype = _element_type(str(raw.get("kind") or "other"))
        section = str(raw.get("section") or "").strip()
        eid = _dedupe_id(screen_name, name, etype.value, seen, section=section)

        behavior_bits = ["exhaustive_extract"]
        if raw.get("input_type"):
            behavior_bits.append(f"input_type={raw['input_type']}")
        if raw.get("disabled"):
            behavior_bits.append("disabled")
        if raw.get("readonly"):
            behavior_bits.append("readonly")
        if section:
            behavior_bits.append(f"section={section}")
        tier = int(raw.get("locator_tier") or 0)
        if tier:
            behavior_bits.append(f"locator_tier={tier}")
        if raw.get("disambiguation_failed"):
            behavior_bits.append("disambiguation_failed=true")
        if raw.get("scope_used"):
            behavior_bits.append(f"scope={raw['scope_used']}")

        l0.append(L0Element(
            element_id=eid,
            name=name,
            type=etype,
            required=bool(raw.get("required", False)),
            behavior="; ".join(behavior_bits),
            options=[str(o).strip() for o in (raw.get("options") or []) if str(o).strip()],
            interaction_order=i,
            default_value=str(raw.get("value") or ""),
            validation_rules=str(raw.get("validation_rules") or ""),
            screen_name=screen_name,
            accept=str(raw.get("accept") or ""),
            semantic_hint="",
            depends_on=[str(d) for d in (raw.get("depends_on") or []) if d],
        ))

        # Locators: XPath is primary now (tier-aware confidence), CSS
        # secondary, label as a last-ditch fallback for legacy callers.
        # Confidence maps from the JS-side tier (1 = uniquely matched on a
        # single strong attribute, 6 = absolute DOM path).
        tier_confidence = {1: 0.99, 2: 0.95, 3: 0.90, 4: 0.80, 5: 0.50, 6: 0.30}

        locators: list[Locator] = []
        xpath = str(raw.get("xpath") or "")
        if xpath:
            locators.append(Locator(
                strategy="xpath",
                value=xpath,
                confidence=tier_confidence.get(tier, 0.7),
            ))
        for fb in raw.get("xpath_fallbacks") or []:
            if fb and fb != xpath:
                locators.append(Locator(strategy="xpath", value=str(fb), confidence=0.6))
        css = str(raw.get("css") or "")
        if css:
            locators.append(Locator(strategy="css", value=css, confidence=0.85))
        for fb in raw.get("css_fallbacks") or []:
            if fb and fb != css:
                locators.append(Locator(strategy="css", value=str(fb), confidence=0.65))
        if name:
            locators.append(Locator(strategy="label", value=name, confidence=0.40))

        retry_strategy = "exhaustive_dom"
        if raw.get("disambiguation_failed"):
            retry_strategy = "exhaustive_dom_disambiguation_failed"

        l1.append(L1Element(
            element_id=eid,
            locators=locators,
            retry_strategy=retry_strategy,
            widget_type=str(raw.get("tag") or raw.get("kind") or ""),
            identifier=xpath or css,
            screen_name=screen_name,
        ))

        l2.append(L2Element(
            element_id=eid,
            change_log=[f"{now[:10]}: discovered by exhaustive_extract"],
            first_seen=now,
            last_seen=now,
        ))

    screen = ScreenKnowledge(
        screen_name=screen_name,
        screen_url=payload.get("page_url") or app.url or "",
        l0=l0,
        l1=l1,
        l2=l2,
    )
    return KnowledgeBase(app=app, screens=[screen], created_at=now, updated_at=now)


async def run_exhaustive_extract(
    url: str,
    app_name: str,
    *,
    screen_name: str = "",
    wait: bool = False,
) -> KnowledgeBase:
    app = TargetApp(platform=Platform.WEB, url=url, app_name=app_name)
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)
    try:
        if wait:
            print()
            print("=" * 60)
            print("  Browser open. Login + navigate to the page you want to extract.")
            print("  When the page is fully visible, press Enter to scan.")
            print("=" * 60)
            try:
                input("  >>> Press Enter... ")
            except EOFError:
                pass
        else:
            # SPA cold-start settle window. Same pattern as page_diagnostic.
            await asyncio.sleep(2.5)

        raw = await adapter._call("evaluate_script", {"function": EXHAUSTIVE_SCAN_JS})
        payload = _safe_parse(raw)
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"exhaustive scan returned unparseable output: {str(raw)[:500]}"
            )

        # Phase 1: dropdown expansion. Only fires for elements where
        # extract recorded kind=dropdown but options[] is empty (custom
        # comboboxes whose list only renders after click). Native <select>
        # already has options captured in the initial scan, so it's untouched.
        await _expand_empty_dropdowns(adapter, payload, max_options=5)

        # Phase 2: radio-group expansion. Toggles each native-radio option
        # to discover conditional fields revealed only under specific
        # selections. New elements are appended to payload with depends_on
        # tags so Plan can scope test cases by selection state.
        await _expand_radio_groups(adapter, payload)

        target_screen = (
            screen_name
            or payload.get("page_title")
            or (payload.get("page_url") or "").rsplit("/", 1)[-1]
            or "default"
        )
        kb = _build_kb(app, payload, target_screen)

        # Merge into existing KB if present (same pattern as form_extract):
        # we replace the matching screen's elements, leave other screens
        # untouched.
        store = KnowledgeStore()
        existing = store.load(app)
        if existing and existing.screens:
            others = [s for s in existing.screens if s.screen_name != target_screen]
            kb_screens = others + kb.screens
            kb = KnowledgeBase(
                app=app,
                screens=kb_screens,
                created_at=existing.created_at,
                updated_at=datetime.now().isoformat(),
            )

        path = store.save(kb)
        return kb, path, payload
    finally:
        await adapter.close()


def _print_summary(kb: KnowledgeBase, path: Path, payload: dict, screen_name: str) -> None:
    target = next(
        (s for s in kb.screens if s.screen_name == screen_name),
        kb.screens[-1] if kb.screens else None,
    )
    if not target:
        print("  (no screen captured)")
        return

    print(f"\nKB saved: {path}")
    print(f"Screen:   {target.screen_name}")
    print(f"URL:      {target.screen_url}")
    print(f"Total interactives captured: {len(target.l0)}")

    by_type: dict[str, int] = {}
    for el in target.l0:
        by_type[el.type.value] = by_type.get(el.type.value, 0) + 1
    print("\nBreakdown by type:")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {t:<14} {n}")

    print("\nFirst 8 elements (preview):")
    for el in target.l0[:8]:
        sec = f"  [{el.behavior.split('section=')[-1].split(';')[0].strip()}]" if "section=" in el.behavior else ""
        print(f"  {el.type.value:<14} {el.name[:55]:<55}{sec}")
    if len(target.l0) > 8:
        print(f"  ... +{len(target.l0) - 8} more")


async def _amain() -> int:
    ap = argparse.ArgumentParser(
        description="Exhaustive DOM extract — every interactable element captured to KB.",
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", "-a", required=True, help="App name (becomes KB filename)")
    ap.add_argument("--screen-name", "-s", default="", help="Override screen name in KB")
    ap.add_argument(
        "--wait", action="store_true",
        help="Pause after launch so you can login / navigate before scan.",
    )
    args = ap.parse_args()

    kb, path, payload = await run_exhaustive_extract(
        args.url,
        args.app_name,
        screen_name=args.screen_name,
        wait=args.wait,
    )
    target_screen = args.screen_name or payload.get("page_title") or "default"
    _print_summary(kb, path, payload, target_screen)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
