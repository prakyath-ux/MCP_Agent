# qa/orchestrators/form_extract.py — Hybrid form extractor.
#
# Python drives the flow (deterministic where it can be) + LLM extracts
# dropdown options from snapshots (robust across varying DOM implementations).
#
# This replaces the earlier "pure JS" attempt which failed on TECU because
# MUI-style dropdowns render options in a structure my JS selectors didn't
# match. The a11y snapshot + narrow LLM classification handles that
# variability for ~$0.03-0.05 per page.
#

# Usage:
#   python -m qa.orchestrators.form_extract <url> --app-name TECU --wait
#
# Flow per page:
#   1. JS enumerates all standard form elements (inputs, selects, radios,
#      checkboxes, file uploads)
#   2. Snapshot → find custom dropdown triggers (buttons w/ "Select X"
#      or label-echo patterns)
#   3. For each trigger: MCP click → snapshot → LLM sub-task extracts
#      options → Escape to close
#   4. Build ScreenKnowledge, merge into KB

import argparse
import asyncio
import json
import re
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from qa.adapters import make_adapter
from qa.config import Defaults
from qa.engine.budget import BudgetTracker
from qa.engine.guardrails import (
    GuardrailContext, GuardrailExit,
    per_dropdown_scope, per_page_scope, per_verify_scope,
)
from qa.knowledge.file_resolver import discover_test_files
from qa.knowledge.store import KnowledgeStore
from qa.models import ExploreInput, KnowledgeBase, Platform, TargetApp
from qa.models.common import ElementType, make_element_id
from qa.models.knowledge import L0Element, L1Element, Locator, ScreenKnowledge
from qa.orchestrators.base import RunContext
from qa.orchestrators.gated_multi_section import (
    GatedMultiSectionFlow,
    SectionFailed,
)
from qa.orchestrators.llm_subtask import llm_classify
from qa.orchestrators.sub_prompts import (
    EXTRACT_DROPDOWN_OPTIONS_PROMPT,
    EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
)
from qa.orchestrators.verify import verify_list_cascaded
from qa.tools.web_tools import _safe_parse


def _narrate(msg: str) -> None:
    """User-facing narration, emitted alongside diagnostic [form] logs.
    Prefix makes these visually distinct in Streamlit chat output so a
    viewer reads a natural account of what the agent is doing, while
    the [form] lines remain for debugging."""
    print(f"🤖 {msg}", flush=True)


def _xpath_string_literal(s: str) -> str:
    """Safely embed an arbitrary string as an XPath literal.

    XPath has no string-escape syntax. If the string contains only one
    kind of quote, wrap with the other. If both, build with concat().

    Examples:
        hello world      → 'hello world'
        it's great       → "it's great"
        mix ' and " here → concat('mix ', "'", ' and " here')
    """
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    pieces = [f"'{p}'" for p in parts]
    return "concat(" + ", \"'\", ".join(pieces) + ")"


def _describe_type_counts(counts: dict[str, int]) -> str:
    """Format a type-count dict as a human sentence, e.g.
    '7 text inputs, 4 dropdowns, 1 file upload'."""
    labels = {
        "text_input": "text input", "dropdown": "dropdown",
        "radio": "radio group", "checkbox": "checkbox",
        "file_upload": "file upload", "date": "date picker",
        "email": "email field", "phone": "phone field",
        "combobox": "combobox", "button": "button",
    }
    parts: list[str] = []
    for t, c in sorted(counts.items(), key=lambda x: -x[1]):
        singular = labels.get(t, t)
        name = singular if c == 1 else (singular + "es" if singular.endswith("x") else singular + "s")
        parts.append(f"{c} {name}")
    return ", ".join(parts) if parts else "none"


def _js_section_for_text(button_text: str) -> str:
    """Build a JS snippet that returns the nearest preceding section
    heading for a button with the given visible text. Used to enrich
    custom dropdown triggers (which we find via snapshot, not DOM
    enumeration) with the same section context we attach to standard
    form elements. Returns {"found": bool, "section": str}."""
    text_js = json.dumps(button_text)
    return (
        "() => {"
        f"  const t = {text_js}.trim();"
        "  if (!t) return JSON.stringify({found: false, section: ''});"
        "  const btns = [...document.querySelectorAll("
        "    'button, [role=\"combobox\"], [role=\"button\"]'"
        "  )];"
        "  const target = btns.find(b => (b.textContent || '').trim() === t);"
        "  if (!target) return JSON.stringify({found: false, section: ''});"
        "  const HEADING_SEL = 'h1, h2, h3, h4, h5, h6, fieldset > legend, [role=\"heading\"]';"
        "  const headings = [...document.querySelectorAll(HEADING_SEL)].filter(h => {"
        "    const hv = (h.textContent || '').trim();"
        "    return hv.length > 0 && hv.length < 120;"
        "  });"
        "  let best = '';"
        "  for (const h of headings) {"
        "    const pos = h.compareDocumentPosition(target);"
        "    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) {"
        "      best = (h.textContent || '').trim();"
        "    } else {"
        "      break;"
        "    }"
        "  }"
        "  return JSON.stringify({found: true, section: best.slice(0, 80)});"
        "}"
    )


def _js_is_disabled_for(button_text: str) -> str:
    """Build a JS snippet that checks whether a button with the given
    visible text is disabled/dependent. Used before clicking to avoid
    contaminating the next snapshot with a previous dropdown's options
    when the current trigger is actually disabled."""
    text_js = json.dumps(button_text)
    return (
        "() => {"
        f"  const t = {text_js}.trim();"
        "  if (!t) return JSON.stringify({found: false});"
        "  const btns = [...document.querySelectorAll("
        "    'button, [role=\"combobox\"], [role=\"button\"]'"
        "  )];"
        "  const target = btns.find(b => (b.textContent || '').trim() === t);"
        "  if (!target) return JSON.stringify({found: false});"
        "  const style = getComputedStyle(target);"
        "  const disabled = target.disabled"
        "    || target.getAttribute('aria-disabled') === 'true'"
        "    || target.hasAttribute('data-disabled')"
        "    || style.pointerEvents === 'none'"
        "    || parseFloat(style.opacity) < 0.4;"
        "  return JSON.stringify({found: true, disabled: disabled});"
        "}"
    )


# ── Wall 1.2: dropdown_data collision-safe insert ───────────────────
#
# Repeating sub-forms (e.g. TECU's Beneficiary 1 / Beneficiary 2 sharing
# field names like "First Name" or "Is Beneficiary a member?") produced
# silent data loss when two triggers hit the same label: the second
# write to `dropdown_data[label]` overwrote the first, erasing options,
# locators, and DOM position for the earlier dropdown.
#
# _dd_put() chooses a unique dict key on collision (appends the section,
# then an index) while persisting the original label + section inside
# the value dict. Downstream readers always use `info["label"]` +
# `info["section"]`, never the raw dict key — so this change is transparent
# to them.

def _dd_put(
    dropdown_data: dict,
    label: str,
    section: str,
    entry: dict,
) -> str:
    """Insert entry into dropdown_data under a collision-free key.
    Stores the true label + section INSIDE the entry so downstream
    code reads them from there, not from the dict key. Returns the
    key used (for callers that need to update the same slot later)."""
    entry = dict(entry)  # shallow-copy so caller's dict isn't aliased
    entry["label"] = label
    entry["section"] = section

    if label not in dropdown_data:
        dropdown_data[label] = entry
        return label

    # Collision — try section-qualified key first, then numeric index.
    if section:
        key = f"{label}|{section}"
        if key not in dropdown_data:
            dropdown_data[key] = entry
            return key
    idx = 2
    while True:
        key = f"{label}#{idx}"
        if key not in dropdown_data:
            dropdown_data[key] = entry
            return key
        idx += 1


def _dd_label(info: dict) -> str:
    """Read the true label from a dropdown_data value dict. Falls back
    to the dict key if the entry predates Wall 1.2 (unlikely but
    defensive)."""
    if isinstance(info, dict):
        return str(info.get("label") or "")
    return ""


# ── Wall 1.6: reveal-trigger detection (conditional UI two-pass) ────
#
# TECU and similar wizards hide sub-forms behind "Would you like to add
# Beneficiary?" Yes/No radios. Without flipping the radio to Yes, the
# sub-form never renders and we never extract its fields. These are the
# regex heuristics used to identify candidate reveal radios from the JS
# enumerate output (label + options). Regex-first keeps this deterministic
# and free; LLM fallback is deliberately deferred.

_REVEAL_TRIGGER_PATTERNS = re.compile(
    r"(would you like|do you want|do you wish|do you have|have you|"
    r"interested in|\badd\b.*\?|include\b.*\?)",
    re.IGNORECASE,
)

_YES_OPTION_PATTERN = re.compile(r"^(yes|y)\b", re.IGNORECASE)


def _detect_reveal_radios(js_elements: list[dict]) -> list[dict]:
    """Scan JS-enumerated elements for radio groups whose label matches a
    "reveal this section?" pattern AND whose options include a Yes-like
    value. Returns candidates in original DOM order."""
    out: list[dict] = []
    for el in js_elements:
        if el.get("type") != "radio":
            continue
        label = str(el.get("label") or "").strip()
        if not label or not _REVEAL_TRIGGER_PATTERNS.search(label):
            continue
        options = el.get("options") or []
        has_yes = any(
            _YES_OPTION_PATTERN.match(str(o).strip()) for o in options
        )
        if not has_yes:
            continue
        out.append(el)
    return out


def _js_click_radio_yes_for(group_name: str) -> str:
    """JS that clicks the Yes option within a named radio group. We click
    the enclosing <label> rather than the <input> directly because React
    / MUI forms usually bind onChange to label clicks (dispatches pointer
    events) — clicking the bare input can skip state updates. Returns
    {"clicked": bool, "was_selected": bool, "found": bool}."""
    name_js = json.dumps(group_name)
    return (
        "() => {"
        f"  const name = {name_js};"
        "  if (!name) return JSON.stringify({found: false, clicked: false, was_selected: false});"
        "  const radios = [...document.querySelectorAll("
        "    'input[type=radio][name=\"' + name + '\"]'"
        "  )];"
        "  let target = null;"
        "  for (const r of radios) {"
        "    const lbl = r.closest('label')"
        "      || (r.id ? document.querySelector('label[for=\"' + r.id + '\"]') : null);"
        "    const t = (lbl ? lbl.textContent : (r.value || '')).trim().toLowerCase();"
        "    if (/^y(es)?\\b/.test(t)) { target = r; break; }"
        "  }"
        "  if (!target) return JSON.stringify({found: false, clicked: false, was_selected: false});"
        "  const wasSelected = !!target.checked;"
        "  if (wasSelected) {"
        "    return JSON.stringify({found: true, clicked: false, was_selected: true});"
        "  }"
        "  const lbl = target.closest('label')"
        "    || (target.id ? document.querySelector('label[for=\"' + target.id + '\"]') : null);"
        "  if (lbl) lbl.click(); else target.click();"
        "  return JSON.stringify({found: true, clicked: true, was_selected: false});"
        "}"
    )


# ── JS: close any open MUI-style popup ──────────────────────────────

_JS_CLOSE_POPUP = r"""() => {
  // Dispatch Escape at the document level — MUI listens for keydown
  // on document, not body. This is more reliable than press_key MCP.
  document.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
    bubbles: true, cancelable: true,
  }));
  document.dispatchEvent(new KeyboardEvent('keyup', {
    key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
    bubbles: true, cancelable: true,
  }));

  // Click the MUI backdrop if it's rendered (that's how MUI popups close
  // when you click outside).
  const backdrop = document.querySelector('.MuiBackdrop-root, .MuiPopover-root .MuiBackdrop-invisible');
  if (backdrop) {
    backdrop.click();
    return 'clicked_backdrop';
  }

  // Last resort: click somewhere neutral (top-left of viewport).
  const neutral = document.elementFromPoint(5, 5);
  if (neutral && neutral !== document.documentElement) neutral.click();
  return 'dispatched_escape';
}"""


# ── JS: enumerate all standard form elements ────────────────────────

_JS_ENUMERATE = r"""() => {
  const results = [];
  const seen = new Set();

  // Collect all section-header candidates in DOM order. We use these to
  // label each element with its nearest preceding section, so that forms
  // with repeating sub-sections (Nominee / Joint Partner / Beneficiary 1 /
  // Beneficiary 2 all containing "First Name") produce distinct KB ids.
  const HEADING_SEL = 'h1, h2, h3, h4, h5, h6, fieldset > legend, [role="heading"]';
  const headings = [...document.querySelectorAll(HEADING_SEL)].filter(h => {
    const t = (h.textContent || '').trim();
    return t.length > 0 && t.length < 120;
  });

  function findSection(el) {
    // The nearest heading that precedes `el` in DOM order IS our section.
    // compareDocumentPosition: DOCUMENT_POSITION_FOLLOWING (0x04) set on
    // `el` relative to `heading` means heading comes before el.
    let best = '';
    for (const h of headings) {
      const pos = h.compareDocumentPosition(el);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) {
        // heading h precedes el → candidate, keep going to find the nearest
        best = (h.textContent || '').trim();
      } else {
        // heading is at or after el → stop (headings are in DOM order)
        break;
      }
    }
    return best.slice(0, 80);
  }

  function findLabel(el) {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) {
      const lbl = document.querySelector('label[for="' + el.id + '"]');
      if (lbl) return lbl.textContent.trim();
    }
    const parentLabel = el.closest('label');
    if (parentLabel) {
      const clone = parentLabel.cloneNode(true);
      clone.querySelectorAll('input,select,textarea,button').forEach(c => c.remove());
      const t = clone.textContent.trim();
      if (t) return t;
    }
    const fc = el.closest('.MuiFormControl-root, .form-group, .field-wrapper, .form-control');
    if (fc) {
      const lbl = fc.querySelector('label, .MuiFormLabel-root, .MuiInputLabel-root, legend');
      if (lbl) return lbl.textContent.trim();
    }
    return el.placeholder || el.name || el.id || '';
  }

  function isRequired(el, label) {
    return el.required
      || el.getAttribute('aria-required') === 'true'
      || /\*\s*$/.test(label)
      || el.closest('.MuiFormControl-root')?.querySelector('.MuiFormLabel-asterisk') != null;
  }

  // Text inputs + textareas
  document.querySelectorAll(
    'input:not([type=file]):not([type=radio]):not([type=checkbox]):not([type=hidden]):not([type=submit]):not([type=button]), textarea'
  ).forEach(el => {
    if (el.offsetParent === null && !el.closest('[role="dialog"]')) return;
    const label = findLabel(el);
    const section = findSection(el);
    // Loosened dedup: prefer id/name/placeholder, fall back to section+label
    // for apps (Angular SPAs, some Vue) whose inputs lack all three but have
    // a resolvable visible label. Section prefix preserves Wall 1.2 — same
    // label across sections stays separate.
    let key = el.id || el.name || el.placeholder || '';
    if (!key && label) key = 'label:' + section + ':' + label;
    if (!key || seen.has(key)) return;
    seen.add(key);
    // Combobox ancestor detection: an <input> inside a combobox / autocomplete
    // wrapper is not a plain text field — it backs a custom dropdown widget.
    // Re-type as 'dropdown' with empty options so form_extract's open-and-read
    // path populates the option list via snapshot + LLM. Covers Odoo Owl.js
    // (.o_field_many2one), MUI Autocomplete, Ant Design Select, Select2.
    const comboboxAncestor = el.closest(
      '[role="combobox"], [aria-autocomplete], ' +
      '.o_field_many2one, .o_field_selection, ' +
      '.MuiAutocomplete-root, ' +
      '.ant-select, ' +
      '.select2-container'
    );
    if (comboboxAncestor) {
      results.push({
        label: label.replace(/\s*\*\s*$/, '').trim(),
        type: 'dropdown',
        required: isRequired(el, label),
        id: el.id || '',
        name: el.name || '',
        options: [],
        native: false,
        section: section,
        dom_top: el.getBoundingClientRect().top,
      });
      return;
    }
    const typeMap = {date: 'date', email: 'email', tel: 'phone', number: 'text_input'};
    results.push({
      label: label.replace(/\s*\*\s*$/, '').trim(),
      type: typeMap[el.type] || 'text_input',
      required: isRequired(el, label),
      id: el.id || '',
      name: el.name || '',
      placeholder: el.placeholder || '',
      value: el.value || '',
      section: section,
      dom_top: el.getBoundingClientRect().top,
    });
  });

  // Native <select>
  document.querySelectorAll('select').forEach(el => {
    if (el.offsetParent === null) return;
    const label = findLabel(el);
    const section = findSection(el);
    let key = el.id || el.name || '';
    if (!key && label) key = 'select:' + section + ':' + label;
    if (!key || seen.has(key)) return;
    seen.add(key);
    const opts = [...el.options]
      .map(o => o.textContent.trim())
      .filter(o => o && !/^(Select|Choose|--|Please)/i.test(o));
    results.push({
      label: label.replace(/\s*\*\s*$/, '').trim(),
      type: 'dropdown',
      required: isRequired(el, label),
      id: el.id || '',
      name: el.name || '',
      options: opts,
      native: true,
      dom_top: el.getBoundingClientRect().top,
      section: section,
    });
  });

  // File inputs
  document.querySelectorAll('input[type=file]').forEach(el => {
    const label = findLabel(el);
    results.push({
      label: label.replace(/\s*\*\s*$/, '').trim() || 'File Upload',
      type: 'file_upload',
      required: isRequired(el, label),
      id: el.id || '',
      accept: el.accept || '',
      section: findSection(el),
      dom_top: el.getBoundingClientRect().top,
    });
  });

  // Radio groups (deduplicate by name)
  const radioGroups = {};
  document.querySelectorAll('input[type=radio]').forEach(el => {
    const grp = el.name || el.id || '';
    if (!grp) return;
    if (!radioGroups[grp]) {
      const fc = el.closest('fieldset, [role="radiogroup"], .MuiFormControl-root, .form-group');
      let groupLabel = '';
      if (fc) {
        // Prefer legend/formlabel — NOT the individual option's label
        const lbl = fc.querySelector('legend, .MuiFormLabel-root, .MuiInputLabel-root');
        if (lbl) groupLabel = lbl.textContent.trim();
      }
      if (!groupLabel) {
        // Walk up looking for a sibling StaticText-like label
        let node = el.parentElement;
        while (node && !groupLabel) {
          const prev = node.previousElementSibling;
          if (prev && !prev.querySelector('input, button')) {
            const t = prev.textContent.trim();
            if (t && t.length < 80) { groupLabel = t; break; }
          }
          node = node.parentElement;
          if (node && node.tagName === 'FORM') break;
        }
      }
      radioGroups[grp] = {
        label: groupLabel.replace(/\s*\*\s*$/, '').trim(),
        type: 'radio',
        required: isRequired(el, groupLabel),
        name: grp,
        options: [],
        section: findSection(el),
        dom_top: el.getBoundingClientRect().top,
      };
    }
    const optLabel = el.closest('label')?.textContent?.trim()
      || el.value || el.id || '';
    if (optLabel && !radioGroups[grp].options.includes(optLabel)) {
      radioGroups[grp].options.push(optLabel);
    }
  });
  Object.values(radioGroups).forEach(g => {
    // Drop radio groups that never picked up a real label (avoids entries
    // named just "Yes" or "No" in the KB)
    if (!g.label || /^(yes|no|ok)$/i.test(g.label.trim())) return;
    results.push(g);
  });

  // Checkboxes
  document.querySelectorAll('input[type=checkbox]').forEach(el => {
    if (el.offsetParent === null) return;
    const label = findLabel(el);
    const key = el.id || el.name || label;
    if (seen.has(key)) return;
    seen.add(key);
    results.push({
      label: label.replace(/\s*\*\s*$/, '').trim(),
      type: 'checkbox',
      required: isRequired(el, label),
      id: el.id || '',
      section: findSection(el),
      dom_top: el.getBoundingClientRect().top,
    });
  });

  // Buttons — captured for KB completeness so reports reflect the full
  // page. Plan decides per-button whether to generate a click test
  // (TAP_VERIFY for action buttons like 'Find Member') or skip
  // (navigation buttons like 'Save & Continue', 'Submit', 'Back').
  // We only capture visible buttons with non-empty short text.
  document.querySelectorAll(
    'button, input[type="submit"], input[type="button"]'
  ).forEach(el => {
    if (el.offsetParent === null) return;
    const text = (el.textContent || el.value || '').trim();
    if (!text || text.length > 80) return;
    // Dedupe: same button text in the same section only counted once.
    const section = findSection(el);
    const key = 'btn:' + section + ':' + text;
    if (seen.has(key)) return;
    seen.add(key);
    results.push({
      label: text,
      type: 'button',
      required: false,
      id: el.id || '',
      name: el.name || '',
      section: section,
      dom_top: el.getBoundingClientRect().top,
    });
  });

  return JSON.stringify(results);
}"""


# Walls E1 + E2 — for each custom-dropdown trigger discovered via the
# a11y snapshot, ask the DOM for a stable locator pair (CSS + XPath) +
# vertical position. We need the locator so ExecuteOrchestrator.
# _select_option can find the trigger (Wall 1.1), and the dom_top so
# _build_screen can sort L0 by true top-to-bottom order.
#
# Chrome DevTools MCP `evaluate_script` expects a ZERO-ARG arrow
# function, so we can't pass `(label) => ...` and call it ourselves —
# we template the label literal into a zero-arg closure via
# `_js_locate_trigger_for(label)`.
#
# Return JSON shape on success:
#   {
#     "css":      "label[for='...'] ~ * button[data-testid='...']",
#     "xpath":    "//label[@for='...']/following-sibling::*//button[@data-testid='...']",
#     "strategy": "tecu_label_for" | "mui_formcontrol" | "generic_text_match",
#     "dom_top":  1234.5
#   }
# Return `null` if no trigger is resolvable.
#
# Strategies in order — first match wins:
#   1. TECU / HTML5 — <label for="X"> anchor, sibling button. Works for
#      Tailwind apps, custom components, anything with semantic HTML.
#      Selector uses the label's `for` attribute → unique & stable.
#   2. MUI — <label> wrapped by .MuiFormControl-root, combobox inside.
#      Works for Material UI apps. Selector uses id / aria-labelledby /
#      role-based nth-of-type.
#   4. <p>-tag / heading label (Tailwind cards) — no <label> element,
#      label text lives in a <p>/<h*>/<span>. XPath-only result with
#      `//p[text()='X']/following::button[@data-testid='Y'][1]` style
#      (version_2 predicates). CSS empty — text match unsupported in CSS.
#      Execute falls back to document.evaluate() when CSS is empty.
#   3. Generic — text-content match on any button/select/combobox.
#      Last resort for apps that have no associated <label>.
_JS_LOCATE_TRIGGER_BODY = r"""
  if (!TARGET_LABEL) return null;
  const needle = TARGET_LABEL
    .replace(/\s+/g, ' ').trim().toLowerCase();
  if (!needle) return null;

  const isVisible = (el) => el && el.offsetParent !== null;

  // Normalize the visible text of a label — strip a trailing '*',
  // collapse whitespace runs (handles "Employment \n   Status"), lower-case.
  function labelText(lbl) {
    return lbl.textContent
      .replace(/\s*\*\s*$/, '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase();
  }

  // Case-preserved version for embedding in XPath literals. XPath's
  // normalize-space() is case-sensitive and does NOT strip a trailing
  // '*', so we preserve case and use `starts-with` in the predicate
  // (the '*' stays in the actual DOM text).
  function labelTextCased(lbl) {
    return lbl.textContent
      .replace(/\s*\*\s*$/, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  // P1 — match priority. Exact string match is always preferred over
  // startsWith / endsWith / includes. Callers iterate twice: first pass
  // exact only, second pass (fallback) partial. Without this, "Country"
  // silently binds to the "Country of Birth" label that appears earlier
  // in DOM order because "country of birth".startsWith("country").
  function isExactMatch(lbl) {
    return labelText(lbl) === needle;
  }
  function isPartialMatch(lbl) {
    const t = labelText(lbl);
    if (!t) return false;
    return t.startsWith(needle) || needle.startsWith(t);
  }
  function matchLabel(lbl) {
    return isExactMatch(lbl) || isPartialMatch(lbl);
  }

  // P2 — uniqueness gate. A generated CSS selector is only usable if
  // document.querySelector(css) would land on exactly the element we
  // targeted. Selectors like `[data-testid="dropdown-button"]` match
  // many elements and would silently hit the first one at Execute time.
  // Reject non-unique selectors so the resolver either falls through to
  // the next strategy or returns null cleanly.
  function isUniqueFor(css, target) {
    if (!css || !target) return false;
    try {
      const hits = document.querySelectorAll(css);
      return hits.length === 1 && hits[0] === target;
    } catch (e) {
      return false;
    }
  }

  function packResult(css, xpath, anchorEl, strategy) {
    // Allow xpath-only results. Some label patterns (e.g. <p>-tag labels
    // on Tailwind apps) have no stable CSS anchor since CSS can't match
    // text content — we ship XPath as the primary locator and leave CSS
    // empty; Execute falls back to document.evaluate for those.
    if ((!css && !xpath) || !anchorEl) return null;
    const r = anchorEl.getBoundingClientRect();
    return { css: css || '', xpath: xpath || '', strategy, dom_top: r.top };
  }

  // Verify an XPath resolves to exactly the target element. Same role as
  // isUniqueFor() for CSS but uses document.evaluate for XPath.
  function isXPathUniqueFor(xpath, target) {
    if (!xpath || !target) return false;
    try {
      const res = document.evaluate(
        xpath, document, null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
      );
      if (res.snapshotLength !== 1) return false;
      return res.snapshotItem(0) === target;
    } catch (e) {
      return false;
    }
  }

  // Escape a value for safe embedding in a double-quoted CSS attribute
  // selector or XPath attribute literal.
  function escAttr(v) {
    return String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }
  function escXpath(v) {
    // XPath 1.0 has no escape — if a literal contains both ' and ", we'd
    // need concat(). Our use case (DOM id/testid strings) almost never
    // contains both; if it does, fall back to escaping single quotes by
    // choosing a different delimiter.
    const s = String(v);
    if (s.indexOf("'") === -1) return `'${s}'`;
    if (s.indexOf('"') === -1) return `"${s}"`;
    // Both quotes present — break into concat pieces. Rare.
    const parts = s.split("'").map((p, i, a) => {
      const lit = `'${p}'`;
      return i < a.length - 1 ? lit + `, "'", ` : lit;
    });
    return 'concat(' + parts.join('') + ')';
  }

  // ── Strategy 1: TECU / HTML5 — label[for=X] as anchor ──────────
  //
  // Structure TECU and most Tailwind-style apps use:
  //   <div>
  //     <label for="fieldKey">Employment Status<span>*</span></label>
  //     <div class="relative ..."> <button data-testid="dropdown-button">
  //   </div>
  //
  // The label's `for` attribute is the only author-supplied identifier
  // that's guaranteed unique per field. We use it as the CSS/XPath anchor
  // and walk forward to the button/select that lives alongside it.
  function tryLabelForStrategy(predicate) {
    const labels = [...document.querySelectorAll('label')].filter(isVisible);
    for (const lbl of labels) {
      if (!predicate(lbl)) continue;
      const forAttr = lbl.getAttribute('for');
      if (!forAttr) continue;

      // Walk forward from the label to find the actual trigger element.
      let tr = null;

      // Case A: <label for="id"> pointing at a real <select> / <input>.
      const directTarget = document.getElementById(forAttr);
      if (isVisible(directTarget)) {
        const tag = directTarget.tagName;
        if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA'
            || tag === 'BUTTON') {
          tr = directTarget;
        }
      }

      // Case B: custom dropdown — button lives in a sibling div.
      if (!tr) {
        for (let s = lbl.nextElementSibling; s && !tr; s = s.nextElementSibling) {
          tr = s.querySelector(
            'button, [role=combobox], [role=button], select, textarea, input:not([type=hidden])'
          ) || (s.matches('button, [role=combobox], select') ? s : null);
          if (tr && !isVisible(tr)) tr = null;
        }
      }

      // Case C: last resort — look within the label's parent.
      if (!tr && lbl.parentElement) {
        tr = lbl.parentElement.querySelector(
          'button, [role=combobox], [role=button], select'
        );
        if (tr && !isVisible(tr)) tr = null;
      }

      if (!isVisible(tr)) continue;

      // Build the CSS + XPath using the `for` attribute as anchor.
      const forCss = escAttr(forAttr);
      const forXp  = escXpath(forAttr);
      const tagLower = (tr.tagName || '').toLowerCase();
      const dtid = tr.getAttribute('data-testid');
      let css = '';
      let xpath = '';

      if (tr === directTarget) {
        css = `#${CSS.escape(forAttr)}`;
        xpath = `//*[@id=${forXp}]`;
      } else if (dtid) {
        const dtidCss = escAttr(dtid);
        const dtidXp  = escXpath(dtid);
        css = `label[for="${forCss}"] ~ * ${tagLower}[data-testid="${dtidCss}"]`;
        xpath = `//label[@for=${forXp}]/following-sibling::*`
              + `//${tagLower}[@data-testid=${dtidXp}]`;
      } else {
        css = `label[for="${forCss}"] ~ * ${tagLower}`;
        xpath = `//label[@for=${forXp}]/following-sibling::*//${tagLower}`;
      }

      // P2 — if the generated CSS isn't unique, treat this label as a
      // non-match and keep scanning. (Should be rare for Strategy 1
      // since the `for` attribute is author-unique, but defensive.)
      if (!isUniqueFor(css, tr)) continue;

      const result = packResult(css, xpath, tr, 'tecu_label_for');
      if (result) return JSON.stringify(result);
    }
    return null;
  }

  // P1 — exact match pass first, partial only if no exact match wins.
  {
    const exact = tryLabelForStrategy(isExactMatch);
    if (exact) return exact;
    const partial = tryLabelForStrategy(isPartialMatch);
    if (partial) return partial;
  }

  // ── Strategy 2: MUI — .MuiFormControl-root container ───────────
  //
  // Structure:
  //   <div class="MuiFormControl-root">
  //     <label class="MuiFormLabel-root MuiInputLabel-root">Name</label>
  //     <div>...</div>
  //     <div role="combobox" aria-labelledby="..." />
  //   </div>
  //
  // Selector: prefer id → aria-labelledby → role-based nth-of-type.
  function tryMuiStrategy(predicate) {
    const LABEL_SEL = 'label, legend, .MuiFormLabel-root, .MuiInputLabel-root';
    const labels = [...document.querySelectorAll(LABEL_SEL)].filter(isVisible);
    for (const lbl of labels) {
      if (!predicate(lbl)) continue;
      const fc = lbl.closest(
        '.MuiFormControl-root, .form-group, .field-wrapper, fieldset'
      );
      if (!fc) continue;
      const tr = fc.querySelector('[role=combobox], [role=button], button, select');
      if (!isVisible(tr)) continue;

      let css = '';
      let xpath = '';

      if (tr.id) {
        css = '#' + CSS.escape(tr.id);
        xpath = `//*[@id=${escXpath(tr.id)}]`;
      } else {
        const labelledby = tr.getAttribute('aria-labelledby');
        if (labelledby) {
          const ab = escAttr(labelledby);
          css = `[aria-labelledby="${ab}"]`;
          xpath = `//*[@aria-labelledby=${escXpath(labelledby)}]`;
        } else if (tr.getAttribute('role')) {
          const role = tr.getAttribute('role');
          const peers = [...document.querySelectorAll(`[role="${role}"]`)]
            .filter(isVisible);
          const idx = peers.indexOf(tr);
          if (idx >= 0) {
            css = `[role="${role}"]:nth-of-type(${idx + 1})`;
            xpath = `(//*[@role=${escXpath(role)}])[${idx + 1}]`;
          }
        }
      }

      if (!isUniqueFor(css, tr)) continue;

      const result = packResult(css, xpath, tr, 'mui_formcontrol');
      if (result) return JSON.stringify(result);
    }
    return null;
  }

  // P1 — exact first, partial fallback.
  {
    const exact = tryMuiStrategy(isExactMatch);
    if (exact) return exact;
    const partial = tryMuiStrategy(isPartialMatch);
    if (partial) return partial;
  }

  // ── Strategy 4: <p>/heading-tag label pattern (Tailwind cards) ─
  //
  // Apps like TECU render some fields inside a "card" with the label as
  // a <p>/<h*>/<span> tag instead of <label for="...">. Strategy 1 skips
  // these (no <label>). Strategy 2 skips these (no .MuiFormControl-root).
  // Strategy 3 can't match by label text because the trigger's inner
  // span sometimes has different text ("Communication Method" while the
  // field label is "Preferred Method of communication").
  //
  // We anchor on the label tag's visible text and produce a version_2-
  // style XPath:
  //   //p[normalize-space(.)='<label>']/following::button[
  //     @data-testid='dropdown-button'
  //   ][1]
  // Uses 2 predicates per version_2's 1-3 attr rule: the label's exact
  // text + the trigger's testid. CSS is left empty for these — CSS can't
  // express "text content matches" so there's no stable CSS anchor.
  // Execute falls back to document.evaluate() when CSS is empty.
  function tryPTagLabelStrategy(predicate) {
    const LABEL_TAG_SEL = 'p, span, legend, h1, h2, h3, h4, h5, h6';
    const candidates = [...document.querySelectorAll(LABEL_TAG_SEL)]
      .filter(isVisible)
      .filter(el => {
        // Skip label-wrappers with nested form controls; we want
        // standalone text labels, not wrappers like <span><input/></span>.
        if (el.querySelector('input, button, select, textarea')) return false;
        const t = labelText(el);
        return t.length > 0 && t.length < 200;
      });

    for (const lbl of candidates) {
      if (!predicate(lbl)) continue;

      // Walk forward/upward to find the trigger that belongs to this
      // label — prefer one that shares a common ancestor within 4 hops.
      let tr = null;
      let container = lbl.parentElement;
      for (let hop = 0; container && hop < 6 && !tr; hop++) {
        tr = container.querySelector(
          'button[data-testid="dropdown-button"], '
          + 'button, [role=combobox], [role=button], select'
        );
        if (tr && !isVisible(tr)) tr = null;
        if (!tr) container = container.parentElement;
      }
      if (!isVisible(tr)) continue;

      const lblTag = (lbl.tagName || 'p').toLowerCase();
      const tagLower = (tr.tagName || 'button').toLowerCase();
      const dtid = tr.getAttribute('data-testid');
      // Case-preserved label text for XPath. Use starts-with because the
      // DOM text commonly has a trailing '*' (required-field marker)
      // that XPath's normalize-space doesn't strip.
      const labelXp = escXpath(labelTextCased(lbl));

      let xpath;
      if (dtid) {
        const dtidXp = escXpath(dtid);
        xpath = `//${lblTag}[starts-with(normalize-space(.), ${labelXp})]`
              + `/following::${tagLower}[@data-testid=${dtidXp}][1]`;
      } else {
        xpath = `//${lblTag}[starts-with(normalize-space(.), ${labelXp})]`
              + `/following::${tagLower}[1]`;
      }

      if (!isXPathUniqueFor(xpath, tr)) continue;

      // XPath-only result — CSS can't match text, no stable CSS here.
      const result = packResult('', xpath, tr, 'p_label_xpath');
      if (result) return JSON.stringify(result);
    }
    return null;
  }

  // P1 — exact first, partial fallback (same two-pass as Strategy 1/2).
  {
    const exact = tryPTagLabelStrategy(isExactMatch);
    if (exact) return exact;
    const partial = tryPTagLabelStrategy(isPartialMatch);
    if (partial) return partial;
  }

  // ── Strategy 3: Generic text-content match ─────────────────────
  //
  // For apps with no <label> / .MuiFormLabel-root: match on the
  // visible text of any combobox / button / select. Only returns if
  // the generated CSS is genuinely unique to this trigger — refuses
  // shared attributes like `[data-testid="dropdown-button"]` that
  // match every dropdown on the page.
  {
    const TRIGGER_SEL = '[role=combobox], [role=button], button, select';
    const triggers = [...document.querySelectorAll(TRIGGER_SEL)].filter(isVisible);
    let tr = triggers.find(el =>
      el.textContent.replace(/\s+/g, ' ').trim().toLowerCase() === needle
    );
    if (!tr) {
      tr = triggers.find(el => {
        const t = el.textContent.replace(/\s+/g, ' ').trim().toLowerCase();
        return t && (t.startsWith(needle) || needle.startsWith(t) || t.includes(needle));
      });
    }
    if (isVisible(tr)) {
      let css = '';
      let xpath = '';
      if (tr.id) {
        css = '#' + CSS.escape(tr.id);
        xpath = `//*[@id=${escXpath(tr.id)}]`;
      } else {
        const dtid = tr.getAttribute('data-testid');
        if (dtid) {
          css = `[data-testid="${escAttr(dtid)}"]`;
          xpath = `//*[@data-testid=${escXpath(dtid)}]`;
        }
      }
      // P2 — refuse a non-unique selector. Silent wrong-element clicks
      // at Execute time are worse than a clean "no match" here.
      if (isUniqueFor(css, tr)) {
        const result = packResult(css, xpath, tr, 'generic_text_match');
        if (result) return JSON.stringify(result);
      }
    }
  }

  return null;
"""


def _js_locate_trigger_for(label: str) -> str:
    """Build a zero-arg arrow function that resolves a dropdown trigger's
    CSS + XPath + dom_top by its label. Four strategies (TECU label[for]
    → MUI → <p>/heading-tag label → generic text match), first match
    wins. The label is JSON-encoded so quotes and special chars in the
    label don't break the JS."""
    body = _JS_LOCATE_TRIGGER_BODY.replace("TARGET_LABEL", json.dumps(label))
    return "() => {\n" + body + "\n}"


# ── Snapshot parsing: find custom dropdown triggers ─────────────────

_TRIGGER_PREFIXES = re.compile(
    r"(select\s|choose\s|choose\san\s)", re.IGNORECASE
)

_NON_TRIGGER_BUTTONS = {
    "save & continue", "save and continue", "save & exit", "save and exit",
    "continue", "submit", "reset form", "reset", "next", "next step",
    "back", "cancel", "close", "ok", "save", "details", "images",
    "yes", "no", "log in", "login", "register", "sign up",
}

# Date-picker placeholder tokens (these are not dropdowns even though
# they may render as clickable buttons in MUI date pickers).
_DATE_PICKER_LABELS = {
    "dd", "mm", "yy", "yyyy", "hh", "ss", "am", "pm",
    "yyyy-mm-dd", "mm/dd/yyyy", "dd/mm/yyyy",
}


def _clean_label(label: str) -> str:
    """Post-process a label: strip placeholder prefixes, trim asterisk,
    collapse whitespace. Empty string means the label is unusable."""
    if not label:
        return ""
    # Drop zero-width / invisible chars
    label = re.sub(r"[\u200b-\u200f\u2028-\u202f]", "", label)
    label = label.replace("\ufeff", "").strip()
    # Strip trailing asterisk
    label = re.sub(r"\s*\*\s*$", "", label).strip()
    # Strip trailing '?' — many apps (Odoo especially) frame boolean fields as
    # questions ("Is Company?"). The '?' is visual framing; the field name is not.
    label = re.sub(r"\s*\?\s*$", "", label).strip()
    # Placeholder-y prefix cleanup ("Enter salary" → "Salary")
    low = label.lower()
    stripped = False
    for prefix in ("enter ", "please enter ", "please select ", "choose "):
        if low.startswith(prefix):
            label = label[len(prefix):].strip()
            stripped = True
            break
    # Title-case if the result is all-lowercase (common after prefix strip)
    if label and label == label.lower() and (stripped or " " not in label):
        label = label.title()
    return label.strip()


async def _try_cascade_unlock_for_extract(
    adapter,
    child_eid: str,
    dropdown_data: dict,
    js_elements: list[dict],
    screen_name: str,
    defaults: Defaults,
) -> tuple[bool, list[str]]:
    """F2 — fill each declared parent of a disabled dropdown with its
    default value so the child becomes enabled and its options readable.

    Returns (unlocked_attempted, notes). `unlocked_attempted=True` means
    every declared parent was successfully set; caller should re-check
    the child's `disabled` state after this returns. Returns False if
    dependencies aren't declared, any parent lookup fails, or any parent
    select/fill fails — caller should fall back to "record empty + move on".

    Parents are resolved in declared order: dropdowns via the already-
    captured `dropdown_data`, text inputs via `js_elements` (Step 1 JS
    enumerate). If a parent isn't found in either, bail — we don't
    guess.
    """
    from qa.orchestrators.execute_flow import _select_option, _python_fill, _close_popup
    from qa.knowledge.element_id import parse_element_id_full

    parent_ids = defaults.get_dependencies(child_eid)
    if not parent_ids:
        return (False, ["no declared dependencies"])

    # Guarantee a clean popup state before any parent select. If the
    # previous loop iteration left a popup half-open, the first
    # _select_option click below would toggle-close it instead of opening,
    # producing a spurious OPEN_FAILED (observed on TECU: after the first
    # enabled dropdown was captured, the second trigger's cascade hit
    # this exact race).
    await _close_popup(adapter)

    # dropdown_data is keyed by trigger label; build a parallel map keyed
    # by element_id so we can look up a parent by its canonical id.
    # Register the entry under BOTH its full (section-qualified) and
    # canonical (3-part) element_id — extract may detect a section for
    # one field but not its sibling, and the sidecar typically uses the
    # canonical form. Matching both lets either side get away with it.
    dd_by_id: dict[str, tuple[str, dict]] = {}
    for _key, info in dropdown_data.items():
        if not isinstance(info, dict):
            continue
        # Wall 1.2 — read true label from info, not dict key (the key
        # may be a collision-resolved alias like "Country|beneficiary_2").
        lbl = str(info.get("label") or _key)
        section = info.get("section") or ""
        eid_full = make_element_id(screen_name, lbl, "dropdown", section=section)
        eid_3part = make_element_id(screen_name, lbl, "dropdown", section="")
        dd_by_id[eid_full] = (lbl, info)
        dd_by_id.setdefault(eid_3part, (lbl, info))

    notes: list[str] = []
    for parent_id in parent_ids:
        # Ensure popup state is clean between parent fills too. Multi-
        # parent chains (Sector needs Employment Status + Employer) would
        # otherwise leave the first parent's popup half-open, toggle-
        # closing it on the next click.
        await _close_popup(adapter)

        # Case 1: parent is a dropdown we've already captured.
        if parent_id in dd_by_id:
            plabel, pinfo = dd_by_id[parent_id]
            pdefault = defaults.get(plabel)
            if not pdefault:
                return (False, notes + [f"no default for {plabel!r}"])
            css = pinfo.get("locator_css") or ""
            xp  = pinfo.get("locator_xpath") or ""
            if css:
                strategy, value = "css", css
            elif xp:
                strategy, value = "xpath", xp
            else:
                return (False, notes + [f"no locator for {plabel!r}"])
            status, _ = await _select_option(adapter, strategy, value, pdefault)
            if status != "SELECTED":
                return (False, notes + [f"{plabel!r} select → {status}"])
            notes.append(f"{plabel!r}={pdefault!r} selected")
            await asyncio.sleep(0.5)
            continue

        # Case 2: parent may be a text input from Step 1 enumerate.
        try:
            _, _, p_slug, p_type = parse_element_id_full(parent_id)
        except ValueError:
            return (False, notes + [f"bad element_id {parent_id!r}"])
        matched = None
        for el in js_elements:
            raw_label = (el.get("label") or "")
            slug = re.sub(r"[^a-z0-9]+", "_", raw_label.lower()).strip("_")
            if slug == p_slug:
                matched = el
                break
        if matched is None:
            return (False, notes + [f"parent {parent_id!r} not in KB"])
        plabel = matched.get("label", "")
        pdefault = defaults.get(plabel)
        if not pdefault:
            return (False, notes + [f"no default for {plabel!r}"])
        css = ""
        if matched.get("id"):
            css = f"#{matched['id']}"
        elif matched.get("name"):
            tag = "select" if matched.get("native") else "input"
            css = f"{tag}[name='{matched['name']}']"
        if not css:
            return (False, notes + [f"no CSS for {plabel!r}"])
        if p_type == "dropdown" or matched.get("native"):
            status, _ = await _select_option(adapter, "css", css, pdefault)
            if status != "SELECTED":
                return (False, notes + [f"{plabel!r} select → {status}"])
            notes.append(f"{plabel!r}={pdefault!r} selected (native)")
        else:
            ok = await _python_fill(adapter, css, pdefault)
            if not ok:
                return (False, notes + [f"{plabel!r} fill failed"])
            notes.append(f"{plabel!r}={pdefault!r} filled")
        await asyncio.sleep(0.4)

    return (True, notes)


def _find_custom_dropdown_triggers(snapshot: str) -> list[dict]:
    """Find button/combobox elements that are dropdown triggers.

    Detects:
    1. Explicit prefix: 'Select X', 'Choose X'
    2. Label echo: button text matches preceding StaticText (substring
       or 2+ shared tokens — handles reworded labels like
       'Preferred Method of communication' ↔ 'Communication Method')

    Excludes: action buttons, numbered section tabs, Yes/No toggles."""
    triggers: list[dict] = []
    seen_uids: set[str] = set()
    lines = snapshot.split("\n")

    static_texts: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = re.search(r'StaticText\s+"([^"]+)"', line)
        if m:
            static_texts[i] = m.group(1).strip()

    for i, line in enumerate(lines):
        m = re.search(r'uid=(\S+)\s+(button|combobox)\s+"([^"]+)"', line)
        if not m:
            continue
        uid, role, text = m.group(1), m.group(2), m.group(3)
        text_lower = text.strip().lower()

        if text_lower in _NON_TRIGGER_BUTTONS:
            continue
        if re.match(r"^\d+[\.\)]\s", text.strip()):
            continue

        label = ""
        is_trigger = False

        # Pattern 1: explicit prefix
        if _TRIGGER_PREFIXES.search(text):
            label = re.sub(
                r"^(Select\s+|Choose\s+an?\s*|Choose\s+)",
                "", text, flags=re.IGNORECASE,
            ).strip()
            is_trigger = True

        # Pattern 2: button text echoes a preceding StaticText
        if not is_trigger:
            stopwords = {"of", "the", "a", "an", "is", "are", "to", "for", "in"}
            btn_tokens = set(re.findall(r"\w+", text_lower)) - stopwords
            for j in range(i - 1, max(i - 5, -1), -1):
                if j not in static_texts:
                    continue
                st = static_texts[j]
                st_clean = re.sub(r"\s*\*\s*$", "", st).strip().lower()
                if not st_clean:
                    continue
                if (st_clean == text_lower
                        or text_lower.startswith(st_clean)
                        or st_clean.startswith(text_lower)):
                    label = re.sub(r"\s*\*\s*$", "", st).strip()
                    is_trigger = True
                    break
                st_tokens = set(re.findall(r"\w+", st_clean)) - stopwords
                if len(btn_tokens & st_tokens) >= 2:
                    label = re.sub(r"\s*\*\s*$", "", st).strip()
                    is_trigger = True
                    break

        # Prefer the preceding StaticText as the label when available —
        # "Income Range" is more descriptive than stripping "Choose an option"
        if is_trigger:
            for j in range(i - 1, max(i - 5, -1), -1):
                if j in static_texts:
                    better = re.sub(r"\s*\*\s*$", "", static_texts[j]).strip()
                    if better:
                        label = better
                    break

        # Clean up the label (strip asterisk, invisible chars, placeholder prefix)
        label = _clean_label(label)

        # Reject date-picker pseudo-dropdowns (DD/MM/YYYY buttons)
        if label.lower() in _DATE_PICKER_LABELS:
            continue

        # Skip triggers we couldn't label — they're almost certainly
        # false positives (a button we failed to recognize as non-trigger).
        if is_trigger and label and uid not in seen_uids:
            seen_uids.add(uid)
            triggers.append({
                "uid": uid,
                "trigger_text": text,
                "label": label,
                "role": role,
            })

    return triggers


# ── Build KB from extracted data ────────────────────────────────────

_TYPE_MAP = {
    "text_input": ElementType.TEXT_INPUT,
    "email": ElementType.TEXT_INPUT,
    "phone": ElementType.TEXT_INPUT,
    "date": ElementType.DATE_PICKER,
    "dropdown": ElementType.DROPDOWN,
    "file_upload": ElementType.FILE_UPLOAD,
    "radio": ElementType.RADIO,
    "checkbox": ElementType.CHECKBOX,
    "button": ElementType.BUTTON,
}


def _build_screen(
    screen_name: str,
    page_url: str,
    js_elements: list[dict],
    dropdown_data: dict[str, dict],
) -> ScreenKnowledge:
    # Wall E2 — track dom_top per L0 element so we can sort the final
    # list by DOM source order (not the type-batched order in which
    # js_elements arrived). Kept as a side-table rather than a new L0
    # field because dom_top is purely an Extract-time artifact, not
    # something Plan / Execute need to reason about downstream.
    l0: list[L0Element] = []
    l1: list[L1Element] = []
    dom_tops: dict[str, float] = {}  # element_id → top (pixels)

    for el in js_elements:
        label = _clean_label(el.get("label") or "")
        if not label:
            continue
        # Skip date-picker piece buttons that leaked in as text_input
        if label.lower() in _DATE_PICKER_LABELS:
            continue
        etype_str = el.get("type", "text_input")
        etype = _TYPE_MAP.get(etype_str, ElementType.OTHER)
        section = _clean_label(el.get("section") or "")
        eid = make_element_id(screen_name, label, etype.value, section=section)

        revealed_by = str(el.get("revealed_by") or "").strip()
        behavior_parts = []
        if section:
            behavior_parts.append(f"Section: {section}")
        if revealed_by:
            behavior_parts.append(f"Revealed by: {revealed_by}")

        l0.append(L0Element(
            element_id=eid,
            name=label,
            type=etype,
            required=bool(el.get("required")),
            options=el.get("options", []),
            screen_name=screen_name,
            default_value=el.get("value", ""),
            accept=el.get("accept", ""),
            behavior=" | ".join(behavior_parts),
        ))
        if isinstance(el.get("dom_top"), (int, float)):
            dom_tops[eid] = float(el["dom_top"])

        locators: list[Locator] = []
        if el.get("id"):
            locators.append(Locator(
                strategy="css", value=f"#{el['id']}", confidence=0.9,
            ))
        if el.get("name"):
            tag = "select" if el.get("native") else "input"
            locators.append(Locator(
                strategy="css",
                value=f"{tag}[name='{el['name']}']",
                confidence=0.8,
            ))
        # Label-based XPath fallbacks — robust across SPA re-renders because
        # visible label text is far more stable than generated IDs or
        # component-scoped name attributes. Fixes Salesforce Lightning,
        # Odoo, Forgenite, and most modern React/Vue apps where the
        # primary id-based CSS goes stale between extract and execute.
        if label:
            if el.get("native"):
                tag_xpath = "select"
            elif etype == ElementType.TEXT_INPUT:
                # textarea + input both valid for text fields
                tag_xpath = "*[self::input or self::textarea]"
            else:
                tag_xpath = "input"
            label_lit = _xpath_string_literal(label)
            # Primary label-xpath: <label>X</label> then the next matching tag
            locators.append(Locator(
                strategy="xpath",
                value=f"//label[normalize-space()={label_lit}]/following::{tag_xpath}[1]",
                confidence=0.75,
            ))
            # aria-label match (modern components)
            locators.append(Locator(
                strategy="xpath",
                value=f"//{tag_xpath}[@aria-label={label_lit}]",
                confidence=0.70,
            ))
            # placeholder match (placeholder-as-label pattern)
            locators.append(Locator(
                strategy="xpath",
                value=f"//{tag_xpath}[@placeholder={label_lit}]",
                confidence=0.65,
            ))
        l1.append(L1Element(
            element_id=eid,
            locators=locators,
            screen_name=screen_name,
        ))

    # Merge custom dropdown options discovered via LLM
    for trigger_key, info in dropdown_data.items():
        if isinstance(info, dict):
            options = info.get("options", [])
            disabled = bool(info.get("disabled", False))
            section = _clean_label(info.get("section") or "")
            locator_css = info.get("locator_css", "") or ""
            locator_xpath = info.get("locator_xpath", "") or ""
            raw_top = info.get("dom_top")
            dom_top = float(raw_top) if isinstance(raw_top, (int, float)) else None
            revealed_by = str(info.get("revealed_by") or "").strip()
            # Wall 1.2 — read the true label from info, not from the dict
            # key, because _dd_put rewrites the key on collision.
            trigger_label = str(info.get("label") or trigger_key)
        else:
            options = info
            disabled = False
            section = ""
            locator_css = ""
            locator_xpath = ""
            dom_top = None
            revealed_by = ""
            trigger_label = str(trigger_key)

        disabled_note = (
            "Disabled when extracted — likely depends on a prior dropdown being filled. "
            "Execute pipeline should fill parent field first."
            if disabled else ""
        )
        section_note = f"Section: {section}" if section else ""
        revealed_note = f"Revealed by: {revealed_by}" if revealed_by else ""
        behavior = " | ".join(
            p for p in [section_note, revealed_note, disabled_note] if p
        )

        eid = make_element_id(screen_name, trigger_label, "dropdown", section=section)
        existing = next((e for e in l0 if e.element_id == eid), None)
        if existing:
            existing.options = options
            if behavior and not existing.behavior:
                existing.behavior = behavior
            # Back-fill dom_top if this dropdown was a native-select
            # we already picked up in step 1 — trust the locator pass
            # over the JS rect since it's measured after any layout shifts.
            if dom_top is not None:
                dom_tops[eid] = dom_top
            # Attach the CSS selector to the matching L1 if we have one
            # and the L1 is currently empty — Wall E1 unblocking.
            if locator_css:
                l1_existing = next(
                    (x for x in l1 if x.element_id == eid), None
                )
                if l1_existing is not None:
                    if not any(loc.strategy == "css" for loc in l1_existing.locators):
                        l1_existing.locators.append(Locator(
                            strategy="css", value=locator_css, confidence=0.85,
                        ))
                    # Also back-fill xpath if missing
                    if locator_xpath and not any(
                        loc.strategy == "xpath" for loc in l1_existing.locators
                    ):
                        l1_existing.locators.append(Locator(
                            strategy="xpath", value=locator_xpath, confidence=0.80,
                        ))
            continue

        l0.append(L0Element(
            element_id=eid,
            name=trigger_label,
            type=ElementType.DROPDOWN,
            required=True,
            options=options,
            behavior=behavior,
            screen_name=screen_name,
        ))
        if dom_top is not None:
            dom_tops[eid] = dom_top

        # Wall E1 — attach CSS locator captured by _JS_LOCATE_TRIGGER so
        # SELECT_AND_VERIFY can target the trigger via document.querySelector.
        dd_locators: list[Locator] = []
        if locator_css:
            dd_locators.append(Locator(
                strategy="css", value=locator_css, confidence=0.85,
            ))
        if locator_xpath:
            dd_locators.append(Locator(
                strategy="xpath", value=locator_xpath, confidence=0.80,
            ))
        l1.append(L1Element(
            element_id=eid,
            locators=dd_locators,
            screen_name=screen_name,
        ))

    # Wall 1.2 — disambiguate duplicate element_ids. When a form has
    # repeating sub-sections with the same field names (TECU page 4's
    # Beneficiary 1 / Beneficiary 2 both containing "First Name",
    # "Country", "Is Beneficiary a member?"), the js_elements enumerate
    # pass produces multiple entries whose (label, type, section)
    # collapse to the same element_id. Execute pipeline then can't tell
    # them apart.
    #
    # Fix: for each duplicate element_id, keep the first occurrence as-is
    # and append "_2", "_3", ... to the section of subsequent occurrences
    # in DOM order, then re-derive element_id. Uses dom_tops when
    # available to establish DOM order; falls back to list order.
    eid_counts: dict[str, int] = {}
    for el in l0:
        eid_counts[el.element_id] = eid_counts.get(el.element_id, 0) + 1
    duplicated = {eid for eid, n in eid_counts.items() if n > 1}

    if duplicated:
        print(f"  [form] 1.2  disambiguating {len(duplicated)} duplicate "
              f"element_id(s) (repeating sub-forms)")
        # Process elements in DOM order so the _1 suffix goes to the
        # topmost occurrence.
        def _dom_order_key(idx_el: tuple[int, L0Element]) -> tuple[float, int]:
            idx, el = idx_el
            top = dom_tops.get(el.element_id)
            return (top if top is not None else 1e12, idx)

        ordered = sorted(enumerate(l0), key=_dom_order_key)
        occurrence_count: dict[str, int] = {}
        for _orig_idx, el in ordered:
            if el.element_id not in duplicated:
                continue
            occurrence_count[el.element_id] = (
                occurrence_count.get(el.element_id, 0) + 1
            )
            n = occurrence_count[el.element_id]
            if n == 1:
                # First in DOM order keeps the plain id.
                continue
            # Parse the old eid to extract screen / section / label / type.
            from qa.knowledge.element_id import parse_element_id_full
            screen_slug, old_section, label_slug, type_slug = (
                parse_element_id_full(el.element_id)
            )
            new_section = f"{old_section}_{n}" if old_section else f"group_{n}"
            new_eid = make_element_id(
                screen_slug, label_slug, type_slug, section=new_section,
            )
            # Rewrite the L0 entry + any matching L1 entries.
            old_eid = el.element_id
            el.element_id = new_eid
            for l1_el in l1:
                if l1_el.element_id == old_eid:
                    l1_el.element_id = new_eid
                    break
            # Preserve dom_top under the new eid so sort still works.
            if old_eid in dom_tops:
                dom_tops[new_eid] = dom_tops[old_eid]
            print(f"  [form]       {old_eid} → {new_eid}")

    # Wall E2 — sort L0 by dom_top ascending. Elements without a
    # captured rect sink to the bottom (preserving their relative order)
    # — this keeps legacy extractors that didn't populate dom_top from
    # breaking. L1 is a lookup table, not an ordered sequence, but we
    # mirror the L0 sort for JSON readability.
    def _sort_key(el: L0Element) -> tuple[int, float, int]:
        top = dom_tops.get(el.element_id)
        if top is None:
            return (1, 0.0, 0)  # unranked bucket
        return (0, top, 0)

    l0_sorted = sorted(enumerate(l0), key=lambda pair: (_sort_key(pair[1]), pair[0]))
    l0 = [pair[1] for pair in l0_sorted]

    # Sort L1 to mirror L0 order (stable for unmatched entries).
    l0_index = {el.element_id: i for i, el in enumerate(l0)}
    l1.sort(key=lambda x: l0_index.get(x.element_id, 10_000_000))

    return ScreenKnowledge(
        screen_name=screen_name,
        screen_url=page_url,
        l0=l0,
        l1=l1,
    )


# ── Wall 1.6: conditional UI two-pass extractor ─────────────────────
#
# Called after the main Step 3 loop (and Fix C retry) completes. Scans
# the already-captured js_elements for reveal-trigger radios, flips each
# one to Yes, re-enumerates + re-finds triggers, and captures any newly-
# visible fields / dropdowns into js_elements + dropdown_data in place.
#
# Radios are NOT restored — revealed fields need to stay addressable for
# Execute. Per-test field restore is Wall 1.3's job downstream.

def _find_yes_radio_uid(snapshot: str, group_label: str) -> str:
    """Find the accessibility-tree uid of the "Yes" radio that belongs to a
    reveal group. Strategy: locate a StaticText line whose normalized text
    matches the group label (fuzzy), then walk forward up to 12 lines
    looking for the first `radio "Yes"` line.

    Returns empty string when no match is found — caller falls back to a
    JS click (works but invisible to the user)."""
    if not snapshot or not group_label:
        return ""
    lines = snapshot.split("\n")
    needle = re.sub(r"\s+", " ", group_label).strip().rstrip("?").lower()
    if not needle:
        return ""

    # First locate the anchor line (the group's question text).
    anchor = -1
    for i, line in enumerate(lines):
        m = re.search(r'StaticText\s+"([^"]+)"', line)
        if not m:
            continue
        text = re.sub(r"\s+", " ", m.group(1)).strip().rstrip("?").lower()
        # Fuzzy match: full-equal or anchor-contains-needle (handles
        # trailing "*" / extra whitespace that _detect saw through).
        if text == needle or (needle in text and len(needle) > 5):
            anchor = i
            break
    if anchor == -1:
        return ""

    # Walk forward for the first Yes radio within the question's block.
    for j in range(anchor + 1, min(anchor + 13, len(lines))):
        m = re.search(r'uid=(\S+)\s+radio\s+"([^"]+)"', lines[j])
        if not m:
            continue
        if re.match(r"^y(es)?\b", m.group(2).strip(), re.IGNORECASE):
            return m.group(1)
    return ""


async def _reveal_and_reextract(
    adapter,
    *,
    budget: BudgetTracker,
    page_gc: GuardrailContext,
    js_elements: list[dict],
    triggers: list[dict],
    dropdown_data: dict[str, dict],
    checkpoint_fn,
) -> None:
    server = adapter.get_mcp_server()

    candidates = _detect_reveal_radios(js_elements)
    if not candidates:
        print("  [form] 1.6  no reveal-trigger radios detected — skipping two-pass")
        return

    print(f"\n  [form] 1.6  reveal pass: {len(candidates)} candidate radio(s)")
    for c in candidates:
        print(f"  [form]       - {c.get('label', '')!r} "
              f"(name={c.get('name', '')!r}, options={c.get('options', [])})")

    # Fingerprints used to diff pre- vs post-reveal DOM state.
    # Include section so repeating sub-sections (Nominee's Country vs
    # Joint Partner's Country, Beneficiary 1's First Name vs Beneficiary 2's)
    # don't get filtered out as "already seen" — Wall 1.2 interaction.
    def _std_key(el: dict) -> tuple[str, str, str]:
        return (
            str(el.get("label", "")).strip().lower(),
            str(el.get("type", "")),
            str(el.get("section", "")).strip().lower(),
        )

    def _trig_key(t: dict) -> tuple[str, str]:
        return (
            str(t.get("label", "")).strip().lower(),
            str(t.get("section", "")).strip().lower(),
        )

    pre_elem_keys: set[tuple[str, str, str]] = {_std_key(el) for el in js_elements}
    pre_trigger_keys: set[tuple[str, str]] = {_trig_key(t) for t in triggers}

    for cand in candidates:
        group_name = str(cand.get("name") or "")
        cand_label = str(cand.get("label") or "")

        try:
            page_gc.check()
        except GuardrailExit as e:
            print(f"  [form]       ✗ page guardrail hit ({e.reason}) "
                  "— stopping reveal pass")
            return

        if not group_name:
            print(f"  [form]       ⚠ {cand_label!r}: no radio group name, skipping")
            continue

        # Clear any stray popup before interacting with the radio.
        await adapter.evaluate_script(_JS_CLOSE_POPUP)
        await asyncio.sleep(0.2)

        # Prefer an MCP click — the cursor visibly moves to the Yes radio,
        # matching the visible dropdown behaviour. The MCP click goes
        # through Chrome DevTools Protocol so the user sees real pointer
        # motion and a highlighted click. Falls back to JS click when the
        # snapshot doesn't expose a uid for the Yes option (still correct,
        # but invisible on screen).
        #
        # Clicking an already-selected radio is idempotent in React
        # (onChange won't fire for value = current). Safe to just click.
        clicked_ok = False
        try:
            pre_snap_result = await server.call_tool("take_snapshot", {})
            pre_snap_text = ""
            if pre_snap_result.content:
                pre_snap_text = pre_snap_result.content[0].text or ""
            yes_uid = _find_yes_radio_uid(pre_snap_text, cand_label)
        except Exception:
            yes_uid = ""

        if yes_uid:
            try:
                await server.call_tool("click", {"uid": yes_uid})
                clicked_ok = True
                print(f"  [form]       ✓ {cand_label!r}: clicked Yes "
                      f"via MCP (uid={yes_uid})")
            except Exception as e:
                print(f"  [form]       ⚠ {cand_label!r}: MCP click failed "
                      f"({e}) — falling back to JS")

        if not clicked_ok:
            click_raw = await adapter.evaluate_script(
                _js_click_radio_yes_for(group_name)
            )
            click_info = _safe_parse(click_raw) or {}
            if not isinstance(click_info, dict):
                click_info = {}

            if not click_info.get("found"):
                print(f"  [form]       ⚠ {cand_label!r}: no Yes option found "
                      "— skipping")
                continue
            if click_info.get("was_selected"):
                print(f"  [form]       ⏭ {cand_label!r}: Yes already selected "
                      "(JS fallback — invisible to viewer)")
            elif click_info.get("clicked"):
                print(f"  [form]       ✓ {cand_label!r}: clicked Yes "
                      "(JS fallback — invisible to viewer)")
            else:
                print(f"  [form]       ⚠ {cand_label!r}: click did not register "
                      "— skipping")
                continue

        await asyncio.sleep(1.5)  # let React / MUI render the revealed fields

        # Re-enumerate standard elements and diff against pre-reveal state.
        raw = await adapter.evaluate_script(_JS_ENUMERATE)
        new_js_elements = _safe_parse(raw) or []
        if not isinstance(new_js_elements, list):
            new_js_elements = []

        added_std = []
        for el in new_js_elements:
            lbl = str(el.get("label", "")).strip()
            if not lbl:
                continue
            if _std_key(el) in pre_elem_keys:
                continue
            added_std.append(el)

        if added_std:
            print(f"  [form]       → {len(added_std)} new standard element(s)")
            for el in added_std:
                print(f"  [form]         + {el.get('label', '')!r} "
                      f"({el.get('type', '')}, "
                      f"section={el.get('section', '')!r})")
                el["revealed_by"] = cand_label
                js_elements.append(el)
                pre_elem_keys.add(_std_key(el))
        else:
            print(f"  [form]       → 0 new standard elements")

        # Find new dropdown triggers in the post-reveal snapshot.
        snap_result = await server.call_tool("take_snapshot", {})
        new_snap_text = ""
        if snap_result.content:
            new_snap_text = snap_result.content[0].text or ""

        new_triggers = _find_custom_dropdown_triggers(new_snap_text)

        # Resolve section per trigger BEFORE diffing so (label, section)
        # disambiguates repeats across sub-sections. Nominee's "Country"
        # and (hypothetical) Joint Partner's "Country" share a label but
        # live under different headings — both should survive the diff.
        added_triggers = []
        for t in new_triggers:
            t_label = str(t.get("label", "")).strip().lower()
            try:
                section_raw = await adapter.evaluate_script(
                    _js_section_for_text(t.get("trigger_text", ""))
                )
                section_info = _safe_parse(section_raw) or {}
                t_section = str(
                    section_info.get("section", "") or ""
                ).strip().lower()
            except Exception:
                t_section = ""
            t["section"] = t_section  # persist for downstream processing
            if (t_label, t_section) in pre_trigger_keys:
                continue
            added_triggers.append(t)

        if not added_triggers:
            print(f"  [form]       → 0 new custom dropdown triggers")
            await checkpoint_fn()
            continue

        print(f"  [form]       → {len(added_triggers)} new custom dropdown "
              f"trigger(s)")

        # Process each new trigger: locator resolve → open → LLM extract →
        # CoVe → close. Intentionally simpler than the main Step 3 loop:
        # no cascade unlock and no default-commit — revealed sections don't
        # typically depend on outside parents on TECU. If that assumption
        # fails on another app, the disabled dropdown will just record
        # empty options (same behaviour as main loop when defaults absent).
        for j, trig in enumerate(added_triggers):
            uid = trig["uid"]
            label = trig["label"]
            trigger_text = trig["trigger_text"]
            print(f"  [form]       [{j+1}/{len(added_triggers)}] {label!r} "
                  f"(uid={uid})")

            try:
                page_gc.check()
            except GuardrailExit as e:
                print(f"  [form]         ✗ page guardrail hit ({e.reason}) "
                      "— stopping reveal pass")
                return

            dropdown_gc = per_dropdown_scope(parent=page_gc)

            section_raw = await adapter.evaluate_script(
                _js_section_for_text(trigger_text)
            )
            section_info = _safe_parse(section_raw) or {}
            trig_section = section_info.get("section", "") or ""

            locator_css = ""
            locator_xpath = ""
            locator_strategy = ""
            dom_top: float | None = None
            loc_raw = await adapter.evaluate_script(_js_locate_trigger_for(label))
            loc_info = _safe_parse(loc_raw) or {}
            if isinstance(loc_info, dict):
                locator_css = loc_info.get("css", "") or ""
                locator_xpath = loc_info.get("xpath", "") or ""
                locator_strategy = loc_info.get("strategy", "") or ""
                if isinstance(loc_info.get("dom_top"), (int, float)):
                    dom_top = float(loc_info["dom_top"])
            if not locator_css and not locator_xpath:
                print(f"  [form]         ⚠ no locator resolved for {label!r} "
                      "— SELECT_AND_VERIFY will block until fixed")

            open_snap_text = ""
            options: list[str] = []
            try:
                await server.call_tool("click", {"uid": uid})
                await asyncio.sleep(1.5)
                open_snap = await server.call_tool("take_snapshot", {})
                if open_snap.content:
                    open_snap_text = open_snap.content[0].text or ""
                result = await llm_classify(
                    EXTRACT_DROPDOWN_OPTIONS_PROMPT,
                    open_snap_text,
                    EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
                    budget=budget,
                    label=f"reveal_opts_{j+1}",
                    guardrails=dropdown_gc,
                )
                options = result.get("options", []) or []
            except GuardrailExit as e:
                print(f"  [form]         ✗ dropdown guardrail ({e.reason})")
            except Exception as e:
                print(f"  [form]         ⚠ open/read failed: {e}")

            seen: set[str] = set()
            clean: list[str] = []
            for o in options:
                o = str(o).strip()
                if (o and o not in seen and not re.match(
                        r"^(Select|Choose|--|Please)", o, re.IGNORECASE)):
                    seen.add(o)
                    clean.append(o)

            if clean and open_snap_text:
                verify_gc = per_verify_scope(parent=dropdown_gc)
                try:
                    verify_result = await verify_list_cascaded(
                        {"options": clean}, "options", open_snap_text,
                        label=f"reveal_opts_{j+1}_cove",
                        guardrails=verify_gc, log=False,
                    )
                    clean = verify_result.claim.get("options", clean)
                    if verify_result.dropped:
                        print(
                            f"  [form]         🔍 CoVe dropped "
                            f"{len(verify_result.dropped)} option(s)"
                        )
                except GuardrailExit as e:
                    print(f"  [form]         ⚠ CoVe guardrail ({e.reason}) "
                          "— keeping raw options")

            print(f"  [form]         → {len(clean)} options")
            for o in clean[:3]:
                print(f"  [form]           - {o!r}")

            _dd_put(dropdown_data, label, trig_section, {
                "options": clean,
                "disabled": False,
                "locator_css": locator_css,
                "locator_xpath": locator_xpath,
                "locator_strategy": locator_strategy,
                "dom_top": dom_top,
                "revealed_by": cand_label,
            })
            pre_trigger_keys.add((label.strip().lower(),
                                   trig_section.strip().lower()))

            try:
                await server.call_tool("press_key", {"key": "Escape"})
            except Exception:
                pass
            await adapter.evaluate_script(_JS_CLOSE_POPUP)
            await asyncio.sleep(0.4)

            await checkpoint_fn()


# ── Main extraction flow ────────────────────────────────────────────

async def extract_form(
    adapter,
    app_name: str,
    screen_name: str,
    budget: BudgetTracker,
    page_url: str = "",
    on_progress=None,
    guardrails: GuardrailContext | None = None,
    defaults: Defaults | None = None,
) -> ScreenKnowledge:
    """Extract all form elements from the currently-loaded page.

    Args:
        defaults: optional user-provided Defaults + dependencies sidecar.
            When present, cascaded (disabled-at-capture) dropdowns will
            be unlocked by filling their parent fields with the declared
            defaults, allowing their real options to be captured. Without
            defaults, those dropdowns remain marked disabled + empty
            (existing behaviour).
        on_progress: optional async callback `(ScreenKnowledge) -> None`
            invoked after each custom-dropdown extraction with the
            current partial ScreenKnowledge. Callers typically use this
            to checkpoint the KB so a mid-loop crash never loses more
            than one element of work. See Wall 2.5f / principle N16.
        guardrails: optional per-page GuardrailContext. If omitted, a
            fresh per_page_scope() is created. Per-dropdown child scopes
            are spawned internally — when a single dropdown hits its
            cap it gets marked empty/partial and the loop continues;
            when the page-level cap is hit the whole loop exits cleanly.
    """
    server = adapter.get_mcp_server()
    t0 = time.time()

    # Page-level guardrail: if caller didn't provide one, make one fresh
    # so this function remains usable without explicit budget management.
    page_gc = guardrails if guardrails is not None else per_page_scope()

    # ── Step 1: JS enumerates standard form elements ──────────────
    _narrate(f"Scanning '{screen_name}' for interactive elements...")
    print("  [form] 1/3  JS: enumerate standard form elements")
    raw = await adapter.evaluate_script(_JS_ENUMERATE)
    js_elements = _safe_parse(raw) or []
    if not isinstance(js_elements, list):
        js_elements = []
    print(f"  [form]      found {len(js_elements)} standard element(s)")

    type_counts: dict[str, int] = {}
    for el in js_elements:
        t = el.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    for t, c in sorted(type_counts.items()):
        print(f"  [form]        {t}: {c}")

    if js_elements:
        _narrate(
            f"Found {len(js_elements)} standard element(s): "
            f"{_describe_type_counts(type_counts)}."
        )

    # dropdown_data is populated in step 3 as we iterate triggers, but we
    # need to declare it up here so the _checkpoint helper (used after
    # step 1 and in step 3) sees it in scope.
    dropdown_data: dict[str, dict] = {}

    # Local helper that builds a partial ScreenKnowledge from whatever
    # data we've captured so far and passes it to the caller's on_progress
    # callback. Invoked after every meaningful step so a mid-loop crash
    # never loses more than one dropdown of work (principle N16).
    async def _checkpoint() -> None:
        if on_progress is None:
            return
        partial = _build_screen(
            screen_name=screen_name,
            page_url=page_url,
            js_elements=js_elements,
            dropdown_data=dropdown_data,
        )
        await on_progress(partial)

    # Step 1 result is already enough for a first checkpoint — all the
    # text fields, radios, checkboxes, and file inputs are captured
    # deterministically, no LLM risk.
    await _checkpoint()

    # ── Step 2: snapshot → find custom dropdown triggers ──────────
    print("  [form] 2/3  snapshot: find custom dropdown triggers")
    snap_result = await server.call_tool("take_snapshot", {})
    snap_text = ""
    if snap_result.content:
        snap_text = snap_result.content[0].text or ""

    triggers = _find_custom_dropdown_triggers(snap_text)
    print(f"  [form]      found {len(triggers)} custom dropdown trigger(s)")

    if triggers:
        _narrate(
            f"Also noticed {len(triggers)} custom dropdown(s) that need to be "
            f"opened to read their options. I'll open each one, capture the "
            f"list, and close it."
        )

    # ── Step 3: open each dropdown → LLM extracts options → close ─
    print("  [form] 3/3  open each dropdown → LLM extracts options → close")

    for i, trig in enumerate(triggers):
        uid = trig["uid"]
        label = trig["label"]
        trigger_text = trig["trigger_text"]
        print(f"  [form]      [{i+1}/{len(triggers)}] {label!r} (uid={uid})")
        _narrate(f"Opening dropdown '{label}' ({i+1}/{len(triggers)})...")

        # Check the page-level guardrail BEFORE spending on this dropdown.
        # If we've already exhausted the page budget, stop the loop cleanly
        # instead of grinding through remaining triggers.
        try:
            page_gc.check()
        except GuardrailExit as e:
            print(f"  [form]      ✗ page guardrail hit ({e.reason}) — "
                  f"stopping loop at trigger {i+1}/{len(triggers)}")
            break

        # Per-dropdown guardrail — bounded cost/calls for this one trigger.
        # When a single dropdown hits its cap it gets recorded as empty and
        # the loop continues to the next.
        dropdown_gc = per_dropdown_scope(parent=page_gc)

        # Enrich trigger with its nearest section heading — matches what
        # the JS enumerate already attaches to standard form elements.
        section_raw = await adapter.evaluate_script(
            _js_section_for_text(trigger_text)
        )
        section_info = _safe_parse(section_raw) or {}
        trig["section"] = section_info.get("section", "") or ""

        # Walls E1 + E2 — resolve a stable locator (CSS + XPath) + dom_top
        # for this trigger BEFORE clicking it. The locator lets
        # ExecuteOrchestrator target the dropdown via document.querySelector
        # (Wall 1.1); dom_top lets _build_screen sort L0 in true
        # top-to-bottom order. Resolver tries TECU/HTML5 → MUI → generic
        # text match and returns whichever strategy won.
        locator_css = ""
        locator_xpath = ""
        locator_strategy = ""
        dom_top: float | None = None
        loc_raw = await adapter.evaluate_script(_js_locate_trigger_for(label))
        loc_info = _safe_parse(loc_raw) or {}
        if isinstance(loc_info, dict):
            locator_css = loc_info.get("css", "") or ""
            locator_xpath = loc_info.get("xpath", "") or ""
            locator_strategy = loc_info.get("strategy", "") or ""
            if isinstance(loc_info.get("dom_top"), (int, float)):
                dom_top = float(loc_info["dom_top"])
        strat = f" [{locator_strategy}]" if locator_strategy else ""
        if not locator_css and not locator_xpath:
            # No strategy matched — Execute will BLOCK on this dropdown.
            print(f"  [form]        ⚠ no locator resolved for {label!r} "
                  "— SELECT_AND_VERIFY will block until fixed")
        elif locator_css:
            print(f"  [form]        css={locator_css!r}{strat} dom_top={dom_top}")
            if locator_xpath:
                print(f"  [form]        xpath={locator_xpath!r}")
        else:
            # XPath-only (Strategy 4 for <p>-tag labels). Execute uses
            # document.evaluate for these — no CSS by design.
            print(f"  [form]        xpath={locator_xpath!r}{strat} dom_top={dom_top}")
            print(f"  [form]        css=<xpath-only, no stable CSS anchor>")

        # Track whether this trigger was unlocked via cascade. After
        # options are captured we'll commit a default value on the child
        # so any grandchildren (that depend on this one being filled)
        # can also unlock in a later iteration or the second pass.
        cascaded_unlocked = False

        # Disabled check BEFORE clicking: TECU / MUI mark cascading
        # dependent dropdowns as disabled until their parent fields are
        # filled. F2 attempts cascade unlock using the defaults sidecar
        # before falling back to "record empty + move on".
        disabled_raw = await adapter.evaluate_script(
            _js_is_disabled_for(trigger_text)
        )
        disabled_info = _safe_parse(disabled_raw) or {}
        is_disabled = bool(disabled_info.get("disabled", False))
        if is_disabled:
            print(f"  [form]        ⊘ disabled at capture — trying cascade unlock")
            attempted, unlock_notes = (False, [])
            if defaults is not None:
                eid = make_element_id(
                    screen_name, label, "dropdown",
                    section=trig.get("section", ""),
                )
                attempted, unlock_notes = await _try_cascade_unlock_for_extract(
                    adapter=adapter,
                    child_eid=eid,
                    dropdown_data=dropdown_data,
                    js_elements=js_elements,
                    screen_name=screen_name,
                    defaults=defaults,
                )
                for n in unlock_notes:
                    print(f"  [form]          unlock: {n}")
            else:
                print(f"  [form]          unlock: skipped (no --defaults passed)")

            if attempted:
                # Re-probe disabled state.
                disabled_raw2 = await adapter.evaluate_script(
                    _js_is_disabled_for(trigger_text)
                )
                is_disabled = bool(
                    (_safe_parse(disabled_raw2) or {}).get("disabled", False)
                )
                if not is_disabled:
                    print(f"  [form]        ✓ unlocked via cascade — capturing options")
                    cascaded_unlocked = True
                else:
                    print(f"  [form]        still disabled after cascade — recording empty")

            if is_disabled:
                _dd_put(dropdown_data, label, trig.get("section", "") or "", {
                    "options": [],
                    "disabled": True,
                    "locator_css": locator_css,
                    "locator_xpath": locator_xpath,
                    "locator_strategy": locator_strategy,
                    "dom_top": dom_top,
                })
                await _checkpoint()
                continue
            # Fall through to the normal open_and_read path below —
            # the dropdown is now unlocked and behaves like any other.

        async def _open_and_read(attempt_label: str) -> tuple[list[str], str]:
            """Click trigger, wait, snapshot, LLM extracts options.
            Returns (options_list, open_snapshot_text) — snapshot is
            needed by CoVe below so options can be source-verified."""
            await server.call_tool("click", {"uid": uid})
            await asyncio.sleep(1.5)  # longer wait — some MUI popups are slow
            open_snap = await server.call_tool("take_snapshot", {})
            snap_text = ""
            if open_snap.content:
                snap_text = open_snap.content[0].text or ""
            result = await llm_classify(
                EXTRACT_DROPDOWN_OPTIONS_PROMPT,
                snap_text,
                EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
                budget=budget,
                label=attempt_label,
                guardrails=dropdown_gc,
            )
            return result.get("options", []) or [], snap_text

        # First attempt — may hit the per-dropdown cap for a weird trigger.
        open_snap_text = ""
        try:
            options, open_snap_text = await _open_and_read(f"opts_{i+1}")
        except GuardrailExit as e:
            print(f"  [form]        ✗ dropdown guardrail hit ({e.reason}) — "
                  f"skipping {label!r}")
            _dd_put(dropdown_data, label, trig.get("section", "") or "", {
                "options": [],
                "disabled": False,
                "guardrail_exit": e.reason,
                "locator_css": locator_css,
                "locator_xpath": locator_xpath,
                "locator_strategy": locator_strategy,
                "dom_top": dom_top,
            })
            await _checkpoint()
            # Still try to close any popup we may have opened before exit.
            try:
                await adapter.evaluate_script(_JS_CLOSE_POPUP)
            except Exception:
                pass
            continue

        # If empty, close fully and retry once with more patience — some
        # dropdowns (e.g. TECU's Purpose / Country of Issuance / Education)
        # take longer to render than the first open allowed.
        if not options:
            print(f"  [form]        ⚠ 0 options on first read — closing + retrying with longer wait")
            try:
                await server.call_tool("press_key", {"key": "Escape"})
            except Exception:
                pass
            await adapter.evaluate_script(_JS_CLOSE_POPUP)
            await asyncio.sleep(1.0)
            # Retry with extra wait — again bounded by dropdown_gc.
            try:
                await server.call_tool("click", {"uid": uid})
                await asyncio.sleep(2.5)
                open_snap = await server.call_tool("take_snapshot", {})
                snap_text_retry = ""
                if open_snap.content:
                    snap_text_retry = open_snap.content[0].text or ""
                open_snap_text = snap_text_retry or open_snap_text
                result = await llm_classify(
                    EXTRACT_DROPDOWN_OPTIONS_PROMPT,
                    snap_text_retry,
                    EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
                    budget=budget,
                    label=f"opts_{i+1}_retry",
                    guardrails=dropdown_gc,
                )
                options = result.get("options", []) or []
            except GuardrailExit as e:
                print(f"  [form]        ✗ dropdown retry guardrail hit ({e.reason})")
                options = []

        # Dedupe + drop placeholder-y entries
        seen: set[str] = set()
        clean: list[str] = []
        for o in options:
            o = str(o).strip()
            if (o and o not in seen
                    and not re.match(r"^(Select|Choose|--|Please)", o, re.IGNORECASE)):
                seen.add(o)
                clean.append(o)

        # ── CoVe verification (Wall 2.7) — every option the LLM claimed must
        # actually appear in the snapshot we took right after opening the
        # dropdown. Tier 1 (deterministic string-presence) is free and
        # catches hallucinations; Tier 2 (LLM rescue) only fires when
        # confidence drops below 0.5, bounded by a verify-scope guardrail.
        if clean and open_snap_text:
            verify_gc = per_verify_scope(parent=dropdown_gc)
            try:
                verify_result = await verify_list_cascaded(
                    {"options": clean},
                    "options",
                    open_snap_text,
                    label=f"opts_{i+1}_cove",
                    guardrails=verify_gc,
                    log=False,
                )
                verified = verify_result.claim.get("options", clean)
                if verify_result.dropped:
                    print(
                        f"  [form]        🔍 CoVe dropped {len(verify_result.dropped)} "
                        f"hallucinated option(s): "
                        f"{[d['value'] for d in verify_result.dropped]}"
                    )
                clean = verified
            except GuardrailExit as e:
                print(f"  [form]        ⚠ CoVe guardrail hit ({e.reason}) — "
                      f"keeping raw options unverified")

        print(f"  [form]        → {len(clean)} options")
        if clean:
            for o in clean[:5]:
                print(f"  [form]          - {o!r}")
            if len(clean) > 5:
                print(f"  [form]          ... +{len(clean) - 5} more")

        if clean:
            if len(clean) > 10:
                _narrate(f"Got {len(clean)} options for '{label}' — it's a long list.")
            else:
                _narrate(f"Got {len(clean)} option(s) for '{label}'.")
        else:
            _narrate(f"'{label}' returned no options — recording as empty.")

        _dd_put(dropdown_data, label, trig.get("section", "") or "", {
            "options": clean,
            "disabled": False,
            "locator_css": locator_css,
            "locator_xpath": locator_xpath,
            "locator_strategy": locator_strategy,
            "dom_top": dom_top,
        })

        # Checkpoint AFTER dropdown_data is updated but BEFORE close —
        # on_progress sees the most up-to-date state; close is cleanup
        # and doesn't affect what we persist.
        await _checkpoint()

        # Fix B — if this dropdown was cascade-unlocked, commit a default
        # value on it so any grandchild that depends on this being filled
        # can unlock later. Without this, chains deeper than 2 levels
        # break: Sector unlocks but stays uncommitted, so Employment Type
        # (which needs Sector filled) never unlocks.
        if cascaded_unlocked and defaults is not None and clean:
            from qa.orchestrators.execute_flow import (
                _select_option as _exec_select_option,
                _close_popup as _exec_close_popup,
            )
            child_default = defaults.get(label)
            if child_default:
                # Validate the default is actually one of the captured
                # options (substring-insensitive). If the user's default
                # doesn't match, warn clearly instead of silently failing
                # on the click.
                match_opt = next(
                    (o for o in clean if child_default.strip().lower() in o.lower()),
                    None,
                )
                if match_opt is None:
                    print(f"  [form]        ⚠ commit skipped: default "
                          f"{child_default!r} not in captured options "
                          f"{clean[:4]!r}{'...' if len(clean) > 4 else ''}")
                else:
                    if locator_css:
                        commit_strategy, commit_value = "css", locator_css
                    elif locator_xpath:
                        commit_strategy, commit_value = "xpath", locator_xpath
                    else:
                        commit_strategy, commit_value = "", ""
                    if commit_strategy:
                        await _exec_close_popup(adapter)
                        commit_status, _ = await _exec_select_option(
                            adapter, commit_strategy, commit_value, child_default,
                        )
                        if commit_status == "SELECTED":
                            print(f"  [form]        ✓ committed {label!r}"
                                  f"={child_default!r} (cascade continuation)")
                        else:
                            print(f"  [form]        ⚠ commit {label!r}"
                                  f"={child_default!r} → {commit_status}")
            else:
                print(f"  [form]        ⚠ commit skipped: no default for {label!r} "
                      "(add to artifacts/defaults/<app>.json to enable chains)")

        # Close before moving to next trigger
        try:
            await server.call_tool("press_key", {"key": "Escape"})
        except Exception:
            pass
        await adapter.evaluate_script(_JS_CLOSE_POPUP)
        await asyncio.sleep(0.5)

    # ── Fix C: Second pass for still-disabled dropdowns ───────────
    # After the first sequential pass, a dropdown may still be disabled
    # because its cascade failed on a transient popup-state race (the
    # Employer-after-Employment-Status case we saw) or because its real
    # parent wasn't processed yet in DOM order. Now that dropdown_data
    # has locators / options / committed state for the whole page, retry
    # each still-disabled trigger one more time.
    # Wall 1.2 — iterate over (key, label) pairs, not bare labels. On
    # repeating sub-forms the same label can live under multiple keys
    # (e.g. "Country", "Country|beneficiary_2"); retrying by label alone
    # would collapse them.
    retry_items = [
        (key, _dd_label(info))
        for key, info in dropdown_data.items()
        if isinstance(info, dict) and info.get("disabled")
    ]
    if retry_items and defaults is not None:
        print(f"\n  [form] 4/4  retry: {len(retry_items)} still-disabled "
              f"dropdown(s)")
        for key, lbl in retry_items:
            retry_trig = next((t for t in triggers if t.get("label") == lbl), None)
            if retry_trig is None:
                continue
            uid = retry_trig["uid"]
            trigger_text = retry_trig["trigger_text"]
            info = dropdown_data[key]
            print(f"  [form]      retry {lbl!r} (uid={uid})")

            # Clean popup state before attempting cascade again.
            await adapter.evaluate_script(_JS_CLOSE_POPUP)
            await asyncio.sleep(0.3)

            try:
                page_gc.check()
            except GuardrailExit as e:
                print(f"  [form]        ✗ page guardrail hit ({e.reason}) "
                      "— skipping remaining retries")
                break

            eid = make_element_id(
                screen_name, lbl, "dropdown",
                section=info.get("section", "") or "",
            )
            try:
                attempted, unlock_notes = await _try_cascade_unlock_for_extract(
                    adapter=adapter,
                    child_eid=eid,
                    dropdown_data=dropdown_data,
                    js_elements=js_elements,
                    screen_name=screen_name,
                    defaults=defaults,
                )
            except Exception as e:
                print(f"  [form]        ⚠ retry cascade raised: {e}")
                continue
            for n in unlock_notes:
                print(f"  [form]          unlock: {n}")
            if not attempted:
                continue

            disabled_raw2 = await adapter.evaluate_script(
                _js_is_disabled_for(trigger_text)
            )
            still_disabled = bool(
                (_safe_parse(disabled_raw2) or {}).get("disabled", False)
            )
            if still_disabled:
                print(f"  [form]        still disabled after retry — "
                      "leaving as empty")
                continue

            # Unlocked — capture options via the same LLM flow as the
            # main loop, guarded by its own per-dropdown scope.
            retry_gc = per_dropdown_scope(parent=page_gc)
            try:
                await server.call_tool("click", {"uid": uid})
                await asyncio.sleep(1.5)
                open_snap = await server.call_tool("take_snapshot", {})
                snap_text = ""
                if open_snap.content:
                    snap_text = open_snap.content[0].text or ""
                result = await llm_classify(
                    EXTRACT_DROPDOWN_OPTIONS_PROMPT,
                    snap_text,
                    EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
                    budget=budget,
                    label=f"retry_{lbl[:22]}",
                    guardrails=retry_gc,
                )
                retry_options = result.get("options", []) or []
            except GuardrailExit as e:
                print(f"  [form]        ⚠ retry guardrail ({e.reason})")
                continue
            except Exception as e:
                print(f"  [form]        ⚠ retry read failed: {e}")
                continue

            retry_seen: set[str] = set()
            retry_clean: list[str] = []
            for o in retry_options:
                o = str(o).strip()
                if (o and o not in retry_seen and not re.match(
                        r"^(Select|Choose|--|Please)", o, re.IGNORECASE)):
                    retry_seen.add(o)
                    retry_clean.append(o)

            if not retry_clean:
                print(f"  [form]        retry read 0 options — leaving as empty")
                continue

            print(f"  [form]        ✓ retry → {len(retry_clean)} options captured")
            for o in retry_clean[:3]:
                print(f"  [form]          - {o!r}")

            # Update in place under the same key so reveal/section tagging
            # is preserved; label + section inside info remain untouched.
            dropdown_data[key] = {
                **info,
                "options": retry_clean,
                "disabled": False,
            }

            # Commit default on this child (same logic as first-pass commit)
            child_default = defaults.get(lbl)
            if child_default and any(
                child_default.strip().lower() in o.lower() for o in retry_clean
            ):
                from qa.orchestrators.execute_flow import (
                    _select_option as _exec_select_option,
                    _close_popup as _exec_close_popup,
                )
                await _exec_close_popup(adapter)
                css = info.get("locator_css") or ""
                xp = info.get("locator_xpath") or ""
                if css:
                    cs, cv = "css", css
                elif xp:
                    cs, cv = "xpath", xp
                else:
                    cs, cv = "", ""
                if cs:
                    cstat, _ = await _exec_select_option(adapter, cs, cv, child_default)
                    if cstat == "SELECTED":
                        print(f"  [form]        ✓ committed {lbl!r}"
                              f"={child_default!r}")

            try:
                await server.call_tool("press_key", {"key": "Escape"})
            except Exception:
                pass
            await adapter.evaluate_script(_JS_CLOSE_POPUP)
            await asyncio.sleep(0.3)
            await _checkpoint()

    # ── Wall 1.6: conditional UI two-pass ─────────────────────────
    # After the normal extract has captured everything visible, flip any
    # reveal-trigger radios ("Would you like to add Beneficiary? Yes/No")
    # to Yes, diff the page, and capture the newly-visible fields. Radios
    # are left on Yes — Execute needs the revealed fields addressable;
    # per-test field restore is handled downstream by Wall 1.3.
    _narrate("Checking for conditional sections that only reveal after a toggle...")
    try:
        await _reveal_and_reextract(
            adapter,
            budget=budget,
            page_gc=page_gc,
            js_elements=js_elements,
            triggers=triggers,
            dropdown_data=dropdown_data,
            checkpoint_fn=_checkpoint,
        )
    except GuardrailExit as e:
        print(f"  [form] 1.6  reveal pass aborted ({e.reason})")
    except Exception as e:
        print(f"  [form] 1.6  reveal pass errored: {e} — continuing")

    elapsed = time.time() - t0
    print(f"\n  [form] ✓ Done in {elapsed:.1f}s — "
          f"{len(js_elements)} standard + {len(triggers)} custom dropdowns")
    print(f"  [form] Cost: ${budget.current_cost:.4f}")
    _narrate(
        f"Extraction complete in {elapsed:.0f}s. "
        f"Let me verify I captured everything..."
    )

    screen = _build_screen(
        screen_name=screen_name,
        page_url=page_url,
        js_elements=js_elements,
        dropdown_data=dropdown_data,
    )

    # Completeness reconciliation — scan count vs extract count, with a
    # list of missed labels so the operator can see which items fell
    # through. Legitimate drops (empty label, date-picker sub-pieces)
    # are filtered out so the miss list is actionable.
    scanned = len(js_elements) + len(triggers)
    extracted = len(screen.l0)
    if scanned == 0:
        print(f"  [form] ⚠ Completeness: nothing scanned on this page")
        _narrate("I didn't find any interactive elements on this page. Nothing to capture.")
    elif extracted >= scanned:
        print(f"  [form] ✓ Completeness: {extracted}/{scanned} elements captured (100%)")
        _narrate(
            f"All {extracted}/{scanned} elements captured. "
            f"'{screen_name}' is ready for test planning."
        )
    else:
        l0_labels = {el.name.lower() for el in screen.l0}
        missed: list[str] = []
        for je in js_elements:
            raw_label = (je.get("label") or "").strip()
            if not raw_label:
                continue
            cleaned = _clean_label(raw_label)
            if not cleaned or cleaned.lower() in _DATE_PICKER_LABELS:
                continue
            if cleaned.lower() not in l0_labels:
                missed.append(f'"{cleaned}" ({je.get("type", "?")})')
        pct = extracted * 100 // scanned
        print(f"  [form] ⚠ Completeness: {extracted}/{scanned} elements captured ({pct}%)")
        if missed:
            shown = missed[:8]
            print(f"  [form]   Missed: {', '.join(shown)}"
                  + (f"  ... +{len(missed) - 8} more" if len(missed) > 8 else ""))
            _narrate(
                f"Captured {extracted} of {scanned} elements ({pct}%). "
                f"Missing: {', '.join(shown)}"
                + (f" and {len(missed) - 8} more" if len(missed) > 8 else "")
                + ". These weren't interactive enough to map — moving on."
            )
        else:
            _narrate(
                f"Captured {extracted} of {scanned} elements ({pct}%). "
                "A few were filtered as non-testable labels."
            )

    return screen


# ── CLI entry point ─────────────────────────────────────────────────

async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Hybrid form extractor — Python drives, LLM extracts options.",
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", required=True, help="App name (e.g. TECU)")
    ap.add_argument(
        "--screen-name", default="",
        help="Screen name for KB. If omitted, uses the page <title>.",
    )
    ap.add_argument(
        "--wait", action="store_true",
        help="Pause after browser launch for manual navigation",
    )
    ap.add_argument(
        "--loop", action="store_true",
        help=(
            "Extract multiple sections in one Chrome session. After each "
            "extract, prompts you to navigate to the next section and press "
            "Enter (or 'q' to quit). Each section is saved as its own screen "
            "in the KB. Useful for multi-step wizards where Chrome state "
            "(login, mandatory fields filled, current section) must persist "
            "across extractions."
        ),
    )
    ap.add_argument(
        "--gated", action="store_true",
        help=(
            "Autonomous gated multi-section mode. Dispatches to "
            "GatedMultiSectionFlow: detects section tabs, then loops each "
            "one (click tab → pick dropdown option → upload file → wait for "
            "OCR → extract auto-filled fields). Designed for KYC-style "
            "document-upload pages (e.g. TECU page 2). Mutex with --loop."
        ),
    )
    ap.add_argument(
        "--wizard", action="store_true",
        help=(
            "Autonomous multi-page wizard mode. After extracting the first "
            "page, fills its fields from artifacts/defaults/<app>.json, "
            "clicks 'Save & Continue' (or similar), waits for the page to "
            "transition, then extracts the next page. Repeats up to "
            "--max-pages. Stops cleanly if no nav button is found, no "
            "page transition is detected, or extract returns 0 elements. "
            "Mutex with --loop and --gated."
        ),
    )
    ap.add_argument(
        "--max-pages", type=int, default=6,
        help="Max pages for --wizard mode (default 6, matches TECU)",
    )
    # --randomize is DEFAULT-ON in --wizard mode (apps with unique-account
    # constraints reject re-runs with the same email). Use --no-randomize
    # to opt out for reproducibility. Outside of wizard mode the default
    # is OFF since other modes don't submit forms.
    rand_grp = ap.add_mutually_exclusive_group()
    rand_grp.add_argument(
        "--randomize", dest="randomize", action="store_true", default=None,
        help=(
            "Force PII randomization (name/email/phone) for this run. "
            "Default in --wizard mode. The override is in-memory only — "
            "artifacts/defaults/<app>.json is not modified."
        ),
    )
    rand_grp.add_argument(
        "--no-randomize", dest="randomize", action="store_false",
        help="Disable PII randomization even in --wizard mode (use static defaults).",
    )
    ap.add_argument("--model", default="gpt-5.1", help="Model for option extraction")
    ap.add_argument("--budget", type=float, default=0.50, help="Max $ budget cap")
    ap.add_argument(
        "--defaults", default="",
        help=(
            "Path to a JSON defaults file (valid values for fields). "
            "If omitted, loads artifacts/defaults/{app_name}.json if present. "
            "Used by downstream walls to unlock dependent fields and drive "
            "execute-phase setup. Loaded here to fail fast on bad files."
        ),
    )
    args = ap.parse_args()

    mutex_flags = sum(int(bool(f)) for f in (args.loop, args.gated, args.wizard))
    if mutex_flags > 1:
        print("  ERROR: --loop, --gated, --wizard are mutually exclusive. Pick one.")
        return 2

    app = TargetApp(platform=Platform.WEB, url=args.url, app_name=args.app_name)

    # Load user-provided defaults early so a bad file fails BEFORE we
    # launch Chrome. Consumed by future walls (1.1 dependent dropdown
    # chaining, 0.1 ExecuteOrchestrator setup phase).
    from qa.config import load_defaults
    defaults_path = args.defaults.strip() or None
    defaults = load_defaults(args.app_name, path=defaults_path)
    print(f"  {defaults.summary()}")

    # Resolve the tristate randomize flag:
    #   args.randomize is True  → user passed --randomize
    #   args.randomize is False → user passed --no-randomize
    #   args.randomize is None  → user passed neither — default ON for wizard
    if args.randomize is None:
        randomize_on = bool(args.wizard)
    else:
        randomize_on = bool(args.randomize)

    if randomize_on:
        from qa.config.defaults import randomize_pii
        applied = randomize_pii(defaults)
        print()
        print("=" * 60)
        if applied:
            print(f"  ★ RANDOMIZED {len(applied)} PII default(s) for this run:")
            for k, v in applied.items():
                print(f"      {k!r:20} → {v!r}")
        else:
            print("  ★ --randomize requested but no PII keys present in defaults")
        print("=" * 60)
    elif args.wizard:
        print()
        print("  ⚠ --no-randomize: wizard will use STATIC defaults this run")
        print()

    store = KnowledgeStore()
    kb = store.load(app) or KnowledgeBase(app=app)
    if kb.screens:
        print(f"  Loaded KB with {len(kb.screens)} screen(s): "
              f"{[s.screen_name for s in kb.screens]}")

    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    if args.wait:
        print()
        print("=" * 60)
        print("  PAUSE: navigate to the page you want to extract.")
        print("  Press Enter when the form is fully visible.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to extract... ")
        except EOFError:
            pass

    # ── Gated dispatch ───────────────────────────────────────────────────
    # --gated short-circuits the normal DOM-scan extract loop. We hand off
    # to GatedMultiSectionFlow which detects section tabs, then iterates
    # each one (click → dropdown → upload → OCR → snapshot post-OCR fields).
    # The flow already merges captured screens into ctx.knowledge and
    # checkpoints to disk after each section, so we just report afterwards.
    if args.gated:
        available = discover_test_files(args.app_name)
        print(f"  [gated] available test files: {available}")
        if not available:
            print(
                f"  [gated] ⚠ no test files in artifacts/test_files/"
                f"{args.app_name.lower()}/ or artifacts/test_files/global/. "
                "Drop documents (passport.png etc.) there before running."
            )
            await adapter.close()
            return 2

        budget = BudgetTracker(model=args.model, max_budget=args.budget)
        inp = ExploreInput(app=app, model=args.model, budget=args.budget)
        ctx = RunContext(
            adapter=adapter,
            inp=inp,
            knowledge=kb,
            budget=budget,
            available_files=available,
        )
        flow = GatedMultiSectionFlow()
        status = 0
        captured: list[ScreenKnowledge] = []
        try:
            captured = await flow.run(ctx)
        except SectionFailed as e:
            print(f"\n  [gated] ✗ SectionFailed: {e}")
            print("  [gated] Prior sections were saved before the failure.")
            status = 1
        except Exception as e:
            print(f"\n  [gated] ✗ Unhandled error: {type(e).__name__}: {e}")
            status = 2
        finally:
            await adapter.close()

        final = store.load(app)
        if final and final.screens:
            print(f"\n  ── Sections in saved KB ──")
            captured_names = {c.screen_name for c in captured}
            for s in final.screens:
                tag = "✓" if s.screen_name in captured_names else " "
                print(f"  {tag} {s.screen_name} ({len(s.l0)} elements)")
        print(f"\n  Final cost: ${budget.current_cost:.4f}")
        return status

    # Checkpoint callback: after every meaningful step inside extract_form
    # (step 1 finished, each dropdown captured), persist a partial KB so a
    # mid-loop crash loses at most the current dropdown in progress.
    # Uses save_checkpoint which is atomic (temp-file + rename) and does
    # NOT archive — so 100 checkpoints don't pollute history/.
    async def _checkpoint_kb(partial_screen: ScreenKnowledge) -> None:
        existing = kb.get_screen(partial_screen.screen_name)
        if existing:
            kb.screens = [
                s for s in kb.screens if s.screen_name != partial_screen.screen_name
            ]
        kb.screens.append(partial_screen)
        store.save_checkpoint(kb)

    # Multi-section loop: when --loop is set we keep Chrome alive across
    # extractions so wizard state (login, filled mandatory fields, current
    # section) persists between captures. Each iteration adds one screen
    # to the KB. User presses Enter to continue, 'q' to quit.
    extracted: list[tuple[str, int]] = []
    section_num = 1
    # Circuit breaker for wizard mode: counts consecutive
    # SAME_PAGE_WITH_EXPANSION verdicts. If we accumulate too many
    # without a NEW_PAGE in between, the wizard is in a loop (TECU's
    # email OTP keeps re-triggering on duplicate users, etc.). Stop
    # with a clear message instead of churning forever.
    expansion_streak = 0
    EXPANSION_STREAK_LIMIT = 3

    try:
        while True:
            # Resolve screen_name for this iteration.
            if section_num == 1:
                # First pass — explicit --screen-name takes priority,
                # otherwise derive from document.title, otherwise fall back.
                screen_name = args.screen_name
                if not screen_name:
                    title_raw = await adapter.evaluate_script("() => document.title")
                    parsed = _safe_parse(title_raw)
                    screen_name = (str(parsed) if parsed else "").strip()
                    # Fallback: if <title> is empty/generic, use the visible
                    # main heading. Many SPAs leave <title> blank and put
                    # the page name in an <h1>/<h2>. Avoids landing every
                    # extract on the same "Extracted Form" name and breaking
                    # the section-qualified defaults lookup.
                    if not screen_name or screen_name.lower() in (
                        "extracted form", "react app", "vue app", "loading",
                    ):
                        h_raw = await adapter.evaluate_script(
                            "() => { const h = document.querySelector("
                            "'h1, h2, [role=heading]'); "
                            "return h ? (h.textContent || '').trim() : ''; }"
                        )
                        h_parsed = _safe_parse(h_raw)
                        h_text = (str(h_parsed) if h_parsed else "").strip()
                        if h_text:
                            screen_name = h_text
                    if not screen_name:
                        screen_name = "Extracted Form"
                    print(f"  Screen name: {screen_name!r}")
            elif args.wizard:
                # Autonomous wizard mode — derive screen_name from the
                # document. User isn't at the keyboard; we want a stable
                # auto-generated name so the KB grows cleanly.
                title_raw = await adapter.evaluate_script("() => document.title")
                parsed = _safe_parse(title_raw)
                screen_name = (str(parsed) if parsed else "").strip()
                if not screen_name:
                    h_raw = await adapter.evaluate_script(
                        "() => { const h = document.querySelector('h1, h2'); "
                        "return h ? (h.textContent || '').trim() : ''; }"
                    )
                    screen_name = (str(_safe_parse(h_raw) or "")).strip()
                if not screen_name:
                    screen_name = f"Page {section_num}"
                print(f"  [wizard] page {section_num} screen_name: {screen_name!r}")
            else:
                # Manual --loop iteration — prompt user for a section name.
                # Show existing KB screen names so they can re-extract a
                # known screen (overwrite-in-place) rather than accidentally
                # forking a new "Section N" entry that diverges from any
                # cascade-dependency keys already declared in defaults.
                existing_names = [s.screen_name for s in kb.screens]
                if existing_names:
                    print()
                    print(f"  Existing screens in this KB ({len(existing_names)}):")
                    for i, n in enumerate(existing_names, 1):
                        print(f"    {i}. {n}")
                    print(
                        "  Tip: type one of those names exactly to UPDATE "
                        "(overwrite) it. A new name CREATES a new screen."
                    )
                default_name = f"Section {section_num}"
                try:
                    user_name = input(
                        f"  Section name [default: {default_name}]: "
                    ).strip()
                except EOFError:
                    user_name = ""
                screen_name = user_name or default_name

            budget = BudgetTracker(model=args.model, max_budget=args.budget)

            screen = await extract_form(
                adapter=adapter,
                app_name=args.app_name,
                screen_name=screen_name,
                budget=budget,
                page_url=args.url,
                on_progress=_checkpoint_kb,
                defaults=defaults,
            )

            # ── LLM field tagger (wizard mode, page ≥ 2) ──────────
            # Many SPAs keep prior-page fields rendered (header summary,
            # persistent sidebar, sticky review panel). The DOM-walk
            # extract picks them all up, polluting the per-page L0
            # with carryovers. Ask the LLM which fields are NEW to
            # this screen vs. CARRYOVER from earlier pages, and drop
            # the carryovers from the screen we save. Only runs on
            # iteration 2+ where we have prior-page fields to compare.
            if args.wizard and section_num > 1 and kb.screens:
                from qa.orchestrators.wizard_steps import tag_fields_new_vs_carryover
                prior_names = [
                    el.name for s in kb.screens for el in s.l0
                    if s.screen_name != screen.screen_name and el.name
                ]
                current_names = [el.name for el in screen.l0]
                tags = await tag_fields_new_vs_carryover(
                    adapter, current_names, prior_names, budget=budget,
                )
                carryovers = [n for n, t in tags.items() if t == "CARRYOVER"]
                if carryovers:
                    print(
                        f"  [wizard] LLM tagged {len(carryovers)}/"
                        f"{len(current_names)} field(s) as CARRYOVER — "
                        f"dropping from {screen.screen_name!r}'s L0:"
                    )
                    for n in carryovers[:8]:
                        print(f"  [wizard]   · {n}")
                    if len(carryovers) > 8:
                        print(f"  [wizard]   …and {len(carryovers) - 8} more")
                    keep_names = {n for n, t in tags.items() if t == "NEW"}
                    screen.l0 = [el for el in screen.l0 if el.name in keep_names]
                    # Same filter on L1 so locators don't leak.
                    keep_ids = {el.element_id for el in screen.l0}
                    screen.l1 = [l1 for l1 in screen.l1 if l1.element_id in keep_ids]

            # Final save for this section. form_extract's _build_screen
            # already deduped/merged at the L0 level; here we replace the
            # whole screen entry in the KB by name so a re-extract of the
            # same screen overwrites cleanly.
            existing = kb.get_screen(screen.screen_name)
            if existing:
                kb.screens = [
                    s for s in kb.screens if s.screen_name != screen.screen_name
                ]
            kb.screens.append(screen)
            path = store.save(kb)

            print(f"\n  KB saved: {path}")
            print(f"  Screen: {screen.screen_name}")
            print(f"  Elements: {len(screen.l0)}")
            for el in screen.l0:
                req = " *" if el.required else ""
                opts = f" [{len(el.options)} opts]" if el.options else ""
                print(f"    - {el.name!r}{req} [{el.type.value}]{opts}")

            extracted.append((screen.screen_name, len(screen.l0)))

            if args.wizard:
                # Autonomous advance: fill defaults → click nav → wait for
                # transition → loop. If any step fails we stop cleanly so
                # the partial KB up to here is preserved.
                if section_num >= args.max_pages:
                    print(f"\n  [wizard] reached --max-pages={args.max_pages} — stopping")
                    break
                if not screen.l0:
                    print(f"\n  [wizard] page {section_num} extracted 0 elements — stopping")
                    break

                from qa.orchestrators.wizard_steps import (
                    click_save_and_continue,
                    fill_page_from_defaults,
                    page_signature,
                    wait_for_page_transition,
                )

                print(f"\n  [wizard] ── advancing from page {section_num} ──")
                filled, skipped = await fill_page_from_defaults(
                    adapter, kb, defaults, screen,
                )
                print(f"  [wizard] filled {len(filled)} field(s):")
                for name, note in filled[:10]:
                    print(f"  [wizard]   ✓ {name}: {note}")
                if len(filled) > 10:
                    print(f"  [wizard]   …and {len(filled) - 10} more")
                if skipped:
                    print(f"  [wizard] skipped {len(skipped)} field(s):")
                    for name, note in skipped[:5]:
                        print(f"  [wizard]   · {name}: {note}")
                    if len(skipped) > 5:
                        print(f"  [wizard]   …and {len(skipped) - 5} more")

                # Required-field precheck: don't click Save & Continue if
                # any required (*) field failed to fill — the form will
                # just reject submission with a validation error and
                # transition will time out. Better to stop here with a
                # clear list of what's missing than burn 12s on a doomed
                # click. Match by element name (skipped tuple's first
                # entry) against the L0's `required` flag.
                required_misses: list[tuple[str, str]] = []
                filled_names = {n for n, _ in filled}
                for el in screen.l0:
                    if not getattr(el, "required", False):
                        continue
                    if el.name in filled_names:
                        continue
                    note = next(
                        (n for fname, n in skipped if fname == el.name),
                        "not filled (no entry in fill report)",
                    )
                    required_misses.append((el.name, note))

                if required_misses:
                    print(
                        f"\n  [wizard] ✗ {len(required_misses)} required "
                        f"field(s) not filled — refusing to click Save & "
                        f"Continue (would fail validation):"
                    )
                    for name, note in required_misses:
                        print(f"  [wizard]   ✗ {name}: {note}")
                    print(
                        f"  [wizard] stopping. Fix defaults / locators and "
                        f"re-run, or fill these fields manually before next attempt."
                    )
                    break

                before = await page_signature(adapter)
                # Snapshot before the click — fed to the LLM transition
                # classifier later so it can compare DOM state pre/post
                # without a second round trip.
                before_snap = await adapter.raw_snapshot_text()
                print(f"  [wizard] looking for nav button on page {section_num}...")
                clicked, label = await click_save_and_continue(adapter)
                if not clicked:
                    print(
                        f"  [wizard] no Save & Continue / Next button found — "
                        f"stopping (captured {section_num} page(s))"
                    )
                    break
                print(f"  [wizard] clicked {label!r} — waiting for transition")

                transitioned, signal = await wait_for_page_transition(
                    adapter, before, timeout=12.0,
                )
                if not transitioned:
                    print(
                        f"  [wizard] no page transition detected ({signal}) — "
                        f"app may have shown a validation error. Stopping."
                    )
                    break
                print(f"  [wizard] transitioned ({signal}) — classifying with LLM")

                # ── LLM transition classifier ──────────────────────
                # The deterministic transition check above is easily
                # fooled (inline error toast inflates body length, OTP
                # box reveal changes heading, etc.). Ask the LLM to
                # judge: did we actually advance, or is this a same-
                # page re-render? On SAME_PAGE_WITH_ERROR, stop and
                # report — saving a fake page to KB is worse than
                # missing one. On SAME_PAGE_WITH_EXPANSION, treat the
                # current screen as still the same page (re-extract
                # will pick up the expanded section).
                from qa.orchestrators.wizard_steps import classify_transition
                verdict, reasoning, error_text = await classify_transition(
                    adapter, before_snap, budget=budget,
                )
                print(f"  [wizard] verdict: {verdict} — {reasoning}")
                if verdict == "SAME_PAGE_WITH_ERROR":
                    if error_text:
                        print(f"  [wizard] error from page: {error_text!r}")
                    print(
                        f"  [wizard] stopping — app rejected the click. "
                        f"Captured {section_num} valid page(s)."
                    )
                    break
                if verdict == "SAME_PAGE_WITH_EXPANSION":
                    expansion_streak += 1
                    if expansion_streak > EXPANSION_STREAK_LIMIT:
                        print(
                            f"  [wizard] ✗ {expansion_streak} consecutive "
                            f"SAME_PAGE_WITH_EXPANSION verdicts — wizard is "
                            f"stuck in a loop."
                        )
                        print(
                            f"  [wizard] Likely cause: the app keeps re-"
                            f"triggering an inline step (e.g. TECU re-shows "
                            f"OTP on duplicate users)."
                        )
                        print(
                            f"  [wizard] Try re-running with --randomize "
                            f"so each run uses a fresh email + mobile, OR "
                            f"check the browser for an error TECU isn't "
                            f"surfacing in the snapshot."
                        )
                        break
                    print(
                        f"  [wizard] same page expanded ({expansion_streak}/"
                        f"{EXPANSION_STREAK_LIMIT}) — re-extracting in place"
                    )
                    # Re-extract the same screen_name; do not advance
                    # section_num so the expanded content overwrites.
                    await asyncio.sleep(1.0)
                    continue

                # NEW_PAGE — reset the expansion streak and advance
                expansion_streak = 0
                await asyncio.sleep(1.5)
                section_num += 1
                continue

            if not args.loop:
                break

            # Manual --loop: prompt user to advance. Keep Chrome open between sections.
            print()
            print("=" * 60)
            print("  Section captured. To advance:")
            print("    1. Fill any required fields on the current section")
            print("    2. Upload any required documents (extract does NOT")
            print("       upload files — do this manually in Chrome)")
            print("    3. Click 'Save & Continue' / 'Next' yourself to")
            print("       advance to the next section")
            print("    4. When the next section is fully visible here in")
            print("       Chrome, return to this terminal and press Enter")
            print("=" * 60)
            try:
                choice = input(
                    "  >>> Press Enter to extract next, or 'q' + Enter to quit: "
                ).strip().lower()
            except EOFError:
                break
            if choice == "q":
                break
            section_num += 1
    finally:
        await adapter.close()

    if len(extracted) > 1:
        print(f"\n  Extracted {len(extracted)} section(s) in this run:")
        for name, n in extracted:
            print(f"    - {name}: {n} elements")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
