# qa/orchestrators/extract_only.py — single-pass extraction harness
#
# Runs ONE extract_form pass against the page the user lands on after
# manual login. No fill, no clicks, no decision-group probing — read-only.
# Use this to validate extraction quality on a new app before committing
# to a full agent_run integration.
#
# Usage:
#   python -m qa.orchestrators.extract_only URL --app-name FORGENITE --wait
#
# The captured screen is appended to the app's KB (artifacts/knowledge/web/{app}.json)
# so subsequent runs build on it. No audit log — this is a one-shot.

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
from qa.config import load_defaults
from qa.engine.budget import BudgetTracker
from qa.knowledge.store import KnowledgeStore
from qa.models import KnowledgeBase, Platform, TargetApp
from qa.orchestrators.form_extract import extract_form


def _summarize_screen(screen) -> dict:
    """Aggregate counts + a flat element list for the post-run printout.
    Mirrors what wizard_step would log when it captures, minus the fill
    bookkeeping."""
    by_type: dict[str, int] = {}
    by_section: dict[str, int] = {}
    items: list[dict] = []
    for el in screen.l0:
        tname = el.type.value if hasattr(el.type, "value") else str(el.type)
        by_type[tname] = by_type.get(tname, 0) + 1
        sec = (getattr(el, "screen_name", "") or "(no section)").strip() or "(no section)"
        by_section[sec] = by_section.get(sec, 0) + 1
        items.append({
            "name": el.name,
            "type": tname,
            "required": getattr(el, "required", False),
            "section": sec,
            "id": getattr(el, "element_id", ""),
        })
    return {
        "screen_name": screen.screen_name,
        "url": screen.screen_url,
        "total": len(screen.l0),
        "by_type": by_type,
        "by_section": by_section,
        "items": items,
    }


async def run_extract_only(
    url: str,
    app_name: str,
    *,
    wait_for_manual_nav: bool,
    model: str,
    dump_json: bool,
) -> int:
    app = TargetApp(platform=Platform.WEB, url=url, app_name=app_name)

    defaults = load_defaults(app_name)
    print(f"  {defaults.summary()}")

    store = KnowledgeStore()
    kb = store.load(app) or KnowledgeBase(app=app)
    if kb.screens:
        print(
            f"  Loaded KB with {len(kb.screens)} screen(s): "
            f"{[s.screen_name for s in kb.screens]}"
        )

    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    if wait_for_manual_nav:
        print()
        print("=" * 60)
        print("  PAUSE: log in (if needed) and navigate to the target page.")
        print("  Press Enter when the page is fully rendered.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to extract... ")
        except EOFError:
            pass

    budget = BudgetTracker(model=model, max_budget=2.0)

    # Resolve a screen_name from page title → main heading → fallback.
    # Same heuristic wizard_step uses, lifted out so the standalone
    # script doesn't depend on strategy code.
    from qa.tools.web_tools import _safe_parse

    title_raw = await adapter.evaluate_script("() => document.title")
    parsed = _safe_parse(title_raw)
    screen_name = (str(parsed) if parsed else "").strip()
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
        screen_name = "Extract-Only Page"

    print(f"  Resolved screen_name: {screen_name!r}")
    print(f"  Running extract_form (read-only, no clicks beyond dropdown-open)...")
    print()

    screen = await extract_form(
        adapter=adapter,
        app_name=app_name,
        screen_name=screen_name,
        budget=budget,
        page_url=url,
        defaults=defaults,
    )

    # Persist to KB. Replace any prior capture under the same screen_name
    # so a re-run on the same page doesn't accumulate stale variants.
    existing = kb.get_screen(screen.screen_name)
    if existing:
        kb.screens = [s for s in kb.screens if s.screen_name != screen.screen_name]
    kb.screens.append(screen)
    store.save(kb)

    summary = _summarize_screen(screen)

    print()
    print("=" * 60)
    print(f"  Extraction complete — {summary['total']} element(s)")
    print("=" * 60)
    print(f"  screen_name: {summary['screen_name']!r}")
    print(f"  url:         {summary['url']!r}")
    print()
    print("  By type:")
    for t, c in sorted(summary["by_type"].items(), key=lambda x: -x[1]):
        print(f"    {t:20} {c}")
    print()
    print("  By section heading:")
    for s, c in sorted(summary["by_section"].items(), key=lambda x: -x[1]):
        print(f"    {s[:40]:40} {c}")
    print()
    print("  Elements (DOM order):")
    for it in summary["items"]:
        req = "*" if it["required"] else " "
        print(f"    {req} [{it['type']:14}] {it['section'][:25]:25}  {it['name']}")

    print()
    print(f"  KB saved → artifacts/knowledge/web/{app_name.lower()}.json")
    print(f"  LLM cost: ${budget.current_cost:.4f}")

    if dump_json:
        out = Path("artifacts/results") / f"{app_name.lower()}_extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, default=str))
        print(f"  Summary JSON → {out}")

    await adapter.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="extract_only",
        description="Single-pass read-only extraction for testing form_extract on a new app.",
    )
    ap.add_argument("url", help="Target URL")
    ap.add_argument("--app-name", required=True, help="App name (used for KB filename + defaults lookup)")
    ap.add_argument("--wait", action="store_true", help="Pause for manual login/navigation before extracting")
    ap.add_argument("--model", default="gpt-5.1", help="Model id for the few LLM calls extract_form makes (dropdown-options extraction)")
    ap.add_argument("--dump-json", action="store_true", help="Also write the summary as JSON under artifacts/results/")
    args = ap.parse_args()

    try:
        return asyncio.run(run_extract_only(
            url=args.url,
            app_name=args.app_name,
            wait_for_manual_nav=args.wait,
            model=args.model,
            dump_json=args.dump_json,
        ))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
