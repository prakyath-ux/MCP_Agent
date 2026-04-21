# qa/orchestrators/app_probe.py — Wall 2.0: App Selector DNA probe
#
# Learns a web app's DOM shape patterns in a single pass:
#   1. JS scans the currently-loaded page and gathers structural evidence
#      about dropdowns, inputs, radios, date pickers, and file uploads.
#   2. An LLM sub-task interprets the evidence and produces an
#      AppFingerprint with concrete CSS selectors.
#   3. Python validates each generated selector via querySelectorAll and
#      downgrades confidence for selectors that match 0 or >N elements.
#   4. The fingerprint is persisted to artifacts/knowledge/web/<app>.meta.json.
#
# form_extract.py calls probe_app() on first extract (no meta.json yet),
# then uses the fingerprint as strategy-0 before falling back to the
# hand-written 4 strategies (tecu_label_for / mui_formcontrol / p_tag /
# generic_text_match).

import json
import re
import time

from qa.engine.budget import BudgetTracker
from qa.engine.guardrails import GuardrailContext, GuardrailExit, per_page_scope
from qa.knowledge.fingerprint_store import FingerprintStore
from qa.models.app_fingerprint import (
    AppFingerprint, DropdownFingerprint, TextInputFingerprint,
    DatePickerFingerprint, RadioFingerprint, FileUploadFingerprint,
)
from qa.orchestrators.llm_subtask import llm_classify
from qa.tools.web_tools import _safe_parse


# ── JS: gather DOM evidence for the probe ───────────────────────────
#
# Returns a JSON string with up to 5-10 samples per category so the LLM
# sees enough variety to spot the repeated pattern but not so much that
# the prompt blows up. Every sample includes tag, class (truncated),
# role, id, data-testid, aria-label, and a text-content excerpt.
#
# Visibility filter: offsetParent !== null, with <dialog> content allowed
# via closest('[role=dialog]') to handle modal-wrapped forms.

_JS_GATHER_EVIDENCE = r"""() => {
  const MAX_SAMPLES = 10;

  function isVisible(el) {
    return el && (el.offsetParent !== null || el.closest('[role="dialog"]'));
  }

  // Shortened class list — drop Tailwind utility classes that add noise
  // (w-*, h-*, p-*, m-*, flex-*, text-*, bg-*) and keep semantic class
  // names. Preserves the "oxd-" / "Mui-" / "data-" prefixes which ARE
  // the app's selector DNA.
  function shortClasses(cls) {
    if (!cls) return '';
    const TAILWIND = /^(w|h|p|m|flex|text|bg|border|rounded|shadow|space|gap|items|justify)-/;
    return cls.split(/\s+/)
      .filter(c => c && !TAILWIND.test(c))
      .slice(0, 8)
      .join(' ');
  }

  function elementSummary(el) {
    return {
      tag: el.tagName.toLowerCase(),
      cls: shortClasses(el.className || ''),
      id: el.id || '',
      testid: el.getAttribute('data-testid') || '',
      role: el.getAttribute('role') || '',
      arialabel: el.getAttribute('aria-label') || '',
      type: el.getAttribute('type') || '',
      placeholder: el.placeholder || '',
      name: el.name || '',
      text: (el.textContent || '').trim().slice(0, 60),
    };
  }

  function domPath(el, maxDepth) {
    maxDepth = maxDepth || 4;
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node !== document.body && depth < maxDepth) {
      const tag = node.tagName.toLowerCase();
      const cls = shortClasses(node.className || '');
      parts.unshift(cls ? tag + '.' + cls.split(' ')[0] : tag);
      node = node.parentElement;
      depth++;
    }
    return parts.join(' > ');
  }

  // ── 1. Clickable widgets (dropdown trigger candidates) ──────────
  const clickables = [];
  const CLICKABLE_SEL = [
    'button',
    'select',
    '[role=combobox]',
    '[role=button]',
    '[role=listbox]',
    // Framework-specific trigger-y divs
    '[class*="select-text"]',
    '[class*="select-trigger"]',
    '[class*="dropdown-trigger"]',
    '[class*="MuiSelect"]',
    '[class*="oxd-select"]',
  ].join(', ');
  const seenClickable = new Set();
  document.querySelectorAll(CLICKABLE_SEL).forEach(el => {
    if (!isVisible(el)) return;
    // Skip obvious non-dropdowns
    const t = (el.textContent || '').trim().toLowerCase();
    const NON_TRIGGER = ['save', 'continue', 'submit', 'cancel', 'close',
      'next', 'back', 'yes', 'no', 'ok', 'log in', 'logout', 'sign in',
      'search', 'reset', 'edit'];
    if (NON_TRIGGER.includes(t)) return;
    // Dedupe by structural fingerprint — we only need unique shapes
    const fp = el.tagName + '|' + shortClasses(el.className || '') + '|'
      + (el.getAttribute('role') || '');
    if (seenClickable.has(fp)) return;
    seenClickable.add(fp);
    if (clickables.length >= MAX_SAMPLES) return;
    const summary = elementSummary(el);
    summary.dom_path = domPath(el, 4);
    // Also capture the nearest visible preceding text — likely the label
    let sibling_text = '';
    let walker = el;
    for (let depth = 0; depth < 4 && !sibling_text; depth++) {
      const prev = walker.previousElementSibling;
      if (prev && !prev.querySelector('button, input, select')) {
        const tt = (prev.textContent || '').trim();
        if (tt && tt.length < 80) sibling_text = tt;
      }
      walker = walker.parentElement;
      if (!walker) break;
    }
    summary.preceding_label_text = sibling_text;
    clickables.push(summary);
  });

  // ── 2. Text inputs + label pairs ────────────────────────────────
  const inputs = [];
  document.querySelectorAll('input:not([type=radio]):not([type=checkbox]):not([type=file]):not([type=hidden]):not([type=submit]):not([type=button]), textarea').forEach(el => {
    if (!isVisible(el)) return;
    if (inputs.length >= MAX_SAMPLES) return;

    // Strategy 1: label[for=el.id]
    let label_text = '';
    let label_binding = '';
    if (el.id) {
      const lbl = document.querySelector('label[for="' + el.id + '"]');
      if (lbl) { label_text = lbl.textContent.trim(); label_binding = 'label_for'; }
    }
    // Strategy 2: closest label ancestor
    if (!label_text) {
      const parentLabel = el.closest('label');
      if (parentLabel) {
        const clone = parentLabel.cloneNode(true);
        clone.querySelectorAll('input,select,textarea,button').forEach(c => c.remove());
        label_text = clone.textContent.trim();
        label_binding = 'ancestor:label';
      }
    }
    // Strategy 3: common wrapper ancestor (MUI / Oxd / Bootstrap)
    if (!label_text) {
      const WRAPPERS = ['.MuiFormControl-root', '.oxd-input-group',
        '.form-group', '.field-wrapper', '.form-control', '.oxd-input-field'];
      for (const sel of WRAPPERS) {
        const wrapper = el.closest(sel);
        if (wrapper) {
          const lbl = wrapper.querySelector(
            'label, .oxd-label, .MuiFormLabel-root, .MuiInputLabel-root, legend'
          );
          if (lbl) {
            label_text = lbl.textContent.trim();
            label_binding = 'ancestor:' + sel + ' child:label-ish';
            break;
          }
        }
      }
    }
    // Strategy 4: preceding sibling text node
    if (!label_text) {
      let walker = el.parentElement;
      for (let depth = 0; depth < 4 && !label_text; depth++) {
        if (!walker) break;
        const prev = walker.previousElementSibling;
        if (prev && !prev.querySelector('input, button, select')) {
          const tt = (prev.textContent || '').trim();
          if (tt && tt.length < 80) {
            label_text = tt;
            label_binding = 'sibling_prev';
          }
        }
        walker = walker.parentElement;
      }
    }

    inputs.push({
      input: elementSummary(el),
      dom_path: domPath(el, 4),
      label_text: label_text.slice(0, 80),
      label_binding_observed: label_binding,
    });
  });

  // ── 3. Radio groups ─────────────────────────────────────────────
  const radioGroups = {};
  document.querySelectorAll('input[type=radio]').forEach(el => {
    if (!isVisible(el)) return;
    const grp = el.name || el.id || '';
    if (!grp || radioGroups[grp]) return;
    // Find the wrapper
    const wrapper = el.closest('fieldset, [role="radiogroup"], .oxd-radio-wrapper, .MuiFormControl-root, .form-group') || el.parentElement;
    // Find a group label
    let group_label = '';
    if (wrapper) {
      const lbl = wrapper.querySelector('legend, .oxd-label, .MuiFormLabel-root, .MuiInputLabel-root');
      if (lbl) group_label = lbl.textContent.trim();
    }
    radioGroups[grp] = {
      group_name: grp,
      group_label: group_label.slice(0, 80),
      wrapper_tag: wrapper ? wrapper.tagName.toLowerCase() : '',
      wrapper_cls: wrapper ? shortClasses(wrapper.className || '') : '',
      radio_dom_path: domPath(el, 4),
    };
  });
  const radioSamples = Object.values(radioGroups).slice(0, MAX_SAMPLES);

  // ── 4. Date-picker candidates ───────────────────────────────────
  const dates = [];
  // Strategy A: <input type=date>
  document.querySelectorAll('input[type=date]').forEach(el => {
    if (!isVisible(el) || dates.length >= 5) return;
    dates.push({...elementSummary(el), dom_path: domPath(el, 4), detection: 'input_type_date'});
  });
  // Strategy B: text inputs with date-ish placeholder
  const DATE_PLACEHOLDER = /\b(yyyy|mm|dd|yy|mmm)\b/i;
  document.querySelectorAll('input[type=text], input:not([type])').forEach(el => {
    if (!isVisible(el) || dates.length >= 5) return;
    if (el.placeholder && DATE_PLACEHOLDER.test(el.placeholder)) {
      dates.push({...elementSummary(el), dom_path: domPath(el, 4), detection: 'placeholder_format'});
    }
  });
  // Strategy C: text input with a calendar-icon sibling button
  document.querySelectorAll('input').forEach(el => {
    if (!isVisible(el) || dates.length >= 5) return;
    const parent = el.parentElement;
    if (!parent) return;
    const icon = parent.querySelector('i, svg, button[class*="calendar"], [class*="calendar-icon"], [class*="date-icon"]');
    if (icon) {
      dates.push({...elementSummary(el), dom_path: domPath(el, 4), detection: 'sibling_icon'});
    }
  });

  // ── 5. File upload candidates ───────────────────────────────────
  const uploads = [];
  // Direct <input type=file>
  document.querySelectorAll('input[type=file]').forEach(el => {
    if (uploads.length >= 5) return;
    uploads.push({
      ...elementSummary(el),
      dom_path: domPath(el, 4),
      hidden: el.offsetParent === null,
      has_visible_trigger_nearby: !!el.closest('label, .oxd-file-input, .MuiDropzoneArea-root'),
    });
  });
  // Buttons with upload-related text (for custom pickers)
  if (uploads.length === 0) {
    const UPLOAD_TEXT = /^(upload|choose file|select file|browse|attach|pick file)/i;
    document.querySelectorAll('button, [role=button]').forEach(el => {
      if (!isVisible(el) || uploads.length >= 5) return;
      const t = (el.textContent || '').trim();
      if (UPLOAD_TEXT.test(t)) {
        uploads.push({
          ...elementSummary(el),
          dom_path: domPath(el, 4),
          kind: 'custom_trigger',
        });
      }
    });
  }

  // ── Metadata ────────────────────────────────────────────────────
  const totals = {
    inputs_total: document.querySelectorAll('input:not([type=hidden])').length,
    selects_total: document.querySelectorAll('select').length,
    buttons_total: document.querySelectorAll('button').length,
    mui_formcontrol_count: document.querySelectorAll('.MuiFormControl-root').length,
    oxd_input_group_count: document.querySelectorAll('.oxd-input-group').length,
    tecu_label_for_count: document.querySelectorAll('label[for]').length,
    data_testid_count: document.querySelectorAll('[data-testid]').length,
  };

  return JSON.stringify({
    url: location.href,
    title: (document.title || '').slice(0, 120),
    clickable_widgets: clickables,
    input_label_pairs: inputs,
    radio_groups: radioSamples,
    date_candidates: dates,
    upload_candidates: uploads,
    totals: totals,
  });
}"""


# ── LLM prompt: interpret evidence → produce fingerprint ─────────────
#
# Deliberately specific: asks for CSS selectors, not English. Rejects
# overly-broad fallbacks ("div", "button") via in-prompt examples.

APP_PROBE_PROMPT = """You are analyzing a web application's DOM structure to produce a reusable
"selector fingerprint" that describes HOW this app styles its form widgets.

You will be given JSON evidence collected from the live page: sample clickable
widgets, input+label pairs, radio groups, date-picker candidates, and file
upload candidates, each with tag, class names, roles, ARIA attributes, and
nearby text.

Your job is to produce CSS selectors that an automated extractor can use to
find this app's form widgets WITHOUT trial-and-error. The patterns should
generalize to other screens of the same app (not over-fit to one widget).

Rules:
- Prefer framework-signature class names (e.g. ".oxd-select-text",
  ".MuiSelect-root", '[data-testid="dropdown-button"]') over tag names.
- Reject selectors that would match every button or every div on the page.
- If evidence is ambiguous or sparse, leave that section's fields empty
  rather than guessing — downstream code will fall back to hand-written
  strategies for missing fingerprints.
- Confidence: 0.0 = no evidence, 0.5 = one weak signal, 0.8 = multiple
  consistent signals, 0.95 = obvious framework signature all over the page.

Common framework signatures:
- "oxd-vue"     → classes prefixed ".oxd-" (OrangeHRM, Oxd components)
- "mui-react"   → classes prefixed ".Mui-" or ".MuiXxx-" (Material UI)
- "tecu-html5"  → <label for="X"> binding, [data-testid="dropdown-button"]
- "bootstrap"   → ".dropdown-menu", ".form-control", ".form-group"
- "chakra"      → "[data-chakra-component]"
- "unknown"     → falls back to heuristics

Return ONLY valid JSON matching the schema. No prose, no markdown fences."""

APP_PROBE_SCHEMA = {
    "name": "app_fingerprint",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "framework_signature": {"type": "string"},
            "dropdown": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "trigger_selector": {"type": "string"},
                    "menu_selector": {"type": "string"},
                    "option_selector": {"type": "string"},
                    "label_binding": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": [
                    "trigger_selector", "menu_selector", "option_selector",
                    "label_binding", "notes",
                ],
            },
            "text_input": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label_binding": {"type": "string"},
                    "use_placeholder_as_label": {"type": "boolean"},
                    "require_id_or_name_or_placeholder": {"type": "boolean"},
                },
                "required": [
                    "label_binding", "use_placeholder_as_label",
                    "require_id_or_name_or_placeholder",
                ],
            },
            "date_picker": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "input_selector": {"type": "string"},
                    "detection_pattern": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["input_selector", "detection_pattern", "notes"],
            },
            "radio": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "wrapper_selector": {"type": "string"},
                    "option_label_selector": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": [
                    "wrapper_selector", "option_label_selector", "notes",
                ],
            },
            "file_upload": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "trigger_selector": {"type": "string"},
                    "requires_click_to_expose": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": [
                    "trigger_selector", "requires_click_to_expose", "notes",
                ],
            },
            "confidence": {"type": "number"},
            "evidence_seen": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "framework_signature", "dropdown", "text_input", "date_picker",
            "radio", "file_upload", "confidence", "evidence_seen",
        ],
    },
}


# ── Validation: sanity-check generated selectors against the live DOM ─
#
# An LLM-generated selector that matches zero elements (or matches
# every div on the page) isn't useful. We down-weight confidence when
# selectors fail these sanity checks rather than hard-rejecting them —
# a probe with some bad selectors is still better than no probe.

_JS_VALIDATE_SELECTOR = r"""(SELECTOR_LITERAL) => {
  try {
    const hits = document.querySelectorAll(SELECTOR_LITERAL);
    return JSON.stringify({ count: hits.length });
  } catch (e) {
    return JSON.stringify({ count: -1, error: String(e).slice(0, 100) });
  }
}"""


async def _count_selector_hits(adapter, selector: str) -> int:
    """Return querySelectorAll hit count for `selector`, or -1 on error
    (e.g. malformed CSS). Used to down-weight probe confidence when the
    LLM generates selectors that don't actually match anything."""
    if not selector:
        return 0
    # Chrome DevTools MCP evaluate_script expects a zero-arg arrow
    # function, so we template the selector literal into the closure.
    sel_literal = json.dumps(selector)
    js = f"() => {{ try {{ return JSON.stringify({{count: document.querySelectorAll({sel_literal}).length}}); }} catch (e) {{ return JSON.stringify({{count: -1, error: String(e).slice(0, 100)}}); }} }}"
    try:
        raw = await adapter.evaluate_script(js)
    except Exception:
        return -1
    data = _safe_parse(raw) or {}
    if not isinstance(data, dict):
        return -1
    count = data.get("count", -1)
    return int(count) if isinstance(count, (int, float)) else -1


async def _validate_and_adjust_confidence(
    adapter, fp: AppFingerprint,
) -> AppFingerprint:
    """Run querySelectorAll on each generated selector and nudge the
    fingerprint's confidence down for selectors that match 0 elements or
    too many. Does not delete bad selectors — form_extract will detect
    zero-hits at call time and fall back.
    """
    penalties = []

    # Dropdown trigger: should match at least 1 element on the probed
    # page (we saw clickables in the evidence), but probably not 100+.
    if fp.dropdown.trigger_selector:
        n = await _count_selector_hits(adapter, fp.dropdown.trigger_selector)
        if n == 0:
            penalties.append(f"dropdown.trigger_selector matched 0 (expected >=1)")
            fp.dropdown.trigger_selector = ""
        elif n > 50:
            penalties.append(f"dropdown.trigger_selector matched {n} — too broad")
            fp.dropdown.trigger_selector = ""

    # Menu/option selectors: at probe time the dropdown is NOT open, so
    # 0 hits is expected. Only penalize if the selector is malformed.
    for name, sel in (
        ("dropdown.menu_selector", fp.dropdown.menu_selector),
        ("dropdown.option_selector", fp.dropdown.option_selector),
    ):
        if sel:
            n = await _count_selector_hits(adapter, sel)
            if n == -1:
                penalties.append(f"{name} malformed")

    # Radio wrapper: should match 1+ if radios are present.
    if fp.radio.wrapper_selector:
        n = await _count_selector_hits(adapter, fp.radio.wrapper_selector)
        if n == 0:
            penalties.append(f"radio.wrapper_selector matched 0")
            fp.radio.wrapper_selector = ""

    # Date picker input selector
    if fp.date_picker.input_selector:
        n = await _count_selector_hits(adapter, fp.date_picker.input_selector)
        if n == 0:
            penalties.append(f"date_picker.input_selector matched 0")
            fp.date_picker.input_selector = ""

    # File upload trigger
    if fp.file_upload.trigger_selector:
        n = await _count_selector_hits(adapter, fp.file_upload.trigger_selector)
        if n == 0:
            penalties.append(f"file_upload.trigger_selector matched 0")
            fp.file_upload.trigger_selector = ""

    # Confidence penalty: subtract 0.1 per invalidated selector, clamped
    # to [0, 1]. A fingerprint with 3 broken selectors drops from 0.9 to
    # 0.6 — still usable (strategy-0 attempt), but marked as uncertain.
    if penalties:
        fp.confidence = max(0.0, fp.confidence - 0.1 * len(penalties))
        fp.evidence_seen.extend([f"[validation] {p}" for p in penalties])

    return fp


# ── Main probe entry point ──────────────────────────────────────────

async def probe_app(
    adapter,
    app_name: str,
    budget: BudgetTracker,
    guardrails: GuardrailContext | None = None,
    save: bool = True,
) -> AppFingerprint:
    """Probe the currently-loaded page and produce an AppFingerprint.

    Caller is expected to have navigated the browser to a form-heavy page
    of the app (same workflow as form_extract's --wait). The probe reads
    the static DOM — it does NOT click anything, so the page state is
    preserved for a subsequent form_extract run.

    Returns the fingerprint. When save=True (default), persists it to
    artifacts/knowledge/web/<app>.meta.json via FingerprintStore.
    """
    print(f"  [probe] ══ Learning selector DNA for {app_name!r} ══")
    t0 = time.time()

    page_gc = guardrails if guardrails is not None else per_page_scope()

    # ── Gather evidence from the live DOM (deterministic JS, no cost) ─
    print(f"  [probe] 1/3  gathering DOM evidence")
    raw = await adapter.evaluate_script(_JS_GATHER_EVIDENCE)
    evidence = _safe_parse(raw)
    if not isinstance(evidence, dict):
        print(f"  [probe]      ⚠ evidence gather failed — returning empty fingerprint")
        return AppFingerprint(app_name=app_name, confidence=0.0,
                              evidence_seen=["evidence gather returned non-dict"])

    # Summary line so user sees what we found
    totals = evidence.get("totals", {}) or {}
    print(f"  [probe]      inputs_total={totals.get('inputs_total', 0)}  "
          f"selects={totals.get('selects_total', 0)}  "
          f"oxd={totals.get('oxd_input_group_count', 0)}  "
          f"mui={totals.get('mui_formcontrol_count', 0)}  "
          f"label_for={totals.get('tecu_label_for_count', 0)}  "
          f"testid={totals.get('data_testid_count', 0)}")
    print(f"  [probe]      clickables={len(evidence.get('clickable_widgets', []))}  "
          f"inputs={len(evidence.get('input_label_pairs', []))}  "
          f"radios={len(evidence.get('radio_groups', []))}  "
          f"dates={len(evidence.get('date_candidates', []))}  "
          f"uploads={len(evidence.get('upload_candidates', []))}")

    # ── Ask the LLM to interpret the evidence ───────────────────────
    print(f"  [probe] 2/3  LLM: derive fingerprint from evidence")
    evidence_text = json.dumps(evidence, indent=2)[:6000]  # cap prompt size

    try:
        result = await llm_classify(
            APP_PROBE_PROMPT,
            evidence_text,
            APP_PROBE_SCHEMA,
            budget=budget,
            label="probe_fingerprint",
            guardrails=page_gc,
        )
    except GuardrailExit as e:
        print(f"  [probe]      ✗ LLM guardrail ({e.reason}) — empty fingerprint")
        return AppFingerprint(app_name=app_name, confidence=0.0,
                              evidence_seen=[f"llm guardrail: {e.reason}"])
    except Exception as e:
        print(f"  [probe]      ⚠ LLM failed: {type(e).__name__}: {e}")
        return AppFingerprint(app_name=app_name, confidence=0.0,
                              evidence_seen=[f"llm error: {type(e).__name__}"])

    if not isinstance(result, dict):
        print(f"  [probe]      ⚠ LLM returned non-dict — empty fingerprint")
        return AppFingerprint(app_name=app_name, confidence=0.0,
                              evidence_seen=["llm returned non-dict"])

    # Build the AppFingerprint from the LLM's partial output. Each sub-
    # section is optional — absent keys produce default-empty sub-models.
    fp = AppFingerprint(
        app_name=app_name,
        framework_signature=str(result.get("framework_signature") or "unknown"),
        dropdown=DropdownFingerprint(**(result.get("dropdown") or {})),
        text_input=TextInputFingerprint(**(result.get("text_input") or {})),
        date_picker=DatePickerFingerprint(**(result.get("date_picker") or {})),
        radio=RadioFingerprint(**(result.get("radio") or {})),
        file_upload=FileUploadFingerprint(**(result.get("file_upload") or {})),
        confidence=float(result.get("confidence") or 0.0),
        evidence_seen=list(result.get("evidence_seen") or []),
    )

    print(f"  [probe]      framework={fp.framework_signature!r}  "
          f"confidence={fp.confidence:.2f}")
    if fp.dropdown.trigger_selector:
        print(f"  [probe]      dropdown.trigger={fp.dropdown.trigger_selector!r}")
    if fp.text_input.label_binding:
        print(f"  [probe]      text_input.label_binding={fp.text_input.label_binding!r}")

    # ── Validate: check each generated selector hits something ───────
    print(f"  [probe] 3/3  validating selectors against live DOM")
    fp = await _validate_and_adjust_confidence(adapter, fp)
    print(f"  [probe]      final confidence={fp.confidence:.2f}")

    # ── Persist ─────────────────────────────────────────────────────
    if save:
        store = FingerprintStore()
        path = store.save(fp)
        print(f"  [probe]      saved to {path}")

    elapsed = time.time() - t0
    print(f"  [probe] ✓ Done in {elapsed:.1f}s — ${budget.current_cost:.4f}")
    return fp


# ── CLI entry (standalone probe run) ────────────────────────────────
#
# Usage:
#   python -m qa.orchestrators.app_probe <url> --app-name TECU --wait
#
# Loads the browser, pauses for navigation (same --wait pattern as
# form_extract), runs the probe, saves the meta.json, prints the
# fingerprint for inspection. Does NOT run extract or execute — purely
# a fingerprint-learning pass.

async def _main() -> int:
    import argparse
    import asyncio as _asyncio
    from dotenv import load_dotenv

    load_dotenv()

    from qa.adapters import make_adapter
    from qa.models import Platform, TargetApp

    ap = argparse.ArgumentParser(
        description="Wall 2.0 — Probe an app's selector DNA and save to meta.json.",
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", required=True, help="App name (e.g. TECU, OrangeHRM)")
    ap.add_argument(
        "--wait", action="store_true",
        help="Pause after browser launch so you can navigate + log in",
    )
    ap.add_argument("--model", default="gpt-5.1", help="Model for probe LLM")
    ap.add_argument("--budget", type=float, default=0.50, help="Max $ budget")
    args = ap.parse_args()

    app = TargetApp(platform=Platform.WEB, url=args.url, app_name=args.app_name)
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    if args.wait:
        print()
        print("=" * 60)
        print("  PAUSE: navigate to a form-heavy page (log in if needed).")
        print("  Press Enter when the form is fully visible.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to probe... ")
        except EOFError:
            pass

    budget = BudgetTracker(model=args.model, max_budget=args.budget)

    try:
        fp = await probe_app(adapter, args.app_name, budget)
    finally:
        await adapter.close()

    print()
    print("=" * 60)
    print(f"  FINGERPRINT: {args.app_name}")
    print("=" * 60)
    print(fp.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    import asyncio as _asyncio
    import sys
    sys.exit(_asyncio.run(_main()))
