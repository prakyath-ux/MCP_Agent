# qa/orchestrators/extract_pipeline.py — Unified extract pipeline.
#
# One subprocess, one Chrome instance, one optional --wait pause for manual
# login. Runs the three stages back-to-back on the same loaded page:
#
#   Stage 1 — page_diagnostic   (DOM blocker scan, GREEN/YELLOW/RED verdict)
#   Stage 2 — exhaustive_extract (full KB, with Phase 1 dropdown options
#             expansion + Phase 2 radio-group conditional discovery)
#   Stage 3 — validate_kb       (per-locator reachability check on the
#             freshly-saved KB)
#
# Each stage prints its own report and saves its own JSON/TXT artifact —
# behavior identical to running the three CLIs separately. The pipeline
# additionally emits one-line stage markers to stdout so a parent process
# (the Streamlit UI, or any CI runner) can flip stage statuses in real
# time without reading the artifact files:
#
#   STAGE_DONE: <stage>: <json>
#
# A final synthesis report is saved next to the per-stage artifacts.
#
# Usage:
#   python -m qa.orchestrators.extract_pipeline <url> -a <app-name> [--wait]

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
from qa.orchestrators.exhaustive_extract import (
    _build_kb,
    _expand_empty_dropdowns,
    _expand_radio_groups,
    _print_summary as _print_extract_summary,
)
from qa.orchestrators.page_diagnostic import (
    _format_report as _format_diagnostic_report,
    _save_report as _save_diagnostic_artifacts,
)
from qa.orchestrators.validate_kb import (
    VALIDATE_KB_JS_TEMPLATE,
    _build_report as _build_validate_report,
    _flatten_l1,
    _save_report as _save_validate_artifacts,
    _safe_app_name,
)
from qa.tools.web_tools import (
    EXHAUSTIVE_SCAN_JS,
    PAGE_DIAGNOSTIC_JS,
    _safe_parse,
)


# ── Stage marker emission ───────────────────────────────────────────────────
#
# Lines in the form `STAGE_DONE: <stage>: <json>` are stable contract
# between this orchestrator and any UI / CI watcher. JSON is single-line
# so a simple line-based stdout reader can split-by-marker.

def _emit_marker(stage: str, **fields) -> None:
    payload = json.dumps(fields, default=str)
    print(f"STAGE_DONE: {stage}: {payload}", flush=True)


# ── Synthesis report ────────────────────────────────────────────────────────


def _save_synthesis_report(app_name: str, summary: dict) -> Path:
    """Combined human-readable summary of all 3 stages. Lives alongside the
    per-stage artifacts and is what the Past-Runs UI surfaces as the
    canonical pipeline result. Saves both .txt (human) and .json (UI)
    side by side; the UI prefers the JSON for structured rendering."""
    now = datetime.now()
    day_dir = Path("artifacts/results") / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d_%H%M%S")
    safe = _safe_app_name(app_name)
    path = day_dir / f"{safe}_pipeline_{ts}.txt"
    json_path = day_dir / f"{safe}_pipeline_{ts}.json"
    json_path.write_text(json.dumps(summary, indent=2, default=str))

    stages = summary.get("stages") or {}
    diag = stages.get("diagnostic") or {}
    ext = stages.get("extract") or {}
    val = stages.get("validate") or {}
    val_summary = val.get("summary") or {}

    lines: list[str] = [
        "Extract Pipeline Report",
        "=" * 60,
        f"App:  {summary.get('app_name', '')}",
        f"URL:  {summary.get('url', '')}",
        "",
        "── Stage 1 / 3: Diagnostic ──────────────────────────────────",
        f"  verdict: {(diag.get('verdict') or 'unknown').upper()}",
        f"  yellow:  {diag.get('yellow_count', 0)} pattern(s)",
        f"  red:     {diag.get('red_count', 0)} pattern(s)",
        f"  report:  {diag.get('txt_path', '')}",
        "",
        "── Stage 2 / 3: Extract ─────────────────────────────────────",
        f"  screen:        {ext.get('screen', 'n/a')}",
        f"  total elements:{ext.get('element_count', 0)}",
        f"  KB:            {ext.get('kb_path', '')}",
    ]
    by_type = ext.get("by_type") or {}
    if by_type:
        lines.append("  breakdown:")
        for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"    {t:<14} {n}")
    lines += [
        "",
        "── Stage 3 / 3: Validate ────────────────────────────────────",
    ]
    if val.get("skipped"):
        lines.append("  skipped — KB had no L1 entries to validate")
    elif val.get("error"):
        lines.append(f"  error: {val['error']}")
    else:
        lines += [
            f"  reachable:    {val_summary.get('reachable', 0)}/{val_summary.get('total', 0)}  ({val_summary.get('reachable_pct', 0)}%)",
            f"  hidden:       {val_summary.get('hidden', 0)}",
            f"  duplicate:    {val_summary.get('duplicate', 0)}",
            f"  broken:       {val_summary.get('broken', 0)}",
            f"  no_locators:  {val_summary.get('no_locators', 0)}",
            f"  label_only:   {val_summary.get('label_only', 0)}",
            f"  report:       {val.get('txt_path', '')}",
        ]

    # Dataset summary — a quick "what's present / missing / hard" digest
    # built from the three stages combined.
    lines += [
        "",
        "── Dataset Summary ──────────────────────────────────────────",
    ]
    present_total = ext.get("element_count", 0)
    reachable_count = val_summary.get("reachable", 0) if val_summary else 0
    hard_count = (
        (val_summary.get("duplicate", 0) if val_summary else 0)
        + (val_summary.get("broken", 0) if val_summary else 0)
        + (val_summary.get("no_locators", 0) if val_summary else 0)
        + (val_summary.get("label_only", 0) if val_summary else 0)
        + (val_summary.get("hidden", 0) if val_summary else 0)
    )
    lines += [
        f"  present:    {present_total} interactable element(s) captured",
        f"  reachable:  {reachable_count}",
        f"  hard:       {hard_count} (broken / duplicate / no-locator / label-only / hidden)",
    ]

    path.write_text("\n".join(lines) + "\n")
    return path


# ── Main pipeline ───────────────────────────────────────────────────────────


async def run_extract_pipeline(
    url: str,
    app_name: str,
    *,
    wait: bool = False,
    screen_name: str = "",
) -> dict:
    """Run all three extract stages on a single Chrome instance.

    Returns a summary dict with per-stage results + the synthesis report
    path. Each stage's artifacts (JSON + TXT) are saved with their
    standard names so existing tooling that reads them keeps working.
    """
    app = TargetApp(platform=Platform.WEB, url=url, app_name=app_name)
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    summary: dict = {
        "url": url,
        "app_name": app_name,
        "stages": {},
        "started_at": datetime.now().isoformat(),
    }

    try:
        if wait:
            print()
            print("=" * 60)
            print("  EXTRACT PIPELINE")
            print("  Browser open. Login + navigate to the page you want to scan.")
            print("  Press Enter once — all 3 stages will then run on this page.")
            print("=" * 60)
            # Print the prompt as its own newline-terminated line so a
            # subprocess parent (Streamlit) sees it via readline() and can
            # surface a Resume button. input()'s prompt arg writes WITHOUT
            # a trailing newline, which blocks line-based stdout readers.
            print("  >>> Press Enter to start... ", flush=True)
            try:
                input()
            except EOFError:
                # Non-interactive shell (e.g., subprocess without stdin).
                # Caller handles this — fall through to scan.
                pass
        else:
            # SPA cold-start settle window. Same as the existing
            # standalone orchestrators.
            await asyncio.sleep(2.5)

        # ── Stage 1: Diagnostic ──────────────────────────────────────────
        print()
        print("=" * 60)
        print("  STAGE 1 / 3: PAGE DIAGNOSTIC")
        print("=" * 60)
        raw = await adapter._call("evaluate_script", {"function": PAGE_DIAGNOSTIC_JS})
        diag_payload = _safe_parse(raw)
        if not isinstance(diag_payload, dict):
            raise RuntimeError(f"diagnostic returned unparseable output: {str(raw)[:500]}")

        diag_text = _format_diagnostic_report(diag_payload)
        print(diag_text)
        diag_json_path, diag_txt_path = _save_diagnostic_artifacts(app_name, diag_payload, diag_text)
        diag_findings = diag_payload.get("findings") or {}
        diag_summary_dict = {
            "verdict": (diag_payload.get("summary") or {}).get("verdict", "unknown"),
            "yellow_count": len(diag_findings.get("yellow") or []),
            "red_count": len(diag_findings.get("red") or []),
            "json_path": str(diag_json_path),
            "txt_path": str(diag_txt_path),
        }
        summary["stages"]["diagnostic"] = diag_summary_dict
        _emit_marker("diagnostic", **diag_summary_dict)

        # ── Stage 2: Exhaustive Extract ──────────────────────────────────
        print()
        print("=" * 60)
        print("  STAGE 2 / 3: EXHAUSTIVE EXTRACT")
        print("=" * 60)
        raw = await adapter._call("evaluate_script", {"function": EXHAUSTIVE_SCAN_JS})
        ext_payload = _safe_parse(raw)
        if not isinstance(ext_payload, dict):
            raise RuntimeError(f"extract scan returned unparseable output: {str(raw)[:500]}")

        await _expand_empty_dropdowns(adapter, ext_payload, max_options=5)
        await _expand_radio_groups(adapter, ext_payload)

        target_screen = (
            screen_name
            or ext_payload.get("page_title")
            or (ext_payload.get("page_url") or "").rsplit("/", 1)[-1]
            or "default"
        )
        kb = _build_kb(app, ext_payload, target_screen)

        # Merge with existing KB if present — same shape as the standalone
        # exhaustive_extract orchestrator: replace the matching screen,
        # keep other screens untouched.
        store = KnowledgeStore()
        existing = store.load(app)
        if existing and existing.screens:
            others = [s for s in existing.screens if s.screen_name != target_screen]
            kb = KnowledgeBase(
                app=app,
                screens=others + kb.screens,
                created_at=existing.created_at,
                updated_at=datetime.now().isoformat(),
            )

        kb_path = store.save(kb)
        _print_extract_summary(kb, kb_path, ext_payload, target_screen)

        target = next(
            (s for s in kb.screens if s.screen_name == target_screen),
            kb.screens[-1] if kb.screens else None,
        )
        by_type: dict[str, int] = {}
        if target:
            for el in target.l0:
                by_type[el.type.value] = by_type.get(el.type.value, 0) + 1

        ext_summary_dict = {
            "screen": target_screen,
            "kb_path": str(kb_path),
            "element_count": len(target.l0) if target else 0,
            "by_type": by_type,
        }
        summary["stages"]["extract"] = ext_summary_dict
        _emit_marker("extract", **{
            k: v for k, v in ext_summary_dict.items()
            if k != "by_type"  # nested dict — keep marker payload flat
        })

        # ── Stage 3: Validate KB ─────────────────────────────────────────
        print()
        print("=" * 60)
        print("  STAGE 3 / 3: KB VALIDATION")
        print("=" * 60)
        entries = _flatten_l1(kb, screen_name=target_screen)
        if not entries:
            print("  No L1 entries — nothing to validate.")
            val_dict = {"skipped": True}
            summary["stages"]["validate"] = val_dict
            _emit_marker("validate", **val_dict)
        else:
            entries_json = json.dumps(entries)
            js = VALIDATE_KB_JS_TEMPLATE.replace("__ENTRIES__", entries_json)
            raw = await adapter._call("evaluate_script", {"function": js})
            val_scan = _safe_parse(raw)
            if not isinstance(val_scan, dict):
                err_msg = f"unparseable output: {str(raw)[:200]}"
                print(f"  ERROR: {err_msg}")
                val_dict = {"error": err_msg}
                summary["stages"]["validate"] = val_dict
                _emit_marker("validate", **val_dict)
            else:
                val_payload, val_text = _build_validate_report(kb, val_scan, entries)
                print()
                print(val_text)
                val_json_path, val_txt_path = _save_validate_artifacts(app_name, val_payload, val_text)
                val_dict = {
                    "summary": val_payload.get("summary", {}),
                    "json_path": str(val_json_path),
                    "txt_path": str(val_txt_path),
                }
                summary["stages"]["validate"] = val_dict
                # Flatten the nested summary into top-level marker fields for easy parsing.
                _emit_marker("validate",
                             json_path=str(val_json_path),
                             txt_path=str(val_txt_path),
                             **(val_payload.get("summary") or {}))

        # ── Synthesis ────────────────────────────────────────────────────
        synth_path = _save_synthesis_report(app_name, summary)
        summary["synthesis_path"] = str(synth_path)
        summary["finished_at"] = datetime.now().isoformat()
        _emit_marker("pipeline", synthesis_path=str(synth_path))

        return summary
    finally:
        await adapter.close()


# ── CLI ─────────────────────────────────────────────────────────────────────


async def _amain() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Extract pipeline — runs page_diagnostic → exhaustive_extract → "
            "validate_kb in one Chrome session. Use --wait for pages that "
            "need manual login."
        ),
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", "-a", required=True, help="App name (KB filename + report grouping)")
    ap.add_argument("--screen-name", "-s", default="", help="Override screen name in KB")
    ap.add_argument(
        "--wait", action="store_true",
        help="Pause once after browser launch for manual login / navigation.",
    )
    args = ap.parse_args()

    summary = await run_extract_pipeline(
        args.url,
        args.app_name,
        wait=args.wait,
        screen_name=args.screen_name,
    )

    print()
    print("=" * 60)
    print("  PIPELINE COMPLETE")
    print("=" * 60)
    print(f"Synthesis report: {summary.get('synthesis_path', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
