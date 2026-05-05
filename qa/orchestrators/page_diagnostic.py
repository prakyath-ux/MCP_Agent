# qa/orchestrators/page_diagnostic.py — Pre-flight DOM blocker scan.
#
# Read-only deterministic scan of the loaded page. Reports green / yellow /
# red against the project's known testability catalog (see
# research/WEB_AGENT_LIMITATIONS.md). No LLM, no clicks, no network.
#
# Usage:
#   python -m qa.orchestrators.page_diagnostic <url> --app-name <name> [--wait]

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
from qa.models import Platform, TargetApp
from qa.tools.web_tools import PAGE_DIAGNOSTIC_JS, _safe_parse


def _safe_app_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "app").lower()).strip("_") or "app"


def _format_report(payload: dict) -> str:
    findings = payload.get("findings", {}) or {}
    summary = payload.get("summary", {}) or {}
    inventory = payload.get("inventory", {}) or {}
    green = findings.get("green", {}) or {}

    verdict = (summary.get("verdict") or "unknown").upper()
    marker = {"GREEN": "✓", "YELLOW": "⚠", "RED": "✗"}.get(verdict, "?")

    lines: list[str] = [
        "Page Diagnostic",
        "=" * 60,
        f"URL:     {payload.get('page_url', '')}",
        f"Title:   {payload.get('page_title', '')}",
        f"Verdict: {marker} {verdict}",
        "",
        "Native form controls detected:",
        f"  inputs:    {green.get('native_inputs', 0)}",
        f"  selects:   {green.get('native_selects', 0)}",
        f"  textareas: {green.get('native_textareas', 0)}",
        f"  buttons:   {green.get('native_buttons', 0)}",
    ]

    yellow = findings.get("yellow", []) or []
    if yellow:
        lines += ["", "⚠ YELLOW patterns (testable with workarounds):"]
        for f in yellow:
            lines.append(f"  • {f.get('pattern', '?'):<30} ×{f.get('count', 0)}")
            if f.get("detail"):
                lines.append(f"      {f['detail']}")
            for ex in (f.get("examples") or [])[:2]:
                lines.append(f"      e.g. {ex}")

    red = findings.get("red", []) or []
    if red:
        lines += ["", "✗ RED patterns (out of scope):"]
        for f in red:
            lines.append(f"  • {f.get('pattern', '?'):<30} ×{f.get('count', 0)}")
            if f.get("detail"):
                lines.append(f"      {f['detail']}")
            for ex in (f.get("examples") or [])[:2]:
                lines.append(f"      e.g. {ex}")

    if not yellow and not red:
        lines += ["", "No yellow/red patterns detected — page should be fully testable."]

    # Full interactive inventory — ground truth of what's on the page.
    # Useful for spotting elements the extractor might drop.
    if inventory:
        lines += ["", "Full interactive inventory (every clickable / fillable):"]
        type_labels = {
            "input_visible":   "input (visible)",
            "textarea":        "textarea",
            "select":          "select",
            "button_native":   "button (native)",
            "div_role_button": "div[role=button]",
            "link_with_href":  "a[href]",
            "role_combobox":   "[role=combobox]",
            "role_radio":      "[role=radio]",
            "role_checkbox":   "[role=checkbox]",
            "role_switch":     "[role=switch]",
            "role_listbox":    "[role=listbox]",
            "contenteditable": "contenteditable",
        }
        for key, label in type_labels.items():
            entry = inventory.get(key) or {}
            count = entry.get("count", 0)
            if count == 0:
                continue
            lines.append(f"  {label:<22} {count}")
            for ex in (entry.get("samples") or [])[:3]:
                lines.append(f"      • {ex}")
        total = summary.get("inventory_total", 0)
        lines.append(f"  {'─' * 30}")
        lines.append(f"  total interactives:    {total}")
        lines.append("")
        lines.append("  Compare to your KB (artifacts/knowledge/web/<app>.json) — any")
        lines.append("  large gap (e.g. 21 div[role=button] but only 5 in KB) means the")
        lines.append("  extract LLM filtered them out.")

    return "\n".join(lines)


async def run_diagnostic(url: str, app_name: str, wait: bool = False) -> dict:
    app = TargetApp(platform=Platform.WEB, url=url, app_name=app_name)
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)
    try:
        if wait:
            print()
            print("=" * 60)
            print("  Browser open. Navigate to the target page if needed.")
            print("  When the page is fully visible, press Enter to scan.")
            print("=" * 60)
            try:
                input("  >>> Press Enter... ")
            except EOFError:
                pass
        else:
            # SPA cold-start: adapter.launch returns when the navigation event
            # fires, but React/Vue hydration may not be done. Give it a moment
            # before we scan, otherwise we get an empty about:blank result.
            await asyncio.sleep(2.5)

        raw = await adapter._call("evaluate_script", {"function": PAGE_DIAGNOSTIC_JS})
        # Some MCP responses come wrapped; _safe_parse handles both shapes.
        payload = _safe_parse(raw)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Diagnostic returned unparseable output: {str(raw)[:500]}")
        return payload
    finally:
        await adapter.close()


def _save_report(app_name: str, payload: dict, report_text: str) -> tuple[Path, Path]:
    now = datetime.now()
    day_dir = Path("artifacts/results") / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d_%H%M%S")
    safe = _safe_app_name(app_name)
    json_path = day_dir / f"{safe}_diagnostic_{ts}.json"
    txt_path = day_dir / f"{safe}_diagnostic_{ts}.txt"
    json_path.write_text(json.dumps(payload, indent=2))
    txt_path.write_text(report_text + "\n")
    return json_path, txt_path


async def _amain() -> int:
    ap = argparse.ArgumentParser(
        description="Page Diagnostic — DOM blocker scan against the project's catalog",
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", "-a", required=True, help="App name for report grouping")
    ap.add_argument(
        "--wait",
        action="store_true",
        help="Pause after launch so you can navigate / log in before scanning",
    )
    args = ap.parse_args()

    payload = await run_diagnostic(args.url, args.app_name, wait=args.wait)
    report = _format_report(payload)
    print()
    print(report)

    json_path, txt_path = _save_report(args.app_name, payload, report)
    print(f"\nReport saved: {txt_path}")
    print(f"JSON saved:   {json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
