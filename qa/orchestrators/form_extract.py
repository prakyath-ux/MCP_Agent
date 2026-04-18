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
from qa.engine.budget import BudgetTracker
from qa.knowledge.store import KnowledgeStore
from qa.models import KnowledgeBase, Platform, TargetApp
from qa.models.common import ElementType, make_element_id
from qa.models.knowledge import L0Element, L1Element, Locator, ScreenKnowledge
from qa.orchestrators.llm_subtask import llm_classify
from qa.orchestrators.sub_prompts import (
    EXTRACT_DROPDOWN_OPTIONS_PROMPT,
    EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
)
from qa.tools.web_tools import _safe_parse


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
    const key = el.id || el.name || el.placeholder || '';
    if (!key || seen.has(key)) return;
    seen.add(key);
    const label = findLabel(el);
    const typeMap = {date: 'date', email: 'email', tel: 'phone', number: 'text_input'};
    results.push({
      label: label.replace(/\s*\*\s*$/, '').trim(),
      type: typeMap[el.type] || 'text_input',
      required: isRequired(el, label),
      id: el.id || '',
      name: el.name || '',
      placeholder: el.placeholder || '',
      value: el.value || '',
      section: findSection(el),
    });
  });

  // Native <select>
  document.querySelectorAll('select').forEach(el => {
    if (el.offsetParent === null) return;
    const key = el.id || el.name || '';
    if (!key || seen.has(key)) return;
    seen.add(key);
    const label = findLabel(el);
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
      section: findSection(el),
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
    });
  });

  return JSON.stringify(results);
}"""


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
}


def _build_screen(
    screen_name: str,
    page_url: str,
    js_elements: list[dict],
    dropdown_data: dict[str, dict],
) -> ScreenKnowledge:
    l0: list[L0Element] = []
    l1: list[L1Element] = []

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

        l0.append(L0Element(
            element_id=eid,
            name=label,
            type=etype,
            required=bool(el.get("required")),
            options=el.get("options", []),
            screen_name=screen_name,
            default_value=el.get("value", ""),
            accept=el.get("accept", ""),
            behavior=(f"Section: {section}" if section else ""),
        ))

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
        l1.append(L1Element(
            element_id=eid,
            locators=locators,
            screen_name=screen_name,
        ))

    # Merge custom dropdown options discovered via LLM
    for trigger_label, info in dropdown_data.items():
        if isinstance(info, dict):
            options = info.get("options", [])
            disabled = bool(info.get("disabled", False))
            section = _clean_label(info.get("section") or "")
        else:
            options = info
            disabled = False
            section = ""

        disabled_note = (
            "Disabled when extracted — likely depends on a prior dropdown being filled. "
            "Execute pipeline should fill parent field first."
            if disabled else ""
        )
        section_note = f"Section: {section}" if section else ""
        behavior = " | ".join(p for p in [section_note, disabled_note] if p)

        eid = make_element_id(screen_name, trigger_label, "dropdown", section=section)
        existing = next((e for e in l0 if e.element_id == eid), None)
        if existing:
            existing.options = options
            if behavior and not existing.behavior:
                existing.behavior = behavior
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
        l1.append(L1Element(element_id=eid, screen_name=screen_name))

    return ScreenKnowledge(
        screen_name=screen_name,
        screen_url=page_url,
        l0=l0,
        l1=l1,
    )


# ── Main extraction flow ────────────────────────────────────────────

async def extract_form(
    adapter,
    app_name: str,
    screen_name: str,
    budget: BudgetTracker,
    page_url: str = "",
    on_progress=None,
) -> ScreenKnowledge:
    """Extract all form elements from the currently-loaded page.

    Args:
        on_progress: optional async callback `(ScreenKnowledge) -> None`
            invoked after each custom-dropdown extraction with the
            current partial ScreenKnowledge. Callers typically use this
            to checkpoint the KB so a mid-loop crash never loses more
            than one element of work. See Wall 2.5f / principle N16.
    """
    server = adapter.get_mcp_server()
    t0 = time.time()

    # ── Step 1: JS enumerates standard form elements ──────────────
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

    # ── Step 3: open each dropdown → LLM extracts options → close ─
    print("  [form] 3/3  open each dropdown → LLM extracts options → close")

    for i, trig in enumerate(triggers):
        uid = trig["uid"]
        label = trig["label"]
        trigger_text = trig["trigger_text"]
        print(f"  [form]      [{i+1}/{len(triggers)}] {label!r} (uid={uid})")

        # Enrich trigger with its nearest section heading — matches what
        # the JS enumerate already attaches to standard form elements.
        section_raw = await adapter.evaluate_script(
            _js_section_for_text(trigger_text)
        )
        section_info = _safe_parse(section_raw) or {}
        trig["section"] = section_info.get("section", "") or ""

        # Disabled check BEFORE clicking: MUI marks cascading dependent
        # dropdowns as disabled until their parent field is filled. We
        # can't open those, so record them as dependent and move on.
        disabled_raw = await adapter.evaluate_script(
            _js_is_disabled_for(trigger_text)
        )
        disabled_info = _safe_parse(disabled_raw) or {}
        is_disabled = bool(disabled_info.get("disabled", False))
        if is_disabled:
            print(f"  [form]        ⊘ DISABLED at capture — likely depends on "
                  "a prior field being filled")
            dropdown_data[label] = {
                "options": [],
                "disabled": True,
                "section": trig.get("section", ""),
            }
            await _checkpoint()
            continue

        async def _open_and_read() -> list[str]:
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
                label=f"opts_{i+1}",
            )
            return result.get("options", []) or []

        # First attempt
        options = await _open_and_read()

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
            # Retry with extra wait
            await server.call_tool("click", {"uid": uid})
            await asyncio.sleep(2.5)
            open_snap = await server.call_tool("take_snapshot", {})
            snap_text = ""
            if open_snap.content:
                snap_text = open_snap.content[0].text or ""
            result = await llm_classify(
                EXTRACT_DROPDOWN_OPTIONS_PROMPT,
                snap_text,
                EXTRACT_DROPDOWN_OPTIONS_SCHEMA,
                budget=budget,
                label=f"opts_{i+1}_retry",
            )
            options = result.get("options", []) or []

        # Dedupe + drop placeholder-y entries
        seen: set[str] = set()
        clean: list[str] = []
        for o in options:
            o = str(o).strip()
            if (o and o not in seen
                    and not re.match(r"^(Select|Choose|--|Please)", o, re.IGNORECASE)):
                seen.add(o)
                clean.append(o)
        print(f"  [form]        → {len(clean)} options")
        if clean:
            for o in clean[:5]:
                print(f"  [form]          - {o!r}")
            if len(clean) > 5:
                print(f"  [form]          ... +{len(clean) - 5} more")

        dropdown_data[label] = {
            "options": clean,
            "disabled": False,
            "section": trig.get("section", ""),
        }

        # Checkpoint AFTER dropdown_data is updated but BEFORE close —
        # on_progress sees the most up-to-date state; close is cleanup
        # and doesn't affect what we persist.
        await _checkpoint()

        # Close before moving to next trigger
        try:
            await server.call_tool("press_key", {"key": "Escape"})
        except Exception:
            pass
        await adapter.evaluate_script(_JS_CLOSE_POPUP)
        await asyncio.sleep(0.5)

    elapsed = time.time() - t0
    print(f"\n  [form] ✓ Done in {elapsed:.1f}s — "
          f"{len(js_elements)} standard + {len(triggers)} custom dropdowns")
    print(f"  [form] Cost: ${budget.current_cost:.4f}")

    return _build_screen(
        screen_name=screen_name,
        page_url=page_url,
        js_elements=js_elements,
        dropdown_data=dropdown_data,
    )


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

    app = TargetApp(platform=Platform.WEB, url=args.url, app_name=args.app_name)

    # Load user-provided defaults early so a bad file fails BEFORE we
    # launch Chrome. Consumed by future walls (1.1 dependent dropdown
    # chaining, 0.1 ExecuteOrchestrator setup phase).
    from qa.config import load_defaults
    defaults_path = args.defaults.strip() or None
    defaults = load_defaults(args.app_name, path=defaults_path)
    print(f"  {defaults.summary()}")

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

    screen_name = args.screen_name
    if not screen_name:
        title_raw = await adapter.evaluate_script("() => document.title")
        screen_name = (title_raw or "").strip().strip('"').strip()
        if not screen_name:
            screen_name = "Extracted Form"
        print(f"  Screen name (from <title>): {screen_name!r}")

    budget = BudgetTracker(model=args.model, max_budget=args.budget)

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

    try:
        screen = await extract_form(
            adapter=adapter,
            app_name=args.app_name,
            screen_name=screen_name,
            budget=budget,
            page_url=args.url,
            on_progress=_checkpoint_kb,
        )
    finally:
        await adapter.close()

    existing = kb.get_screen(screen.screen_name)
    if existing:
        kb.screens = [s for s in kb.screens if s.screen_name != screen.screen_name]
    kb.screens.append(screen)

    path = store.save(kb)
    print(f"\n  KB saved: {path}")
    print(f"  Screen: {screen.screen_name}")
    print(f"  Elements: {len(screen.l0)}")
    for el in screen.l0:
        req = " *" if el.required else ""
        opts = f" [{len(el.options)} opts]" if el.options else ""
        print(f"    - {el.name!r}{req} [{el.type.value}]{opts}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
