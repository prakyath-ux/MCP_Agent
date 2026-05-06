# qa/tools/web_tools.py — Task-level compound tools for web testing
#
# Mirrors mobile_tools architecture:
#   - Module-level MCP server injection (set_server)
#   - Internal helpers (non-LLM): _call_mcp, _eval, _find_error_for
#   - Task tools (LLM-facing): do entire sequences in ONE call
#
# Web-specific design choices:
#   - All write operations use evaluate_script (proven more reliable than fill/click)
#     — sets .value, dispatches input/change/blur events.
#   - Error detection reads siblings with multi-class fallback (MUI/generic/aria).
#   - Dropdowns auto-detect native <select> vs custom [role=combobox] and use the
#     right flow for each.
#   - No coordinate or keyboard handling needed.

import json
import time

from agents import function_tool
from agents.mcp import MCPServerStdio


# ── Module-level state ───────────────────────────────────────────────────────

_mcp_server: MCPServerStdio | None = None
_kb = None  # KnowledgeBase — set per execute run; tools query L0 metadata
_app_name: str = ""


def set_server(server: MCPServerStdio) -> None:
    global _mcp_server
    _mcp_server = server


def set_kb(kb, app_name: str = "") -> None:
    """Inject the active KB so compound tools can look up L0 metadata
    (semantic_hint, accept) and resolve file paths server-side."""
    global _kb, _app_name
    _kb = kb
    _app_name = app_name or (kb.app.app_name if kb else "")


def clear_caches() -> None:
    """Reset any module state. Called between screens/pages."""
    pass  # No coordinate caches on web — DOM handles it


# ── Internal helpers (not exposed to LLM) ────────────────────────────────────

async def _call_mcp(tool_name: str, args: dict) -> str:
    if not _mcp_server:
        return "ERROR: MCP server not initialized"
    t0 = time.time()
    result = await _mcp_server.call_tool(tool_name, args)
    elapsed = time.time() - t0
    if elapsed > 5:
        print(f"    ⚠ MCP slow: {tool_name} took {elapsed:.1f}s")
    if result.content and len(result.content) > 0:
        return result.content[0].text
    return ""


async def _eval(js: str) -> str:
    """Run JS in the page via Chrome DevTools MCP. The tool takes a `function`
    parameter expecting an arrow function. We auto-convert legacy IIFE patterns
    `(function(){ ... })()` to `() => { ... }` so both shapes work.
    """
    import re
    body = js.strip()
    m = re.match(r"^\(function\(\)\s*\{(.*)\}\)\(\)$", body, flags=re.DOTALL)
    if m:
        body = "() => {" + m.group(1) + "}"
    elif not body.startswith("()") and not body.startswith("async"):
        # Single expression like `document.title` → `() => document.title`
        body = f"() => {{ return {body}; }}"
    return await _call_mcp("evaluate_script", {"function": body})


def _extract_json(raw: str) -> str | None:
    """Chrome DevTools MCP wraps evaluate_script results in various ways.
    Known shape: "Script ran on page and returned:\\n```json\\n<quoted-string>\\n```".
    Strip wrappers and return the inner JSON payload, or None if nothing
    JSON-looking is present.
    """
    if not raw:
        return None
    import re
    # Strip common prefixes
    for prefix in (
        "Script ran on page and returned:",
        "Result:", "Execution result:", "Return value:", "Output:",
    ):
        idx = raw.find(prefix)
        if idx != -1:
            raw = raw[idx + len(prefix):]
            break
    raw = raw.strip()
    # Strip markdown code fences (```json ... ```)
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, flags=re.DOTALL)
    if fence_match:
        raw = fence_match.group(1).strip()
    # Unwrap outer double-quotes if the whole thing is a quoted JSON string
    if raw.startswith('"') and raw.endswith('"'):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raw = raw[1:-1]
    # After unwrap, the payload itself should be valid JSON — no need to
    # guess where it starts/ends. The old find-first-`[` / rfind-last-`]`
    # heuristic would grab brackets embedded in string values (e.g. the
    # `[for=...]` inside a CSS selector value), producing a mangled
    # substring. Trust the unwrap instead.
    raw = raw.strip() if isinstance(raw, str) else raw
    if isinstance(raw, str) and (raw.startswith("{") or raw.startswith("[")):
        return raw
    return None


def _safe_parse(raw: str) -> object | None:
    """Try hard to get a Python object out of whatever Chrome MCP returned."""
    if not raw:
        return None
    payload = _extract_json(raw)
    if payload is None:
        return None
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        # Second try — sometimes internal quotes are escaped
        try:
            return json.loads(payload.replace('\\"', '"').replace("\\'", "'"))
        except (json.JSONDecodeError, ValueError):
            return None


def _js_string(value: str) -> str:
    """Safely escape a Python string for embedding in a JS string literal."""
    return (
        value.replace("\\", "\\\\")
             .replace("'", "\\'")
             .replace("\n", "\\n")
             .replace("\r", "\\r")
    )


def _normalize_selector(sel: str) -> str:
    """Strip jQuery/Playwright pseudo-selectors that querySelector can't handle.

    LLMs frequently emit `button:has(span:contains("X"))` or
    `button:has-text("X")` which are NOT valid CSS. We extract the text
    inside the pseudo-class and return it as plain text so the caller can
    fall back to text-content matching.
    """
    import re as _re
    # `:contains("X")` or `:contains('X')` → keep the text only
    m = _re.search(r":contains\(\s*['\"]([^'\"]+)['\"]\s*\)", sel)
    if m:
        return m.group(1)
    # `:has-text("X")` → keep the text only
    m = _re.search(r":has-text\(\s*['\"]([^'\"]+)['\"]\s*\)", sel)
    if m:
        return m.group(1)
    # `tag:has(...)` with non-CSS-spec usage → strip
    m = _re.search(r":has\(\s*[^)]*['\"]([^'\"]+)['\"][^)]*\)", sel)
    if m:
        return m.group(1)
    return sel


# ── Task-level compound tools (exposed to LLM) ───────────────────────────────


@function_tool
async def scan_page_summary(include_buttons: str = "yes") -> str:
    """Return a compact summary of ALL interactive elements on the current page.
    Use this at the start of every page to understand layout. Much cheaper than a
    full snapshot — just labels, types, and CSS selectors.

    Args:
        include_buttons: "yes" to include buttons, "no" to only include form fields.
    """
    include_buttons_flag = "true" if include_buttons.lower() == "yes" else "false"
    js = f"""
      (function() {{
        const includeButtons = {include_buttons_flag};
        const selectors = ['input', 'select', 'textarea', '[role=combobox]', '[role=listbox]'];
        if (includeButtons) selectors.push('button', '[role=button]', 'a[href]');
        // Take everything that matches — do NOT filter by visibility. Hidden
        // <input type=file>, aria-hidden, and late-painted React nodes must
        // still be captured for the knowledge base.
        const elements = [...document.querySelectorAll(selectors.join(','))]
          .filter(el => !(el.type === 'hidden' && el.tagName === 'INPUT' && !el.name));
        const out = elements.map(el => {{
          const tag = el.tagName.toLowerCase();
          const type = el.type || el.getAttribute('role') || tag;
          const name = el.name || el.id || el.getAttribute('aria-label') ||
                       el.placeholder || el.textContent.trim().substring(0,60) || '(unlabeled)';
          let cssSelector = '';
          if (el.name) cssSelector = tag + '[name="' + el.name + '"]';
          else if (el.id) cssSelector = tag + '[id="' + el.id + '"]';
          else if (el.getAttribute('aria-label'))
            cssSelector = tag + '[aria-label="' + el.getAttribute('aria-label') + '"]';
          const required = el.required || el.getAttribute('aria-required') === 'true';
          const value = el.value || '';
          return {{ name: name, type: type, css: cssSelector, required: required, value: value }};
        }});
        return JSON.stringify(out);
      }})()
    """
    # Try twice with a small wait in between — React pages often need a beat.
    raw = await _eval(js)
    elements = _safe_parse(raw)
    if not isinstance(elements, list) or len(elements) == 0:
        import asyncio
        await asyncio.sleep(1.2)
        raw = await _eval(js)
        elements = _safe_parse(raw)

    if not isinstance(elements, list):
        print(f"  ⚠ scan_page_summary parse failed. Raw (first 400 chars): {raw[:400]}")
        return f"Page scan returned unparseable output. Raw: {raw[:500]}"

    if len(elements) == 0:
        return "Page scan returned 0 interactive elements. The page may still be loading. Try navigate_page to reload, or use evaluate_script to query specific elements directly."

    lines = [f"Page elements ({len(elements)} interactive):"]
    for el in elements:
        req = " *" if el.get("required") else ""
        val = f" = '{el.get('value')}'" if el.get("value") else ""
        css = f" [{el.get('css')}]" if el.get("css") else ""
        lines.append(f"  {el.get('type')}: '{el.get('name')}'{req}{val}{css}")
    return "\n".join(lines)


@function_tool
async def fill_field_and_verify(css_selector: str, value: str) -> str:
    """Fill a text field, dispatch input/change/blur events, then read back the
    actual value AND any validation error. One MCP call, no LLM turns wasted.

    Args:
        css_selector: CSS selector (e.g. input[name="firstName"]).
        value: Text to enter. Use empty string to clear and test required validation.
    """
    sel = _js_string(css_selector)
    val = _js_string(value)
    js = f"""
      (function() {{
        const el = document.querySelector('{sel}');
        if (!el) return JSON.stringify({{status: 'ELEMENT_NOT_FOUND', selector: '{sel}'}});
        el.focus();
        // React-safe setter: bypasses React's synthetic-event reconciliation
        // so the value actually sticks in controlled inputs.
        const proto = (el.tagName === 'TEXTAREA')
          ? window.HTMLTextAreaElement.prototype
          : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, '{val}');
        el.dispatchEvent(new InputEvent('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        el.blur();
        const parent = el.closest('.MuiFormControl-root') ||
                       el.closest('.field-group') ||
                       el.closest('[class*=field]') ||
                       el.parentElement && el.parentElement.parentElement;
        const err = parent ? parent.querySelector(
          '.MuiFormHelperText-root.Mui-error, .error, .helper-text, ' +
          '[class*=error], [class*=Error], [role=alert]'
        ) : null;
        return JSON.stringify({{
          status: 'FILLED',
          actual: el.value,
          error: err ? err.textContent.trim() : 'NO_ERROR'
        }});
      }})()
    """
    raw = await _eval(js)
    return raw


@function_tool
async def test_text_field(css_selector: str, test_values: str) -> str:
    """Test a text field with MULTIPLE values in one call. For each value: clear,
    fill, dispatch events, read error. Returns a JSON array of results.

    Args:
        css_selector: CSS selector for the field (e.g. input[name="email"]).
        test_values: Comma-separated values (e.g. ",invalid,,valid@test.com").
                     Empty entry tests required validation.
    """
    values = [v.strip() for v in test_values.split(",")]
    results = []
    sel = _js_string(css_selector)

    for value in values:
        val = _js_string(value)
        js = f"""
          (function() {{
            const el = document.querySelector('{sel}');
            if (!el) return JSON.stringify({{value: '{val}', status: 'SKIP', error: 'ELEMENT_NOT_FOUND'}});
            el.focus();
            const proto = (el.tagName === 'TEXTAREA')
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, '');
            el.dispatchEvent(new InputEvent('input', {{bubbles: true}}));
            setter.call(el, '{val}');
            el.dispatchEvent(new InputEvent('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            el.blur();
            const parent = el.closest('.MuiFormControl-root') ||
                           el.closest('.field-group') ||
                           el.closest('[class*=field]') ||
                           el.parentElement && el.parentElement.parentElement;
            const err = parent ? parent.querySelector(
              '.MuiFormHelperText-root.Mui-error, .error, .helper-text, ' +
              '[class*=error], [class*=Error], [role=alert]'
            ) : null;
            const errText = err ? err.textContent.trim() : 'NO_ERROR';
            const hasError = errText !== 'NO_ERROR' && errText.length > 0;
            const isEmpty = '{val}' === '';
            return JSON.stringify({{
              value: '{val}' || '(empty)',
              actual: el.value,
              error: errText,
              status: (isEmpty && hasError) ? 'PASS' :
                      (isEmpty && !hasError) ? 'FAIL' :
                      (!isEmpty && !hasError) ? 'PASS' : 'FAIL'
            }});
          }})()
        """
        raw = await _eval(js)
        parsed = _safe_parse(raw)
        if isinstance(parsed, dict):
            results.append(parsed)
        else:
            results.append({"value": value, "status": "SKIP", "raw": raw[:200]})

    # ── Restore a stable valid value at end ──────────────────────────────
    # After testing N values rapidly, the field's final state is whatever
    # was last set — often empty or invalid. For multi-page flows where we
    # later click Save & Continue, every required field must hold a valid
    # value. Restore the last NON-EMPTY value tested if one exists.
    last_valid = ""
    for r in reversed(results):
        v = r.get("value", "")
        if v and v != "(empty)" and r.get("status") in ("PASS", "FAIL"):
            last_valid = v
            break

    if last_valid:
        restore_val = _js_string(last_valid)
        restore_js = f"""
          () => {{
            const el = document.querySelector('{sel}');
            if (!el) return 'NOT_FOUND';
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            el.focus();
            setter.call(el, '{restore_val}');
            el.dispatchEvent(new InputEvent('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            el.blur();
            return el.value;
          }}
        """
        await _eval(restore_js)
        results.append({"_post_test_state": f"field restored to last valid value: {last_valid!r}"})

    return json.dumps(results, indent=2)


@function_tool
async def test_dropdown(css_selector: str, select_option: str = "") -> str:
    """Test a dropdown. Auto-detects native <select> vs custom combobox.
    Captures ALL options. Optionally selects one and verifies.

    Args:
        css_selector: CSS selector for the dropdown trigger.
        select_option: Option text to select (substring match, case-insensitive).
                       Leave empty to just capture options.
    """
    # Sanitize: LLMs love hallucinating jQuery/Playwright selectors that
    # don't work in querySelector. Strip them to a plain text-match fallback.
    css_selector = _normalize_selector(css_selector)
    sel = _js_string(css_selector)
    opt = _js_string(select_option)

    # Step 1: Detect type and capture options. Note: querySelector with a
    # space-containing string like "Select Branch" returns null (treats
    # space as a CSS descendant combinator — valid syntax, no match) rather
    # than throwing, so the text-content fallback runs whenever el is null,
    # not just on exceptions.
    detect_js = f"""
      (function() {{
        let el = null;
        try {{ el = document.querySelector('{sel}'); }} catch(e) {{}}
        if (!el) {{
          const txt = '{sel}'.toLowerCase().trim();
          el = [...document.querySelectorAll(
            'button, [role=combobox], [role=button], [aria-haspopup], '
            + '[data-testid*="dropdown" i], [data-testid*="select" i]'
          )]
            .filter(e => e.offsetParent !== null)
            .find(e => e.textContent.trim().toLowerCase().includes(txt));
        }}
        if (!el) return JSON.stringify({{status: 'ELEMENT_NOT_FOUND', selector_tried: '{sel}'}});
        if (el.tagName === 'SELECT') {{
          const opts = [...el.options].map(o => o.textContent.trim()).filter(t => t);
          return JSON.stringify({{kind: 'native', options: opts, current: el.value}});
        }}
        return JSON.stringify({{kind: 'custom'}});
      }})()
    """
    raw = await _eval(detect_js)
    detect = _safe_parse(raw)
    if not isinstance(detect, dict):
        print(f"  ⚠ test_dropdown detect parse failed. Raw: {raw[:300]}")
        return json.dumps({"status": "FAIL", "error": f"Detection failed: {raw[:200]}"})

    if detect.get("status") == "ELEMENT_NOT_FOUND":
        return json.dumps({"status": "SKIP", "error": f"ELEMENT_NOT_FOUND: '{css_selector}'"})

    # ── Native <select> path ──
    if detect.get("kind") == "native":
        options = detect.get("options", [])
        if not select_option:
            return json.dumps({
                "status": "PASS",
                "kind": "native",
                "options": options,
                "count": len(options),
            })

        # Select by substring match
        select_js = f"""
          (function() {{
            const el = document.querySelector('{sel}');
            const target = [...el.options].find(o =>
              o.textContent.trim().toLowerCase().includes('{opt}'.toLowerCase())
            );
            if (!target) return JSON.stringify({{status: 'OPTION_NOT_FOUND'}});
            el.value = target.value;
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{status: 'SELECTED', value: target.textContent.trim()}});
          }})()
        """
        result_raw = await _eval(select_js)
        result = _safe_parse(result_raw)
        if not isinstance(result, dict):
            result = {"status": "FAIL", "raw": result_raw[:200]}

        if result.get("status") == "OPTION_NOT_FOUND":
            return json.dumps({
                "status": "FAIL",
                "kind": "native",
                "options": options,
                "error": f"Option '{select_option}' not found",
            })
        return json.dumps({
            "status": "PASS",
            "kind": "native",
            "options": options,
            "count": len(options),
            "selected": result.get("value"),
        })

    # ── Custom combobox path ──
    # Same null-check + text-match fallback as detect step, otherwise a
    # space-containing label like "Select Branch" yields null silently.
    # React-Select needs focus + ArrowDown to reliably open its menu when
    # the click target is the inner <input role=combobox> rather than the
    # control wrapper, so we send that as a follow-up nudge.
    open_js = f"""
      (function() {{
        let el = null;
        try {{ el = document.querySelector('{sel}'); }} catch(e) {{}}
        if (!el) {{
          const txt = '{sel}'.toLowerCase().trim();
          el = [...document.querySelectorAll(
            'button, [role=combobox], [role=button], [aria-haspopup], '
            + '[data-testid*="dropdown" i], [data-testid*="select" i]'
          )]
            .filter(e => e.offsetParent !== null)
            .find(e => e.textContent.trim().toLowerCase().includes(txt));
        }}
        if (!el) return JSON.stringify({{status: 'ELEMENT_NOT_FOUND'}});
        el.scrollIntoView({{block: 'center', behavior: 'instant'}});
        el.click();
        // Nudge React-Select / Headless UI: focus the trigger then ArrowDown.
        try {{ el.focus({{preventScroll: true}}); }} catch (_) {{}}
        const evtInit = {{key: 'ArrowDown', code: 'ArrowDown', bubbles: true, cancelable: true}};
        el.dispatchEvent(new KeyboardEvent('keydown', evtInit));
        return JSON.stringify({{status: 'OPENED'}});
      }})()
    """
    await _eval(open_js)
    # give the popup a moment to render
    import asyncio
    await asyncio.sleep(0.4)

    options_js = """
      (function() {
        const opts = [...document.querySelectorAll(
          '[role=option], [role=menuitem], li[role=listitem], .MuiMenuItem-root, .dropdown-item, [class*="select__option"], [class*="dropdown__option"], [class*="combobox__option"], [data-option-index], [class*="-Option"]'
        )].filter(el => el.offsetParent !== null);
        return JSON.stringify(opts.map(o => o.textContent.trim()).filter(t => t));
      })()
    """
    opts_raw = await _eval(options_js)
    parsed_opts = _safe_parse(opts_raw)
    options = parsed_opts if isinstance(parsed_opts, list) else []

    if not options:
        # Smart fallback: standard role/menuitem selectors found nothing
        # (TECU-style apps render options as plain divs without ARIA roles).
        # If a target option text was given, try matching by visible textContent.
        if select_option:
            smart_js = f"""
              (function() {{
                const target = '{opt}'.toLowerCase().trim();
                const trigger = document.querySelector('{sel}');
                const all = [...document.querySelectorAll('*')];
                const matches = all.filter(el => {{
                  if (el === trigger) return false;
                  if (el.offsetParent === null) return false;
                  const t = (el.textContent || '').trim();
                  if (!t || t.length > 200) return false;
                  if (!t.toLowerCase().includes(target)) return false;
                  // leaf-ish: no element child whose own text contains target
                  for (const c of el.children) {{
                    const ct = (c.textContent || '').trim().toLowerCase();
                    if (ct.includes(target)) return false;
                  }}
                  return true;
                }});
                if (!matches.length) return JSON.stringify({{status: 'OPTION_NOT_FOUND'}});
                const pick = matches[0];
                const text = pick.textContent.trim().slice(0, 200);
                pick.click();
                return JSON.stringify({{status: 'SELECTED', text, kind: 'smart_fallback'}});
              }})()
            """
            smart_raw = await _eval(smart_js)
            smart_result = _safe_parse(smart_raw)
            if isinstance(smart_result, dict) and smart_result.get("status") == "SELECTED":
                return json.dumps({
                    "status": "PASS",
                    "kind": "custom_smart_fallback",
                    "selected": smart_result.get("text"),
                    "note": "Matched option by visible text content (no standard option markup found).",
                })
        return json.dumps({
            "status": "FAIL",
            "kind": "custom",
            "opened": False,
            "error": "No options appeared after clicking combobox",
        })

    if not select_option:
        # Close the popup without selecting (press Escape)
        await _eval("document.activeElement && document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        return json.dumps({
            "status": "PASS",
            "kind": "custom",
            "options": options,
            "count": len(options),
        })

    # FIRST/* sentinels: pick the first non-placeholder option discovered at
    # runtime. Used by plan when extract didn't capture options[] (React-Select
    # and similar custom comboboxes whose list only renders after click).
    if select_option.upper() in ("FIRST", "*", "__FIRST_OPTION__"):
        placeholder_phrases = (
            "select", "choose", "please select", "-- select --", "select...",
            "select an option", "choose an option", "open this select menu",
        )
        first_real = next(
            (o for o in options if o.strip().lower() not in placeholder_phrases),
            options[0] if options else "",
        )
        if not first_real:
            return json.dumps({
                "status": "FAIL",
                "kind": "custom",
                "error": "FIRST requested but no non-placeholder option available",
                "options": options,
            })
        select_option = first_real
        opt = _js_string(select_option)

    # Click the matching option
    click_js = f"""
      (function() {{
        const match = [...document.querySelectorAll(
          '[role=option], [role=menuitem], li[role=listitem], .MuiMenuItem-root, .dropdown-item, [class*="select__option"], [class*="dropdown__option"], [class*="combobox__option"], [data-option-index], [class*="-Option"]'
        )].filter(el => el.offsetParent !== null)
         .find(el => el.textContent.trim().toLowerCase().includes('{opt}'.toLowerCase()));
        if (!match) return JSON.stringify({{status: 'OPTION_NOT_FOUND'}});
        match.click();
        return JSON.stringify({{status: 'SELECTED', text: match.textContent.trim()}});
      }})()
    """
    click_raw = await _eval(click_js)
    click_result = _safe_parse(click_raw)
    if not isinstance(click_result, dict):
        click_result = {"status": "FAIL", "raw": click_raw[:200]}

    if click_result.get("status") == "OPTION_NOT_FOUND":
        return json.dumps({
            "status": "FAIL",
            "kind": "custom",
            "options": options,
            "error": f"Option '{select_option}' not found",
        })
    return json.dumps({
        "status": "PASS",
        "kind": "custom",
        "options": options,
        "count": len(options),
        "selected": click_result.get("text"),
    })


@function_tool
async def verify_elements_exist(css_selectors: str) -> str:
    """Check if multiple elements exist on the page in ONE call.

    Args:
        css_selectors: Comma-separated CSS selectors
                       (e.g. 'button[type=submit], input[name=email]').
    """
    selectors = [s.strip() for s in css_selectors.split(",")]
    js_sels = json.dumps(selectors)
    # JS handles three selector flavors:
    #   1. Standard CSS via querySelector
    #   2. Playwright-style `:has-text("X")` → fall back to text-content scan
    #      (LLMs frequently hallucinate this; we accept it gracefully)
    #   3. JS:` prefix → run as raw expression (find element by code)
    js = f"""
      () => {{
        const sels = {js_sels};
        return JSON.stringify(sels.map(s => {{
          let el = null;
          try {{
            const hasTextMatch = s.match(/^([a-zA-Z0-9*]+)?:has-text\\(["']([^"']+)["']\\)$/);
            if (hasTextMatch) {{
              const tag = (hasTextMatch[1] || '*').toLowerCase();
              const needle = hasTextMatch[2].toLowerCase();
              el = [...document.querySelectorAll(tag)].find(e => e.textContent.trim().toLowerCase().includes(needle));
            }} else if (s.startsWith('JS:')) {{
              el = eval(s.slice(3));
            }} else {{
              el = document.querySelector(s);
            }}
          }} catch (e) {{
            return {{ selector: s, status: 'INVALID_SELECTOR', error: String(e).slice(0, 120) }};
          }}
          return {{
            selector: s,
            status: el ? 'EXISTS' : 'NOT_FOUND',
            tag: el ? el.tagName.toLowerCase() : null,
            text: el ? (el.textContent.trim().substring(0,80) || el.value || '') : null
          }};
        }}));
      }}
    """
    raw = await _eval(js)
    results = _safe_parse(raw)
    if not isinstance(results, list):
        print(f"  ⚠ verify_elements_exist parse failed. Raw: {raw[:300]}")
        return f"Verification failed. Raw: {raw[:300]}"
    return json.dumps(results, indent=2)


async def _upload_file_for_field_impl(
    field_name: str,
    file_name: str = "",
    wait_for_ocr: bool = True,
    target_input_id: str = "",
) -> str:
    """Internal implementation. The decorated @function_tool wrapper below
    delegates here so orchestrators can call this function directly from
    Python without going through the LLM's tool-invocation path.

    wait_for_ocr=False → attach the file and return immediately after the
    DOM settles. Use this from orchestrators that need to click a
    confirm/Upload/Verify button BEFORE OCR kicks off, then poll for OCR
    themselves. Default True preserves the existing LLM-facing behavior.

    target_input_id → when a section has multiple input[type=file] elements
    (e.g. KYC "front of ID" + "back of ID"), the caller can route this
    upload to a specific input by its DOM id. Empty string keeps the old
    "grab the last input in the DOM" behavior.
    """
    import asyncio
    import re
    from pathlib import Path

    print(f"\n  [upload] Starting upload for field '{field_name}'" + (f" with explicit file '{file_name}'" if file_name else ""))

    # ── Step 0: Resolve which file to upload ─────────────────────────────────
    # Try to find a matching L0 entry in KB (normal execute path).
    # If KB is empty or has no match AND an explicit file_name was given,
    # proceed anyway — we're likely running during explore where the KB is
    # being built and doesn't yet have entries for the current screen.
    upload_l0 = None
    if _kb is not None:
        name_lower = field_name.lower()
        for screen in _kb.screens:
            for el in screen.l0:
                if el.type.value != "file_upload":
                    continue
                if (el.name.lower() == name_lower
                    or name_lower in el.name.lower()
                    or el.name.lower() in name_lower):
                    upload_l0 = el
                    break
            if upload_l0:
                break

    if upload_l0 is None and not file_name:
        available = [el.name for s in _kb.screens for el in s.l0 if el.type.value == "file_upload"] if _kb else []
        print(f"  [upload] ✗ No matching L0 element AND no explicit file_name provided.")
        return json.dumps({
            "status": "ERROR",
            "reason": f"No file_upload L0 element found matching '{field_name}' and no file_name override given",
            "available": available,
            "hint": "During explore (when KB is still being built), call this tool with both field_name AND file_name.",
        })

    if upload_l0:
        print(f"  [upload] L0 matched: {upload_l0.element_id} (hint={upload_l0.semantic_hint!r})")
    else:
        print(f"  [upload] No L0 yet (explore mode) — proceeding with explicit file_name '{file_name}'")

    # Explicit file_name override takes priority over auto-resolution
    file_path = ""
    if file_name:
        from qa.knowledge.file_resolver import _safe_app_dir
        root = Path("artifacts/test_files").resolve()
        candidates = [
            root / _safe_app_dir(_app_name) / file_name,
            root / "global" / file_name,
        ]
        for c in candidates:
            if c.exists():
                file_path = str(c)
                print(f"  [upload] Using explicit file: {file_path}")
                break
        if not file_path:
            from qa.knowledge.file_resolver import _safe_app_dir as _s
            available_files = []
            for d in (root / _s(_app_name), root / "global"):
                if d.exists():
                    available_files.extend(f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() != ".json")
            return json.dumps({
                "status": "ERROR",
                "reason": f"Explicit file '{file_name}' not found",
                "searched": [str(c) for c in candidates],
                "available_in_app_folder": sorted(set(available_files)),
            })

    from qa.knowledge.file_resolver import resolve_upload_path
    if not file_path:
        # Only reached when no explicit file_name was given — upload_l0 must
        # exist (we errored earlier if both were missing).
        file_path = resolve_upload_path(
            element_id=upload_l0.element_id,
            semantic_hint=upload_l0.semantic_hint or "other",
            accept=upload_l0.accept,
            app_name=_app_name,
            element_name=upload_l0.name,
        )
    print(f"  [upload] File path: {file_path}")

    # ── Step 1: Look for hidden <input type=file> WITHOUT any clicks ───────
    # This is version_2's proven approach. Most apps have the input in the
    # DOM from page load (display:none but present). Querying directly
    # avoids the trigger-click → modal → OS picker rabbit hole.
    print(f"  [upload] Step 1: querying DOM for input[type=file] (no clicks)")
    initial_count_raw = await _eval(
        "() => JSON.stringify({n: document.querySelectorAll('input[type=file]').length})"
    )
    initial_count = (_safe_parse(initial_count_raw) or {}).get("n", 0)
    print(f"  [upload] input[type=file] in DOM at start: {initial_count}")

    pre_snapshot = await _call_mcp("take_snapshot", {})
    # Look for the trigger by L0 name if available, else use the LLM-provided field_name
    l0_name = upload_l0.name if upload_l0 else field_name
    trigger_uid = _find_uid_by_text(pre_snapshot, l0_name) or _find_uid_by_text(pre_snapshot, field_name)
    print(f"  [upload] Trigger uid (kept for verification only): {trigger_uid or 'NOT_FOUND'}")

    # ── Step 2: If no input on page, ONLY THEN click the trigger ───────────
    post_click_snapshot = pre_snapshot
    modal_button_uid = ""
    if initial_count == 0:
        if not trigger_uid:
            print(f"  [upload] ✗ No hidden input AND no trigger — cannot proceed")
            return json.dumps({
                "status": "FAIL",
                "reason": "No input[type=file] in DOM and no visible trigger to click",
                "file_resolved": file_path,
            })
        print(f"  [upload] Step 2: no input on page — clicking trigger uid={trigger_uid} as fallback")
        await _call_mcp("click", {"uid": trigger_uid})
        await asyncio.sleep(0.4)
        post_click_snapshot = await _call_mcp("take_snapshot", {})
        recheck_raw = await _eval(
            "() => JSON.stringify({n: document.querySelectorAll('input[type=file]').length})"
        )
        recheck = (_safe_parse(recheck_raw) or {}).get("n", 0)
        print(f"  [upload] input[type=file] after trigger click: {recheck}")
        if recheck == 0:
            print(f"  [upload] ✗ Trigger click did not inject input. App likely uses pure JS picker.")
            return json.dumps({
                "status": "BLOCKED",
                "reason": "No input[type=file] in DOM even after trigger click. App may use drag-drop or pure JS picker.",
                "file_resolved": file_path,
                "trigger_uid": trigger_uid,
            })
    else:
        print(f"  [upload] Step 2: skipped — input already in DOM from page load")

    # ── Step 4: Expose hidden <input type=file> AND tag it with a known label ─
    # Setting a unique aria-label gives us a deterministic way to locate the
    # uid in the next snapshot, instead of guessing which element is the input.
    # When target_input_id is given, pick that specific input; otherwise fall
    # back to the last input[type=file] in the DOM (prior behavior).
    #
    # CRITICAL: clear the marker from ALL other inputs first. Leftover
    # markers from a prior upload cause _find_uid_by_text to return the
    # wrong uid (the first still-tagged input), which silently routes the
    # current file to the previous input's slot.
    MARKER = "qa-upload-target-input"
    target_id_json = json.dumps(target_input_id)
    expose_js = (
        "() => {"
        "  const inps = [...document.querySelectorAll('input[type=file]')];"
        "  if (inps.length === 0) return JSON.stringify({status: 'NO_FILE_INPUT'});"
        "  inps.forEach(el => {"
        f"    if (el.getAttribute('aria-label') === '{MARKER}') "
        "      el.removeAttribute('aria-label');"
        "  });"
        f"  const targetId = {target_id_json};"
        "  let inp = null;"
        "  if (targetId) {"
        "    inp = document.getElementById(targetId);"
        "    if (!inp || inp.type !== 'file') inp = inps.find(el => el.id === targetId);"
        "  }"
        "  if (!inp) inp = inps[inps.length - 1];"
        "  inp.style.cssText = 'display:block !important; visibility:visible !important; opacity:1 !important; position:static !important; width:200px; height:30px; pointer-events:auto;';"
        f"  inp.setAttribute('aria-label', '{MARKER}');"
        f"  inp.setAttribute('id', inp.id || '{MARKER}');"
        "  return JSON.stringify({status: 'EXPOSED', count: inps.length, name: inp.name||'', id: inp.id||'', targeted: Boolean(targetId)});"
        "}"
    )
    print(f"  [upload] Step 4: exposing hidden input via JS")
    expose_raw = await _eval(expose_js)
    expose = _safe_parse(expose_raw)
    print(f"  [upload] Expose result: {expose}")
    if not isinstance(expose, dict) or expose.get("status") != "EXPOSED":
        print(f"  [upload] ✗ BLOCKED — no input[type=file] in DOM")
        return json.dumps({
            "status": "BLOCKED",
            "reason": "No <input type=file> in DOM after trigger click. App may use drag-drop or JS-only picker.",
            "file_resolved": file_path,
            "trigger_uid": trigger_uid,
            "modal_button_clicked": bool(modal_button_uid),
        })

    # ── Step 5: Snapshot — find the input by our MARKER. ───────────────────
    # Safe because expose_js cleared the MARKER off every other input first,
    # so only this one carries it. The DOM id is unreliable for snapshot
    # lookup — TECU (and similar apps) render labels with `for="<input_id>"`
    # which puts the id string on MULTIPLE lines and _find_uid_by_text would
    # return the first match, which isn't necessarily the input itself.
    print(f"  [upload] Step 5: snapshot to find input uid")
    exposed_snapshot = await _call_mcp("take_snapshot", {})
    file_input_uid = _find_uid_by_text(exposed_snapshot, MARKER)
    if file_input_uid:
        print(f"  [upload] Found input via marker '{MARKER}' → uid={file_input_uid}")
    if not file_input_uid:
        print(f"  [upload] ✗ BLOCKED — exposed input not in a11y tree")
        return json.dumps({
            "status": "BLOCKED",
            "reason": "Exposed input not visible in snapshot a11y tree (marker '" + MARKER + "' not found)",
            "file_resolved": file_path,
            "trigger_uid": trigger_uid,
            "modal_button_clicked": bool(modal_button_uid),
            "expose_result": expose,
        })
    print(f"  [upload] File input uid: {file_input_uid}")

    # ── Step 6: Upload the file ─────────────────────────────────────────────
    print(f"  [upload] Step 6: upload_file(uid={file_input_uid}, filePath={file_path})")
    upload_result = await _call_mcp("upload_file", {"uid": file_input_uid, "filePath": file_path})
    print(f"  [upload] upload_file MCP returned: {str(upload_result)[:150]}")

    # ── Step 7: Wait for verification / OCR processing ──────────────────────
    # Orchestrators set wait_for_ocr=False so they can click a confirm
    # button (Upload / Verify / Continue) BEFORE OCR starts, then handle
    # the wait themselves. LLM-facing default stays True for back-compat.
    if not wait_for_ocr:
        # Give the DOM a brief moment to settle after attachment so the
        # caller's next snapshot reflects any immediate filename/thumbnail.
        await asyncio.sleep(1.0)
        post_attach_snap = await _call_mcp("take_snapshot", {})
        return json.dumps({
            "status": "ATTACHED",
            "file_uploaded": file_path,
            "trigger_uid": trigger_uid,
            "file_input_uid": file_input_uid,
            "modal_button_clicked": bool(modal_button_uid),
            "post_attach_snapshot_len": len(post_attach_snap or ""),
            "upload_result_excerpt": str(upload_result)[:200],
        }, indent=2)

    print(f"  [upload] Step 7: polling for verification complete (max 30s)")
    verify_snapshot, wait_signal, elapsed = await _wait_for_verification(
        post_click_snapshot, timeout=30.0, poll_interval=1.5,
    )
    print(f"  [upload]   {wait_signal} after {elapsed:.1f}s")

    # ── Step 8: Final success check ─────────────────────────────────────────
    success_signal = _detect_upload_success(post_click_snapshot, verify_snapshot, file_path)
    print(f"  [upload] {'✓ PASS' if success_signal else '✗ FAIL'} — signal: {success_signal or 'none'}")

    return json.dumps({
        "status": "PASS" if success_signal else "FAIL",
        "file_uploaded": file_path,
        "trigger_uid": trigger_uid,
        "file_input_uid": file_input_uid,
        "modal_button_clicked": bool(modal_button_uid),
        "success_signal": success_signal or "none detected (no Re-upload text, filename, or thumbnail)",
        "upload_result_excerpt": str(upload_result)[:200],
    }, indent=2)


@function_tool
async def upload_file_for_field(field_name: str, file_name: str = "") -> str:
    """Upload a test file to the named upload field. Python handles the entire
    sequence: file resolution, trigger click, modal cascade, hidden input
    exposure, upload_file MCP call, and wait-for-verification.

    Args:
      field_name: Visible upload trigger text (e.g. "Add profile picture",
                  "First form of ID front image upload"). Match the L0 name.
      file_name: OPTIONAL. Explicit filename to upload (e.g. "passport.png").
                 Use this when you've selected a specific dropdown option
                 (e.g. Passport) and need to pair it with the right file.
                 Path is resolved relative to artifacts/test_files/{app_name}/
                 first, then artifacts/test_files/global/. If omitted, Python
                 picks the best-matching file automatically via token overlap.
    """
    return await _upload_file_for_field_impl(field_name, file_name)


# ── Internal helpers for upload_file_for_field ───────────────────────────────

def _find_uid_by_text(snapshot: str, text: str) -> str:
    """Find uid of an element whose visible text/label matches `text` exactly
    (case-insensitive). Snapshot format: lines like `uid=1_14 button "camera Add profile picture"`."""
    if not snapshot or not text:
        return ""
    text_l = text.lower()
    for line in snapshot.split("\n"):
        if text_l in line.lower():
            m = __import__("re").search(r"uid=(\S+)", line)
            if m:
                return m.group(1)
    return ""


def _find_uid_by_text_pattern(snapshot: str, pattern, exclude_uid: str = "") -> str:
    """Find uid of an element whose VISIBLE text (between quotes) matches a regex."""
    if not snapshot:
        return ""
    import re as _re
    for line in snapshot.split("\n"):
        # Extract uid + quoted text
        m = _re.search(r'uid=(\S+).*?"([^"]+)"', line)
        if not m:
            continue
        uid, text = m.group(1), m.group(2).strip()
        if uid == exclude_uid:
            continue
        if pattern.match(text):
            return uid
    return ""


def _find_file_input_uid(snapshot: str) -> str:
    """Find uid of an <input type=file> in the snapshot."""
    if not snapshot:
        return ""
    import re as _re
    for line in snapshot.split("\n"):
        if "input" in line.lower() and ("type=\"file\"" in line.lower() or "[file]" in line.lower() or 'value="file"' in line.lower()):
            m = _re.search(r"uid=(\S+)", line)
            if m:
                return m.group(1)
    # Fallback: any element with file-related label
    for line in snapshot.split("\n"):
        if "file_upload" in line.lower() or 'role="file"' in line.lower():
            m = _re.search(r"uid=(\S+)", line)
            if m:
                return m.group(1)
    return ""


async def _wait_for_verification(
    before_snap: str,
    timeout: float = 30.0,
    poll_interval: float = 1.5,
) -> tuple[str, str, float]:
    """Poll snapshots after an upload until verification/OCR completes.

    Detects: "Verifying..." / "Processing..." / spinner text disappearing,
    OR new content appearing that wasn't there before (indicating the next
    section rendered). Returns (final_snapshot, reason, elapsed_seconds).
    """
    import asyncio
    import re as _re

    loading_patterns = _re.compile(
        r"(verifying|processing|loading|uploading|please\s+wait|analy[sz]ing)",
        _re.IGNORECASE,
    )
    before_len = len(before_snap or "")
    start = asyncio.get_event_loop().time()
    last_snap = before_snap or ""
    saw_loading = False

    while True:
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed >= timeout:
            return last_snap, f"timeout after {timeout:.0f}s", elapsed

        await asyncio.sleep(poll_interval)
        snap = await _call_mcp("take_snapshot", {})
        if not snap:
            last_snap = last_snap  # keep last good
            continue
        last_snap = snap

        is_loading = bool(loading_patterns.search(snap))
        if is_loading:
            saw_loading = True
            continue  # still verifying, keep polling

        # Not currently loading. If we ever saw loading, this means verification
        # just completed. OR: content size grew noticeably (new section rendered).
        if saw_loading:
            return snap, "verification complete (loading signal cleared)", elapsed
        if len(snap) > before_len + 500:
            return snap, f"new content rendered (snapshot +{len(snap) - before_len} chars)", elapsed

        # No loading ever seen, no significant new content — stop early if
        # we've given it at least 3s to be safe.
        if elapsed >= 3.0:
            return snap, "no verification step detected", elapsed


def _detect_upload_success(before_snap: str, after_snap: str, file_path: str) -> str:
    """Look for signals that an upload succeeded."""
    if not after_snap:
        return ""
    after_l = after_snap.lower()
    filename = file_path.rsplit("/", 1)[-1].lower()

    if filename in after_l:
        return f"filename '{filename}' visible"
    if "re-upload" in after_l or "reupload" in after_l:
        return "trigger text changed to Re-upload"
    if "uploaded" in after_l and "uploaded" not in (before_snap or "").lower():
        return "'uploaded' text appeared"
    # Thumbnail — image element added that wasn't there before
    before_imgs = (before_snap or "").lower().count("image ")
    after_imgs = after_l.count("image ")
    if after_imgs > before_imgs:
        return f"new image element appeared (count {before_imgs} → {after_imgs})"
    return ""


PAGE_DIAGNOSTIC_JS = r"""
() => {
  const findings = {
    green: { native_inputs: 0, native_selects: 0, native_textareas: 0, native_buttons: 0 },
    yellow: [],
    red: [],
  };

  // Visible-only filter; we don't want hidden chat widgets etc. polluting counts.
  const visible = (el) => el && el.offsetParent !== null;

  // GREEN — native form controls
  findings.green.native_inputs = [...document.querySelectorAll('input:not([type="hidden"])')]
    .filter(visible).length;
  findings.green.native_selects = [...document.querySelectorAll('select')].filter(visible).length;
  findings.green.native_textareas = [...document.querySelectorAll('textarea')].filter(visible).length;
  findings.green.native_buttons = [...document.querySelectorAll('button')].filter(visible).length;

  // YELLOW — custom dropdowns (non-native trigger elements)
  // Dedupe by visible text: a class*=dropdown match commonly fires on
  // parent wrapper + trigger + inner div all sharing the same label, which
  // are one conceptual dropdown, not three.
  const dropdownRaw = [...document.querySelectorAll(
    '[class*="dropdown" i]:not(select), [data-testid*="dropdown" i]:not(select), [aria-haspopup="listbox"]'
  )].filter(el => el.tagName !== 'SELECT' && visible(el));
  const dropdownByText = new Map();
  for (const el of dropdownRaw) {
    const text = (el.textContent || '').trim().slice(0, 80);
    if (!text) continue;
    if (!dropdownByText.has(text)) dropdownByText.set(text, el);
  }
  const customDropdowns = [...dropdownByText.values()];
  if (customDropdowns.length) {
    findings.yellow.push({
      pattern: 'custom_dropdown',
      count: customDropdowns.length,
      detail: 'Non-native dropdowns. Supported via test_dropdown smart fallback.',
      examples: customDropdowns.slice(0, 3).map(e => (e.textContent || '').trim().slice(0, 60)),
    });
  }

  // IFRAMES — same-origin (yellow if forms or rich-text inside) vs
  // cross-origin (red). Rich-text editor iframes (TinyMCE, CKEditor, Quill)
  // host a contenteditable body and are just as hard to drive as a form;
  // we flag them under the same yellow bucket so coverage expectations
  // are honest.
  const iframes = [...document.querySelectorAll('iframe')].filter(visible);
  const crossOrigin = [], sameOriginWithForm = [], sameOriginRichText = [];
  for (const f of iframes) {
    let same = false;
    try { same = !!(f.contentDocument && f.contentDocument.body); } catch (_) { same = false; }
    if (!same) { crossOrigin.push(f); continue; }
    try {
      const formish = f.contentDocument.querySelectorAll('input, select, textarea').length;
      if (formish > 0) {
        sameOriginWithForm.push(f);
        continue;
      }
      const body = f.contentDocument.body;
      const ce = body && (body.getAttribute('contenteditable') || '').toLowerCase();
      if (ce === 'true' || f.contentDocument.querySelector('[contenteditable="true"]')) {
        sameOriginRichText.push(f);
      }
    } catch (_) {}
  }
  if (crossOrigin.length) {
    findings.red.push({
      pattern: 'cross_origin_iframe',
      count: crossOrigin.length,
      detail: 'Cross-origin iframe content is unreachable due to browser security boundary.',
      examples: crossOrigin.slice(0, 3).map(f => (f.src || '(no src)').slice(0, 80)),
    });
  }
  if (sameOriginWithForm.length) {
    findings.yellow.push({
      pattern: 'same_origin_iframe_with_form',
      count: sameOriginWithForm.length,
      detail: 'Form rendered inside a same-origin iframe. Top-level extractor does not recurse.',
      examples: sameOriginWithForm.slice(0, 3).map(f => (f.src || '(no src)').slice(0, 80)),
    });
  }
  if (sameOriginRichText.length) {
    findings.yellow.push({
      pattern: 'rich_text_editor_iframe',
      count: sameOriginRichText.length,
      detail: 'Rich-text editor iframe (TinyMCE / CKEditor / Quill etc). The contenteditable surface inside requires editor-specific drive logic — fill_check on the iframe wrapper does not work.',
      examples: sameOriginRichText.slice(0, 3).map(f => (f.src || f.id || '(unlabeled)').slice(0, 80)),
    });
  }

  // SHADOW DOM — skip framework infrastructure that doesn't host form content.
  // next-route-announcer (Next.js a11y), iron-* (legacy Polymer), etc. attach
  // shadow roots but never carry user-facing fields.
  const SHADOW_INFRASTRUCTURE = new Set([
    'next-route-announcer',
    'iron-meta',
  ]);
  const shadowHosts = [...document.querySelectorAll('*')].filter(el => {
    try {
      if (!el.shadowRoot) return false;
      return !SHADOW_INFRASTRUCTURE.has(el.tagName.toLowerCase());
    } catch (_) { return false; }
  });
  if (shadowHosts.length) {
    findings.yellow.push({
      pattern: 'shadow_dom',
      count: shadowHosts.length,
      detail: 'Open shadow roots detected — querying inside requires traversal. Closed shadow roots cannot be reached.',
      examples: shadowHosts.slice(0, 3).map(e => e.tagName.toLowerCase()),
    });
  }

  // FILE UPLOADS — direct vs hidden-behind-trigger
  const fileInputs = [...document.querySelectorAll('input[type="file"]')];
  const hiddenFileInputs = fileInputs.filter(f => f.offsetParent === null);
  if (hiddenFileInputs.length) {
    findings.yellow.push({
      pattern: 'hidden_file_input',
      count: hiddenFileInputs.length,
      detail: 'File inputs hidden behind a styled trigger (modal flow). Supported via upload_file_for_field.',
    });
  }

  // CONSENT / COOKIE OVERLAY
  const consents = [...document.querySelectorAll(
    '[class*="cookie" i], [class*="consent" i], [id*="cookie" i], [id*="consent" i]'
  )].filter(el => visible(el) && getComputedStyle(el).position === 'fixed');
  if (consents.length) {
    findings.yellow.push({
      pattern: 'consent_overlay',
      count: consents.length,
      detail: 'Cookie/consent overlay may cover form. Dismiss before testing.',
    });
  }

  // CAPTCHA — designed to defeat automation
  const captchaSel = (
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
    + '[class*="g-recaptcha"], [class*="h-captcha"], '
    + '[id*="captcha" i]:not(label)'
  );
  const captchas = [...document.querySelectorAll(captchaSel)];
  if (captchas.length) {
    findings.red.push({
      pattern: 'captcha',
      count: captchas.length,
      detail: 'CAPTCHA present — by design defeats automation.',
    });
  }

  // BOT DETECTION — Cloudflare-style challenges
  const bodyText = (document.body && document.body.innerText || '').slice(0, 5000).toLowerCase();
  const botPhrases = ['just a moment', 'checking your browser', 'cloudflare ray id'];
  const botMarkerEl = document.querySelector('#cf-challenge-running, [class*="cf-challenge" i], [id*="challenge-form" i]');
  if (botMarkerEl || botPhrases.some(p => bodyText.includes(p))) {
    findings.red.push({
      pattern: 'bot_detection_challenge',
      count: 1,
      detail: 'Bot-detection page (Cloudflare or similar) — page may not load reliably.',
    });
  }

  // MULTI-PAGE WIZARD — Save & Continue / Next-step buttons
  const wizardButtons = [...document.querySelectorAll('button, a, [role="button"]')]
    .filter(el => visible(el) && /save\s*(&|and)\s*continue|continue|next\s*step|proceed/i.test(
      (el.textContent || '').trim()
    ));
  if (wizardButtons.length) {
    findings.yellow.push({
      pattern: 'multi_page_wizard',
      count: wizardButtons.length,
      detail: 'Multi-page wizard pattern. Page transitions may not change the URL.',
      examples: wizardButtons.slice(0, 3).map(b => (b.textContent || '').trim().slice(0, 40)),
    });
  }

  // CASCADE — disabled inputs likely depending on a parent.
  // Suppress noise: skip elements inside iframes/dialogs/closed-form-chrome,
  // and only flag when the count is small (≤ 5). High counts almost always
  // mean framework infrastructure (TinyMCE controls, hidden form scaffolding,
  // CAPTCHA inner-form fields), not real parent→child cascades.
  const disabledInputs = [...document.querySelectorAll(
    'input[disabled], select[disabled], textarea[disabled], [aria-disabled="true"]'
  )].filter(el => {
    if (!visible(el)) return false;
    if (el.closest('iframe, dialog, [role="dialog"]')) return false;
    return true;
  });
  if (disabledInputs.length > 0 && disabledInputs.length <= 5) {
    findings.yellow.push({
      pattern: 'disabled_cascade_inputs',
      count: disabledInputs.length,
      detail: 'Disabled fields likely depend on a parent value. Cascade order needs to be declared per app.',
    });
  }

  // LAZY / SKELETON — content not yet hydrated
  const lazy = [...document.querySelectorAll(
    '[aria-busy="true"], [class*="skeleton" i]'
  )].filter(visible);
  if (lazy.length) {
    findings.yellow.push({
      pattern: 'lazy_or_skeleton',
      count: lazy.length,
      detail: 'Skeleton/loading state still visible. Wait for hydration before testing.',
    });
  }

  // PLACEHOLDER-AS-LABEL — input has placeholder but no associated <label>,
  // no aria-label, and isn't wrapped in a label. Field name in reports will
  // be the placeholder text instead of a real label.
  const placeholderOnly = [...document.querySelectorAll(
    'input[placeholder]:not([type="hidden"]), textarea[placeholder]'
  )].filter(el => {
    if (!visible(el)) return false;
    if (el.getAttribute('aria-label')) return false;
    if (el.getAttribute('aria-labelledby')) return false;
    if (el.closest('label')) return false;
    if (el.id) {
      const cssEsc = (CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
      try {
        if (document.querySelector('label[for="' + cssEsc + '"]')) return false;
      } catch (_) {}
    }
    return Boolean((el.placeholder || '').trim());
  });
  if (placeholderOnly.length) {
    findings.yellow.push({
      pattern: 'placeholder_as_label',
      count: placeholderOnly.length,
      detail: 'Inputs use placeholder text instead of a proper <label>. Field names in reports will be the placeholder, weakening readability.',
      examples: placeholderOnly.slice(0, 3).map(el => (el.placeholder || '').trim().slice(0, 50)),
    });
  }

  // PROGRAMMATIC RADIO NAMES — radio groups whose accessible name is missing
  // or matches the kebab/snake-case `name` attribute (i.e. testid leaked into
  // the label). Reports will show 'verify-who-speaks-first' instead of
  // 'Who Speaks First'.
  const radioGroups = new Map();
  for (const r of document.querySelectorAll('input[type="radio"]')) {
    if (!visible(r)) continue;
    const groupName = r.name || '';
    if (!groupName) continue;
    if (!radioGroups.has(groupName)) {
      // Pull the group's "label" the same way the extractor does: aria-label,
      // wrapping label, label[for=id], or fallback to the name attribute.
      let labelText = r.getAttribute('aria-label') || '';
      if (!labelText && r.closest('label')) {
        labelText = (r.closest('label').textContent || '').trim();
      }
      if (!labelText && r.id) {
        const cssEsc = (CSS && CSS.escape) ? CSS.escape(r.id) : r.id;
        try {
          const lab = document.querySelector('label[for="' + cssEsc + '"]');
          if (lab) labelText = (lab.textContent || '').trim();
        } catch (_) {}
      }
      radioGroups.set(groupName, labelText);
    }
  }
  const programmaticRadios = [...radioGroups.entries()].filter(([name, label]) => {
    if (!label) return true;  // no label at all
    // Exact match (snake/kebab/camel of name) → leak.
    const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
    return norm(name) === norm(label);
  });
  if (programmaticRadios.length) {
    findings.yellow.push({
      pattern: 'programmatic_radio_names',
      count: programmaticRadios.length,
      detail: 'Radio groups have no human-readable label — agent will see the testid (e.g. "verify-who-speaks-first") in reports.',
      examples: programmaticRadios.slice(0, 3).map(([name]) => name.slice(0, 50)),
    });
  }

  // CHIP / PILL TOGGLE BUTTONS — <button> elements that hold binary state
  // rather than triggering an action. Easy signals: aria-pressed attribute,
  // class containing "chip"/"pill"/"tag", or data-state attribute. Need to be
  // tested with click→verify-toggle, not "verify exists".
  const chipToggles = [...document.querySelectorAll('button')].filter(b => {
    if (!visible(b)) return false;
    if (b.hasAttribute('aria-pressed')) return true;
    if (b.hasAttribute('data-state')) return true;
    const cls = (b.className && typeof b.className === 'string')
      ? b.className.toLowerCase() : '';
    return /\b(chip|pill|tag|badge|toggle)\b/.test(cls);
  });
  if (chipToggles.length) {
    findings.yellow.push({
      pattern: 'chip_toggle_buttons',
      count: chipToggles.length,
      detail: 'Buttons holding binary state (chip/pill/aria-pressed). Test as "click then verify state changed" rather than treating as action buttons.',
      examples: chipToggles.slice(0, 3).map(b => (b.textContent || '').trim().slice(0, 40)),
    });
  }

  // FULL INTERACTIVE INVENTORY — ground truth of what's on the page
  // (per-type counts + a few sample names). Useful for comparing against
  // extract output to spot what the LLM dropped.
  const sampleText = (el) => {
    const t = (el.textContent || '').trim();
    if (t) return t.slice(0, 60);
    const ph = el.getAttribute && el.getAttribute('placeholder');
    if (ph) return '[placeholder] ' + ph.slice(0, 60);
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return '[aria-label] ' + al.slice(0, 60);
    const nm = el.getAttribute && el.getAttribute('name');
    if (nm) return '[name] ' + nm.slice(0, 60);
    const id = el.getAttribute && el.getAttribute('id');
    if (id) return '[id] ' + id.slice(0, 60);
    return '(unlabeled)';
  };

  const collect = (selector, max_samples = 5) => {
    const els = [...document.querySelectorAll(selector)].filter(visible);
    return {
      count: els.length,
      samples: els.slice(0, max_samples).map(sampleText),
    };
  };

  const inventory = {
    input_visible:       collect('input:not([type="hidden"])'),
    textarea:            collect('textarea'),
    select:              collect('select'),
    button_native:       collect('button'),
    div_role_button:     collect('div[role="button"]'),
    link_with_href:      collect('a[href]'),
    role_combobox:       collect('[role="combobox"]'),
    role_radio:          collect('[role="radio"]'),
    role_checkbox:       collect('[role="checkbox"]'),
    role_switch:         collect('[role="switch"]'),
    role_listbox:        collect('[role="listbox"]'),
    contenteditable:     collect('[contenteditable="true"]'),
  };

  const inventoryTotal = Object.values(inventory).reduce((s, v) => s + v.count, 0);

  const greenTotal = findings.green.native_inputs + findings.green.native_selects
    + findings.green.native_textareas;
  const verdict = findings.red.length > 0 ? 'red'
    : (findings.yellow.length > 0 ? 'yellow' : 'green');

  return JSON.stringify({
    page_url: location.href,
    page_title: document.title,
    findings,
    inventory,
    summary: {
      green_native_total: greenTotal,
      yellow_count: findings.yellow.length,
      red_count: findings.red.length,
      inventory_total: inventoryTotal,
      verdict,
    },
  });
}
"""


EXHAUSTIVE_SCAN_JS = r"""
() => {
  const visible = (el) => el && el.offsetParent !== null;
  const text = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();
  const cssEsc = (v) => {
    if (v == null) return '';
    if (CSS && CSS.escape) return CSS.escape(String(v));
    return String(v).replace(/(["\\])/g, '\\$1');
  };
  const xpathLit = (v) => {
    const s = String(v == null ? '' : v);
    if (!s.includes("'")) return "'" + s + "'";
    if (!s.includes('"')) return '"' + s + '"';
    return "concat('" + s.split("'").join("', \"'\", '") + "')";
  };

  // Best-effort accessible name. Mirrors the priority WAI-ARIA defines:
  // aria-labelledby > aria-label > <label for=id> > wrapping <label> >
  // container heuristic > placeholder/name/text fallback.
  const labelFor = (el) => {
    const lb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (lb) {
      const joined = lb.split(/\s+/).map(id => {
        const node = document.getElementById(id);
        return node ? text(node) : '';
      }).filter(Boolean).join(' ');
      if (joined) return joined;
    }
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return al.trim();
    if (el.id) {
      try {
        const lab = document.querySelector('label[for="' + cssEsc(el.id) + '"]');
        if (lab) return text(lab);
      } catch (_) {}
    }
    const wrap = el.closest && el.closest('label');
    if (wrap) {
      const wt = text(wrap);
      const et = text(el);
      const stripped = (wt && et) ? wt.replace(et, '').trim() : '';
      if (stripped) return stripped;
      if (wt) return wt;
    }
    const container = el.closest && el.closest(
      '.MuiFormControl-root, .ant-form-item, .form-group, .form-field, '
      + '.field-group, [class*="field"]'
    );
    if (container) {
      const lab = container.querySelector('label, [role="label"], .label, .form-label');
      if (lab) return text(lab);
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'button' || tag === 'a' || el.getAttribute('role') === 'button') {
      const t = text(el);
      if (t) return t;
    }
    if (el.placeholder) return el.placeholder;
    if (el.name) return el.name;
    if (el.id) return el.id;
    const t = text(el);
    return t ? t.slice(0, 80) : '';
  };

  // Section heading nearest above this element. Used so plan can group
  // related fields ("Lead Configuration", "Agent Configuration", etc.).
  const sectionFor = (el) => {
    const headings = [...document.querySelectorAll(
      'h1,h2,h3,h4,h5,h6,legend,[role="heading"]'
    )].filter(h => visible(h) && text(h).length > 0 && text(h).length < 120);
    let best = '';
    for (const h of headings) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
        best = text(h);
      }
    }
    return best;
  };

  // ── Locator builder ───────────────────────────────────────────────────────
  //
  // XPath is the primary strategy because it can express text(),
  // normalize-space(), parent-scope, and positional indices — none of
  // which CSS supports. CSS is kept as a same-page fallback for tools
  // that prefer it.
  //
  // Each element walks an escalation ladder. The first XPath that
  // resolves to exactly one node (verified inline via document.evaluate)
  // wins. Tiers in increasing fragility:
  //
  //   1. Single high-trust attribute  (id, data-testid, name, aria-label,
  //                                    placeholder, exact button text)
  //   2. Two attributes               (role + label, role + text,
  //                                    type + name, etc.)
  //   3. Three attributes             (class-substring added as third axis)
  //   4. Section scope                (ancestor with a heading text anchors
  //                                    the inner predicate)
  //   5. Positional within scope      (when section + attrs still match
  //                                    multiple — e.g., 3 "More information"
  //                                    buttons all in "Lead Capture")
  //   6. Absolute DOM path            (last resort; brittle if the layout
  //                                    changes by even one node)
  //
  // Tiers 5 and 6 set disambiguation_failed = true so the validator can
  // surface them as YELLOW rather than silently shipping wrong-element
  // locators.

  const matchCount = (sel) => {
    try { return document.querySelectorAll(sel).length; } catch (_) { return 0; }
  };
  const xpathCount = (xp) => {
    try {
      return document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength;
    } catch (_) { return 0; }
  };
  const xpathNodes = (xp) => {
    const out = [];
    try {
      const r = document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i));
    } catch (_) {}
    return out;
  };

  // Heading-bearing ancestor closest to the element. Returns the heading's
  // visible text plus whether it's reached via a *direct* child relationship
  // (tighter scope) or descendant relationship (broader scope).
  const findSectionAnchor = (el) => {
    let cur = el.parentElement;
    while (cur && cur !== document.body) {
      const headings = [...cur.querySelectorAll(
        'h1,h2,h3,h4,h5,h6,legend,[role="heading"]'
      )];
      const useful = headings.find(h => {
        if (h === el || h.contains(el)) return false;
        const t = text(h);
        return t.length > 0 && t.length < 120;
      });
      if (useful) return { anchor: cur, heading: text(useful) };
      cur = cur.parentElement;
    }
    return null;
  };

  // Class names we trust enough to use in XPath predicates. Skip Emotion
  // hashes (`css-1abc-`), CSS Modules hashes, and tiny tokens that don't
  // narrow the search.
  const usefulClasses = (el) => {
    if (!el.className || typeof el.className !== 'string') return [];
    return el.className
      .split(/\s+/)
      .filter(c => c
        && c.length >= 4
        && !/^css-/i.test(c)
        && !/^[a-z]+-[a-z0-9]{4,}$/i.test(c)  // CSS-Modules-style hashes
        && !/^_[A-Z]/.test(c));
  };

  // Absolute path of the form /html/body/div[1]/main/.../button[2].
  // Last-resort locator only — breaks on any DOM reshuffle.
  const absolutePath = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      const tag = cur.tagName.toLowerCase();
      const siblings = cur.parentElement
        ? [...cur.parentElement.children].filter(c => c.tagName === cur.tagName)
        : [cur];
      const idx = siblings.indexOf(cur) + 1;
      parts.unshift(tag + '[' + idx + ']');
      cur = cur.parentElement;
    }
    return '/html/' + parts.join('/');
  };

  const locatorsFor = (el) => {
    const tag = el.tagName.toLowerCase();
    const id = el.getAttribute('id') || '';
    const name = el.getAttribute('name') || '';
    const testid = el.getAttribute('data-testid')
      || el.getAttribute('data-test-id')
      || el.getAttribute('data-test')
      || el.getAttribute('data-cy')
      || el.getAttribute('data-qa')
      || '';
    const aria = el.getAttribute('aria-label') || '';
    const role = el.getAttribute('role') || '';
    const type = (el.getAttribute('type') || el.type || '').toLowerCase();
    const value = el.getAttribute('value') || '';
    const placeholder = el.getAttribute('placeholder') || '';
    const elText = text(el);
    const isButtonLike = (tag === 'button' || tag === 'a' || role === 'button');

    // Tier 1 — single discriminating attribute.
    const tier1 = [];
    if (testid)      tier1.push('//' + tag + '[@data-testid=' + xpathLit(testid) + ']');
    if (id)          tier1.push('//' + tag + '[@id=' + xpathLit(id) + ']');
    if (name)        tier1.push('//' + tag + '[@name=' + xpathLit(name) + ']');
    if (aria)        tier1.push('//' + tag + '[@aria-label=' + xpathLit(aria) + ']');
    if (placeholder) tier1.push('//' + tag + '[@placeholder=' + xpathLit(placeholder) + ']');
    if (isButtonLike && elText) {
      tier1.push('//' + tag + '[normalize-space()=' + xpathLit(elText) + ']');
    }

    // Tier 2 — two attributes combined. Radio/checkbox inputs almost always
    // need name + value to be unique.
    const tier2 = [];
    if (['radio','checkbox'].includes(type) && name && value) {
      tier2.push('//' + tag + '[@type=' + xpathLit(type)
                 + ' and @name=' + xpathLit(name)
                 + ' and @value=' + xpathLit(value) + ']');
    }
    if (role && aria) {
      tier2.push('//' + tag + '[@role=' + xpathLit(role)
                 + ' and @aria-label=' + xpathLit(aria) + ']');
    }
    if (role && elText) {
      tier2.push('//' + tag + '[@role=' + xpathLit(role)
                 + ' and normalize-space()=' + xpathLit(elText) + ']');
    }
    if (role && placeholder) {
      tier2.push('//' + tag + '[@role=' + xpathLit(role)
                 + ' and @placeholder=' + xpathLit(placeholder) + ']');
    }
    if (type && name && !['radio','checkbox'].includes(type)) {
      tier2.push('//' + tag + '[@type=' + xpathLit(type)
                 + ' and @name=' + xpathLit(name) + ']');
    }
    if (type && placeholder) {
      tier2.push('//' + tag + '[@type=' + xpathLit(type)
                 + ' and @placeholder=' + xpathLit(placeholder) + ']');
    }
    if (aria && elText && aria !== elText) {
      tier2.push('//' + tag + '[@aria-label=' + xpathLit(aria)
                 + ' and normalize-space()=' + xpathLit(elText) + ']');
    }

    // Tier 3 — three attributes. Class-substring is the additional axis.
    const tier3 = [];
    const classes = usefulClasses(el);
    if (classes.length > 0) {
      const cls = classes[0];
      const classPred = 'contains(concat(" ",normalize-space(@class)," ")," ' + cls + ' ")';
      if (role && elText) {
        tier3.push('//' + tag + '[@role=' + xpathLit(role)
                   + ' and ' + classPred
                   + ' and normalize-space()=' + xpathLit(elText) + ']');
      }
      if (role && aria) {
        tier3.push('//' + tag + '[@role=' + xpathLit(role)
                   + ' and ' + classPred
                   + ' and @aria-label=' + xpathLit(aria) + ']');
      }
      if (aria && elText) {
        tier3.push('//' + tag + '[' + classPred
                   + ' and @aria-label=' + xpathLit(aria)
                   + ' and normalize-space()=' + xpathLit(elText) + ']');
      }
    }

    // Pick first unique candidate from tiers 1-3.
    let bestXpath = '';
    let bestTier = 0;
    const allCandidates = [...tier1, ...tier2, ...tier3];
    const tierOf = (xp) =>
      tier1.includes(xp) ? 1 : tier2.includes(xp) ? 2 : 3;
    for (const xp of allCandidates) {
      if (xpathCount(xp) === 1) {
        bestXpath = xp;
        bestTier = tierOf(xp);
        break;
      }
    }

    let disambiguation_failed = false;
    let scope_used = '';

    // Tier 4 — section scope. Apply when nothing in tiers 1-3 was unique.
    if (!bestXpath) {
      const section = findSectionAnchor(el);
      if (section) {
        scope_used = section.heading;
        // *[has direct heading child with this text] -> //inner
        // Direct-child anchoring (`*[h1[...]]`) is tighter than descendant
        // (`*[.//h1[...]]`), avoiding cases where a top-level wrapper "owns"
        // every heading on the page.
        const headTags = ['h1','h2','h3','h4','h5','h6','legend'];
        const headPred = headTags
          .map(t => t + '[normalize-space()=' + xpathLit(section.heading) + ']')
          .join(' or ') + ' or *[@role="heading"][normalize-space()=' + xpathLit(section.heading) + ']';
        const scopePrefix = '//*[' + headPred + ']';

        const innerCandidates = [];
        if (aria) innerCandidates.push('.//' + tag + '[@aria-label=' + xpathLit(aria) + ']');
        if (placeholder) innerCandidates.push('.//' + tag + '[@placeholder=' + xpathLit(placeholder) + ']');
        if (role && elText) {
          innerCandidates.push('.//' + tag + '[@role=' + xpathLit(role)
                               + ' and normalize-space()=' + xpathLit(elText) + ']');
        }
        if (isButtonLike && elText) {
          innerCandidates.push('.//' + tag + '[normalize-space()=' + xpathLit(elText) + ']');
        }
        if (role) innerCandidates.push('.//' + tag + '[@role=' + xpathLit(role) + ']');

        for (const inner of innerCandidates) {
          const candidate = scopePrefix + inner.substring(1); // strip leading "."
          if (xpathCount(candidate) === 1) {
            bestXpath = candidate;
            bestTier = 4;
            break;
          }
        }

        // Tier 5 — positional within scope. Pick the strongest non-unique
        // scoped candidate, find this element's index among the matches,
        // and pin to that position.
        if (!bestXpath && innerCandidates.length > 0) {
          for (const inner of innerCandidates) {
            const baseScoped = scopePrefix + inner.substring(1);
            const matches = xpathNodes(baseScoped);
            if (matches.length === 0) continue;
            const idx = matches.indexOf(el);
            if (idx < 0) continue;
            const positional = '(' + baseScoped + ')[' + (idx + 1) + ']';
            bestXpath = positional;
            bestTier = 5;
            disambiguation_failed = true;
            break;
          }
        }
      }
    }

    // Tier 6 — absolute path. Always works (uniquely), always brittle.
    if (!bestXpath) {
      bestXpath = absolutePath(el);
      bestTier = 6;
      disambiguation_failed = true;
    }

    // ── CSS (secondary) ─────────────────────────────────────────────────
    // Same priorities as before, plus placeholder for inputs/textareas
    // (closes the gap for label-less fields).
    const cssCandidates = [];
    if (testid)      cssCandidates.push(tag + '[data-testid="' + cssEsc(testid) + '"]');
    if (id)          cssCandidates.push(tag + '[id="' + cssEsc(id) + '"]');
    if (name) {
      if (['radio','checkbox'].includes(type) && value) {
        cssCandidates.push(tag + '[name="' + cssEsc(name) + '"][value="' + cssEsc(value) + '"]');
      } else {
        cssCandidates.push(tag + '[name="' + cssEsc(name) + '"]');
      }
    }
    if (aria)        cssCandidates.push(tag + '[aria-label="' + cssEsc(aria) + '"]');
    if (placeholder && (tag === 'input' || tag === 'textarea')) {
      cssCandidates.push(tag + '[placeholder="' + cssEsc(placeholder) + '"]');
    }
    if (role)        cssCandidates.push(tag + '[role="' + cssEsc(role) + '"]');

    let bestCss = '';
    for (const sel of cssCandidates) {
      if (matchCount(sel) === 1) { bestCss = sel; break; }
    }
    if (!bestCss && cssCandidates.length > 0) bestCss = cssCandidates[0];

    return {
      css: bestCss,
      css_fallbacks: cssCandidates.filter(s => s && s !== bestCss),
      xpath: bestXpath,
      xpath_fallbacks: allCandidates.filter(xp => xp && xp !== bestXpath),
      locator_tier: bestTier,
      disambiguation_failed,
      scope_used,
    };
  };

  // Semantic type — maps DOM shape to our ElementType enum values.
  // Role checks come BEFORE tag checks so that <input role="combobox">
  // (the React-Select / Headless UI pattern) classifies as dropdown
  // rather than as a plain text input.
  const classify = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || el.type || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();

    // Role-first: lets accessible custom widgets win over their underlying tag.
    if (role === 'combobox') return 'dropdown';
    if (role === 'listbox') return 'dropdown';
    if (role === 'radio') return 'radio';
    if (role === 'checkbox') return 'checkbox';
    if (role === 'switch') return 'checkbox';
    if (role === 'tab') return 'nav_tab';
    if (role === 'button') return 'button';

    if (tag === 'select') return 'dropdown';
    if (tag === 'textarea') return 'text_input';
    if (tag === 'a') return 'link';
    if (tag === 'input') {
      if (['date','datetime-local','month','time','week'].includes(type)) return 'date_picker';
      if (type === 'file') return 'file_upload';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['button','submit','reset'].includes(type)) return 'button';
      return 'text_input';
    }
    if (tag === 'button') return 'button';
    if ((el.getAttribute('contenteditable') || '').toLowerCase() === 'true') return 'text_input';
    return 'other';
  };

  const isRequired = (el, label) => Boolean(
    el.required
    || el.getAttribute('aria-required') === 'true'
    || (label && /\*/.test(label))
  );

  const validationRules = (el) => {
    const rules = [];
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || el.type || tag).toLowerCase();
    if (type) rules.push('input_type=' + type);
    for (const a of ['min','max','minlength','maxlength','pattern','step','accept']) {
      const v = el.getAttribute(a);
      if (v) rules.push(a + '=' + v);
    }
    if (el.required || el.getAttribute('aria-required') === 'true') rules.push('required');
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') rules.push('disabled');
    if (el.readOnly || el.getAttribute('readonly') !== null) rules.push('readonly');
    return rules.join('; ');
  };

  // ── Enumerate every interactable. Radio groups are deduped to one entry
  //    per group (input[type=radio] by name; [role=radio] by closest
  //    [role=radiogroup] or shared parent container).
  const elements = [];
  const seenNativeRadioGroup = new Set();
  const seenAriaRadioGroup = new Set();

  const SELECTOR = (
    'input:not([type="hidden"]), select, textarea, button, a[href], '
    + '[role="combobox"]:not(input):not(select), '
    + '[role="listbox"], '
    + '[role="radio"], '
    + '[role="checkbox"]:not(input), '
    + '[role="switch"]:not(input), '
    + '[role="tab"], '
    + '[role="button"]:not(button), '
    + '[contenteditable="true"]'
  );

  const allCandidates = [...document.querySelectorAll(SELECTOR)];

  for (let i = 0; i < allCandidates.length; i++) {
    const el = allCandidates[i];
    if (!visible(el)) continue;

    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || el.type || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();

    // Native radio groups: collapse by name attribute.
    if (tag === 'input' && type === 'radio') {
      const groupKey = el.name || el.id || '';
      if (groupKey && seenNativeRadioGroup.has(groupKey)) continue;
      if (groupKey) seenNativeRadioGroup.add(groupKey);
      const peers = groupKey
        ? [...document.querySelectorAll('input[type="radio"][name="' + cssEsc(groupKey) + '"]')]
        : [el];
      const visiblePeers = peers.filter(visible);
      const optionLabels = visiblePeers.map(p => labelFor(p) || p.value).filter(Boolean);
      const checked = visiblePeers.find(p => p.checked);
      const lab = labelFor(el) || groupKey || 'radio group';
      const loc = locatorsFor(el);
      elements.push({
        kind: 'radio',
        tag,
        input_type: type,
        name: lab,
        section: sectionFor(el),
        required: isRequired(el, lab),
        disabled: visiblePeers.every(p => p.disabled || p.getAttribute('aria-disabled') === 'true'),
        readonly: false,
        value: checked ? (checked.value || '') : '',
        options: optionLabels,
        validation_rules: validationRules(el),
        accept: '',
        css: loc.css, css_fallbacks: loc.css_fallbacks,
        xpath: loc.xpath, xpath_fallbacks: loc.xpath_fallbacks,
        locator_tier: loc.locator_tier,
        disambiguation_failed: loc.disambiguation_failed,
        scope_used: loc.scope_used,
      });
      continue;
    }

    // ARIA radio groups: collapse by closest [role=radiogroup] or shared parent.
    if (role === 'radio' && tag !== 'input') {
      const groupRoot = el.closest('[role="radiogroup"]') || el.parentElement;
      if (!groupRoot) continue;
      const key = groupRoot.id || groupRoot.getAttribute('aria-label')
        || groupRoot.tagName + ':' + (groupRoot.className || '').slice(0, 30);
      if (seenAriaRadioGroup.has(key)) continue;
      seenAriaRadioGroup.add(key);
      const peers = [...groupRoot.querySelectorAll('[role="radio"]')].filter(visible);
      const optionLabels = peers.map(labelFor).filter(Boolean);
      const checked = peers.find(p => p.getAttribute('aria-checked') === 'true');
      const lab = labelFor(groupRoot) || labelFor(el) || 'radio group';
      const loc = locatorsFor(el);
      elements.push({
        kind: 'radio',
        tag,
        input_type: 'aria-radio',
        name: lab,
        section: sectionFor(el),
        required: isRequired(el, lab),
        disabled: peers.every(p => p.getAttribute('aria-disabled') === 'true'),
        readonly: false,
        value: checked ? labelFor(checked) || '' : '',
        options: optionLabels,
        validation_rules: validationRules(el),
        accept: '',
        css: loc.css, css_fallbacks: loc.css_fallbacks,
        xpath: loc.xpath, xpath_fallbacks: loc.xpath_fallbacks,
        locator_tier: loc.locator_tier,
        disambiguation_failed: loc.disambiguation_failed,
        scope_used: loc.scope_used,
      });
      continue;
    }

    // Default: emit one element per visible candidate.
    const lab = labelFor(el) || '(unlabeled)';
    const kind = classify(el);
    const loc = locatorsFor(el);
    let options = [];
    if (kind === 'dropdown' && tag === 'select') {
      options = [...el.options].map(o => text(o) || o.value).filter(Boolean);
    }

    elements.push({
      kind,
      tag,
      input_type: type || role || tag,
      name: lab,
      section: sectionFor(el),
      required: isRequired(el, lab),
      disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
      readonly: el.readOnly || el.getAttribute('readonly') !== null,
      value: el.value || '',
      options,
      validation_rules: validationRules(el),
      accept: kind === 'file_upload' ? (el.accept || '') : '',
      css: loc.css, css_fallbacks: loc.css_fallbacks,
      xpath: loc.xpath, xpath_fallbacks: loc.xpath_fallbacks,
      locator_tier: loc.locator_tier,
      disambiguation_failed: loc.disambiguation_failed,
      scope_used: loc.scope_used,
    });
  }

  return JSON.stringify({
    page_url: location.href,
    page_title: document.title || '',
    extracted_at: new Date().toISOString(),
    element_count: elements.length,
    elements,
  });
}
"""


@function_tool
async def analyze_page_blockers() -> str:
    """Scan the current page DOM for known blocker patterns and classify the
    page as green / yellow / red against the project's tested-and-supported
    catalog.

    Pure read-only DOM inspection — no clicks, no fills, no network. Free
    (no LLM cost). Returns JSON with: native form-control counts, list of
    yellow findings (patterns we support with workarounds), list of red
    findings (out-of-scope hard blockers), and an overall verdict.

    Use at the START of a session to set expectations on what's testable.
    """
    raw = await _eval(PAGE_DIAGNOSTIC_JS)
    parsed = _safe_parse(raw)
    if not isinstance(parsed, dict):
        return json.dumps({"status": "ERROR", "raw": raw[:500]})
    parsed["status"] = "OK"
    return json.dumps(parsed)


@function_tool
async def click_and_observe(field_name: str = "", css_selector: str = "") -> str:
    """Click an action button and watch console + network for errors.

    Use this for action buttons that perform an in-page operation (Save,
    Submit, Apply, Update Bot, Find Member, etc) — NOT navigation buttons
    that change pages.

    Captures all console messages + network requests, clicks the target,
    waits 2 seconds for async errors to surface, then reports anything
    new that looks like an error (Uncaught/Exception/TypeError/etc on the
    console; HTTP 4xx/5xx on the network).

    Args:
        field_name: Visible label of the button (e.g. "Update Bot"). Used
                    as a text-content fallback if no css_selector matches.
        css_selector: Optional CSS selector. Preferred when the button has
                    a stable id/data-testid.

    Returns: JSON
        {status: PASS, clicked: "...", console_lines: N, network_lines: N}
        {status: FAIL, clicked: "...", errors: [{kind, detail}, ...]}
        {status: BLOCKED, reason: "could not click | element_not_found"}
    """
    import asyncio
    import re as _re

    # ── Step 1: Baseline console + network state ─────────────────────────
    async def _safe_call(name: str) -> str:
        try:
            out = await _call_mcp(name, {})
            return out if isinstance(out, str) else str(out)
        except Exception as e:
            return f"__ERROR__:{type(e).__name__}:{e}"

    before_console = await _safe_call("list_console_messages")
    before_network = await _safe_call("list_network_requests")

    # ── Step 2: Locate + click the target ────────────────────────────────
    sel = _normalize_selector(css_selector or field_name)
    sel_js = _js_string(sel)
    name_js = _js_string(field_name)
    click_js = f"""
      (function() {{
        let el = null;
        try {{ el = document.querySelector('{sel_js}'); }} catch(e) {{}}
        if (!el && '{name_js}'.length > 0) {{
          const txt = '{name_js}'.toLowerCase().trim();
          el = [...document.querySelectorAll(
            'button, a, [role="button"]'
          )].filter(e => e.offsetParent !== null)
            .find(e => (e.textContent || '').trim().toLowerCase().includes(txt));
        }}
        if (!el) return JSON.stringify({{
          status: 'ELEMENT_NOT_FOUND',
          tried_selector: '{sel_js}',
          tried_text: '{name_js}'
        }});
        el.scrollIntoView({{block: 'center', behavior: 'instant'}});
        el.click();
        return JSON.stringify({{
          status: 'CLICKED',
          text: ((el.textContent || '').trim() || el.value || el.tagName).slice(0, 80)
        }});
      }})()
    """
    click_raw = await _eval(click_js)
    click_parsed = _safe_parse(click_raw)
    if not isinstance(click_parsed, dict):
        return json.dumps({"status": "BLOCKED", "reason": "click_unparseable", "raw": click_raw[:200]})
    if click_parsed.get("status") != "CLICKED":
        return json.dumps({
            "status": "BLOCKED",
            "reason": click_parsed.get("status", "click_failed"),
            "tried_selector": click_parsed.get("tried_selector", ""),
            "tried_text": click_parsed.get("tried_text", ""),
        })

    clicked_text = click_parsed.get("text", "")

    # ── Step 3: Wait for async errors / requests to settle ───────────────
    await asyncio.sleep(2.0)

    # ── Step 4: Capture post-state ───────────────────────────────────────
    after_console = await _safe_call("list_console_messages")
    after_network = await _safe_call("list_network_requests")

    # ── Step 5: Compute deltas (defensive: text-prefix subtraction) ──────
    def _delta(before: str, after: str) -> str:
        if not isinstance(before, str) or not isinstance(after, str):
            return after if isinstance(after, str) else ""
        if after.startswith(before):
            return after[len(before):]
        return after  # mismatched format — examine all of after

    console_delta = _delta(before_console, after_console)
    network_delta = _delta(before_network, after_network)

    # ── Step 6: Detect errors ────────────────────────────────────────────
    errors: list[dict] = []
    console_error_patterns = (
        "error:", "uncaught", "exception", "typeerror", "syntaxerror",
        "referenceerror", "rangeerror", "failed to fetch",
    )
    for line in (console_delta or "").split("\n"):
        low = line.lower()
        if any(p in low for p in console_error_patterns):
            stripped = line.strip()
            if stripped:
                errors.append({"kind": "console", "detail": stripped[:200]})

    # Network: line containing HTTP 4xx/5xx status code, but not 200/2xx
    for line in (network_delta or "").split("\n"):
        m = _re.search(r"\b([45]\d{2})\b", line)
        if m and not _re.search(r"\b2\d{2}\b", line):
            stripped = line.strip()
            if stripped:
                errors.append({
                    "kind": "network",
                    "status": m.group(1),
                    "detail": stripped[:200],
                })

    if errors:
        return json.dumps({
            "status": "FAIL",
            "clicked": clicked_text,
            "error_count": len(errors),
            "errors": errors[:5],
        })

    return json.dumps({
        "status": "PASS",
        "clicked": clicked_text,
        "console_lines_after": len((console_delta or "").split("\n")),
        "network_lines_after": len((network_delta or "").split("\n")),
    })


@function_tool
async def list_test_files() -> str:
    """Return the test files available for the active app.

    Reads `artifacts/test_files/{app_name}/` and `artifacts/test_files/global/`
    and returns the files the agent can pass to upload tools.

    The agent picks the right file by matching the element name/semantic
    to the filename (e.g. "Add profile picture" → "profile_picture.png",
    "Upload National ID front" → "national_id_front.png").

    Returns: JSON with status + file list. Pass just the filename (not
    a full path) to `upload_file_for_field`; the file_resolver handles
    the path lookup.
    """
    from pathlib import Path
    from qa.knowledge.file_resolver import _safe_app_dir

    root = Path("artifacts/test_files").resolve()
    app_dir = root / _safe_app_dir(_app_name) if _app_name else None
    global_dir = root / "global"

    def _list(path: Path) -> list[str]:
        if not path.exists():
            return []
        return sorted(
            f.name for f in path.iterdir()
            if f.is_file() and f.suffix.lower() not in (".json", ".md", ".tmp")
        )

    app_files = _list(app_dir) if app_dir else []
    global_files = _list(global_dir)

    return json.dumps({
        "status": "OK",
        "app_name": _app_name,
        "app_folder": str(app_dir) if app_dir else "",
        "app_files": app_files,
        "global_files": global_files,
        "hint": "Pass just the filename to upload tools; resolver picks the right path.",
    })


# ── Tool lists ───────────────────────────────────────────────────────────────

EXPLORE_TOOLS = [
    scan_page_summary,
    fill_field_and_verify,
    test_dropdown,
    verify_elements_exist,
    # Upload available during explore so gated multi-step pages can be advanced
    # (e.g. "section 2 only unlocks after section 1's document is verified").
    upload_file_for_field,
    list_test_files,
    analyze_page_blockers,
]

TASK_TOOLS = [
    scan_page_summary,
    fill_field_and_verify,
    test_text_field,
    test_dropdown,
    upload_file_for_field,
    verify_elements_exist,
    list_test_files,
    analyze_page_blockers,
    click_and_observe,
]


def get_task_tools(mcp_server) -> list:
    """Task-level tools for execute pipeline."""
    clear_caches()
    set_server(mcp_server)
    return TASK_TOOLS


def get_explore_tools(mcp_server) -> list:
    """Tools for explore pipeline (single-fill, collects knowledge)."""
    clear_caches()
    set_server(mcp_server)
    return EXPLORE_TOOLS
