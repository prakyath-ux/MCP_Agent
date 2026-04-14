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


def set_server(server: MCPServerStdio) -> None:
    global _mcp_server
    _mcp_server = server


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
    sel = _js_string(css_selector)
    opt = _js_string(select_option)

    # Step 1: Detect type and capture options
    detect_js = f"""
      (function() {{
        const el = document.querySelector('{sel}');
        if (!el) return JSON.stringify({{status: 'ELEMENT_NOT_FOUND'}});
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
    js = f"""
      (function() {{
        const sels = {js_sels};
        return JSON.stringify(sels.map(s => {{
          const el = document.querySelector(s);
          return {{
            selector: s,
            status: el ? 'EXISTS' : 'NOT_FOUND',
            tag: el ? el.tagName.toLowerCase() : null,
            text: el ? (el.textContent.trim().substring(0,80) || el.value || '') : null
          }};
        }}));
      }})()
    """
    raw = await _eval(js)
    results = _safe_parse(raw)
    if not isinstance(results, list):
        print(f"  ⚠ verify_elements_exist parse failed. Raw: {raw[:300]}")
        return f"Verification failed. Raw: {raw[:300]}"
    return json.dumps(results, indent=2)


# ── Tool lists ───────────────────────────────────────────────────────────────

EXPLORE_TOOLS = [
    scan_page_summary,
    fill_field_and_verify,
    test_dropdown,
    verify_elements_exist,
]

TASK_TOOLS = [
    scan_page_summary,
    test_text_field,
    test_dropdown,
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
