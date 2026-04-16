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
    # Now `raw` should be a JSON array/object. Return the first complete one.
    for opener, closer in (("[", "]"), ("{", "}")):
        i = raw.find(opener)
        if i != -1:
            j = raw.rfind(closer)
            if j > i:
                return raw[i:j + 1]
    return raw if (raw.startswith("[") or raw.startswith("{")) else None


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

    # Step 1: Detect type and capture options
    detect_js = f"""
      (function() {{
        let el = null;
        try {{ el = document.querySelector('{sel}'); }} catch(e) {{
          // Selector failed — try as text-content match on buttons/comboboxes
          const txt = '{sel}'.toLowerCase();
          el = [...document.querySelectorAll('button, [role=combobox], [role=button]')]
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
    open_js = f"""
      (function() {{
        const el = document.querySelector('{sel}');
        if (!el) return JSON.stringify({{status: 'ELEMENT_NOT_FOUND'}});
        el.click();
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
          '[role=option], [role=menuitem], li[role=listitem], .MuiMenuItem-root, .dropdown-item'
        )].filter(el => el.offsetParent !== null);
        return JSON.stringify(opts.map(o => o.textContent.trim()).filter(t => t));
      })()
    """
    opts_raw = await _eval(options_js)
    parsed_opts = _safe_parse(opts_raw)
    options = parsed_opts if isinstance(parsed_opts, list) else []

    if not options:
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

    # Click the matching option
    click_js = f"""
      (function() {{
        const match = [...document.querySelectorAll(
          '[role=option], [role=menuitem], li[role=listitem], .MuiMenuItem-root, .dropdown-item'
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
    MARKER = "qa-upload-target-input"
    expose_js = (
        "() => {"
        "  const inps = [...document.querySelectorAll('input[type=file]')];"
        "  if (inps.length === 0) return JSON.stringify({status: 'NO_FILE_INPUT'});"
        "  const inp = inps[inps.length - 1];"
        "  inp.style.cssText = 'display:block !important; visibility:visible !important; opacity:1 !important; position:static !important; width:200px; height:30px; pointer-events:auto;';"
        f"  inp.setAttribute('aria-label', '{MARKER}');"
        f"  inp.setAttribute('id', inp.id || '{MARKER}');"
        "  return JSON.stringify({status: 'EXPOSED', count: inps.length, name: inp.name||'', id: inp.id||''});"
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

    # ── Step 5: Snapshot — find the input by our marker ─────────────────────
    print(f"  [upload] Step 5: snapshot to find marked input uid")
    exposed_snapshot = await _call_mcp("take_snapshot", {})
    file_input_uid = _find_uid_by_text(exposed_snapshot, MARKER)
    if not file_input_uid:
        reported_id = expose.get("id", "")
        if reported_id:
            file_input_uid = _find_uid_by_text(exposed_snapshot, reported_id)
            if file_input_uid:
                print(f"  [upload] Found input via reported id '{reported_id}'")
    else:
        print(f"  [upload] Found input via marker '{MARKER}'")
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
    # Some apps (banking, KYC) validate uploaded docs via backend OCR before
    # rendering the next section. Poll snapshots until loading/verifying
    # signals disappear, new content appears, or we hit the timeout.
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


# ── Tool lists ───────────────────────────────────────────────────────────────

EXPLORE_TOOLS = [
    scan_page_summary,
    fill_field_and_verify,
    test_dropdown,
    verify_elements_exist,
    # Upload available during explore so gated multi-step pages can be advanced
    # (e.g. "section 2 only unlocks after section 1's document is verified").
    upload_file_for_field,
]

TASK_TOOLS = [
    scan_page_summary,
    test_text_field,
    test_dropdown,
    upload_file_for_field,
    verify_elements_exist,
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
