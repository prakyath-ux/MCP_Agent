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
            depends_on=[],
        ))

        locators: list[Locator] = []
        css = str(raw.get("css") or "")
        if css:
            locators.append(Locator(strategy="css", value=css, confidence=0.95))
        for fb in raw.get("css_fallbacks") or []:
            if fb and fb != css:
                locators.append(Locator(strategy="css", value=str(fb), confidence=0.75))
        xpath = str(raw.get("xpath") or "")
        if xpath:
            locators.append(Locator(strategy="xpath", value=xpath, confidence=0.9))
        for fb in raw.get("xpath_fallbacks") or []:
            if fb and fb != xpath:
                locators.append(Locator(strategy="xpath", value=str(fb), confidence=0.7))
        if name:
            locators.append(Locator(strategy="label", value=name, confidence=0.55))

        l1.append(L1Element(
            element_id=eid,
            locators=locators,
            retry_strategy="exhaustive_dom",
            widget_type=str(raw.get("tag") or raw.get("kind") or ""),
            identifier=css or xpath,
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
