# qa/orchestrators/run_smoke.py — Phase 1 smoke-test runner.
#
# Standalone entry point so we can drive the orchestrator end-to-end against
# a real browser without touching the explore pipeline yet. Integration into
# qa/pipelines/explore.py lands in Phase 3.
#
# Usage:
#   python -m qa.orchestrators.run_smoke <url> --app-name TECU [--wait]
#
# --wait pauses after Chrome launches so you can manually navigate to the
# gated page (TECU page 2 requires submitting page 1 first).

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from qa.adapters import make_adapter
from qa.engine.budget import BudgetTracker
from qa.knowledge.file_resolver import _safe_app_dir, discover_test_files
from qa.knowledge.store import KnowledgeStore
from qa.models import (
    ExploreInput,
    KnowledgeBase,
    Platform,
    TargetApp,
)
from qa.orchestrators.base import RunContext
from qa.orchestrators.gated_multi_section import (
    GatedMultiSectionFlow,
    SectionFailed,
)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Path B orchestrator smoke test (Phase 1)")
    ap.add_argument("url", help="Target URL (page 2 for the TECU smoke test)")
    ap.add_argument("--app-name", required=True, help="App name, e.g. 'TECU'")
    ap.add_argument("--model", default="gpt-5.1", help="Model for sub-tasks")
    ap.add_argument(
        "--wait", action="store_true",
        help="Pause after browser launch for manual navigation",
    )
    ap.add_argument("--budget", type=float, default=1.0, help="Max $ budget")
    args = ap.parse_args()

    app = TargetApp(platform=Platform.WEB, url=args.url, app_name=args.app_name)

    # Preload any existing KB so we merge into it rather than overwrite.
    store = KnowledgeStore()
    knowledge = store.load(app) or KnowledgeBase(app=app)
    if knowledge.screens:
        print(f"  Loaded existing KB with {len(knowledge.screens)} screen(s): "
              f"{[s.screen_name for s in knowledge.screens]}")
    else:
        print("  No existing KB — starting fresh.")

    available_files = discover_test_files(args.app_name)
    print(f"  Available test files: {available_files}")
    if not available_files:
        print("  ⚠ No test files found. Drop some into "
              f"artifacts/test_files/{_safe_app_dir(args.app_name)}/ or "
              f"artifacts/test_files/global/ and re-run.")
        return 2

    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    if args.wait:
        print()
        print("=" * 60)
        print("  PAUSE: browser is open. Navigate to the gated page now.")
        print("  When ready, press Enter to start the orchestrator.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to continue... ")
        except EOFError:
            pass

    budget = BudgetTracker(model=args.model, max_budget=args.budget)

    inp = ExploreInput(app=app, model=args.model, budget=args.budget)
    ctx = RunContext(
        adapter=adapter,
        inp=inp,
        knowledge=knowledge,
        budget=budget,
        available_files=available_files,
    )

    flow = GatedMultiSectionFlow()
    status = 0
    captured: list = []
    try:
        captured = await flow.run(ctx)
    except SectionFailed as e:
        print(f"\n  ✗ SectionFailed: {e}")
        print("  Prior sections (if any) were saved before the failure.")
        status = 1
    except Exception as e:
        print(f"\n  ✗ Unhandled error: {type(e).__name__}: {e}")
        status = 2
    finally:
        await adapter.close()

    # Reload KB from disk to report what actually persisted (handles
    # both success and partial-failure cases identically).
    final_kb = store.load(app)
    if final_kb and final_kb.screens:
        print(f"\n  ── Sections in saved KB ──")
        for s in final_kb.screens:
            tag = "✓" if any(c.screen_name == s.screen_name for c in captured) else " "
            print(f"  {tag} {s.screen_name} ({len(s.l0)} elements)")

    print(f"\n  Final cost: ${budget.current_cost:.4f} "
          f"(input={budget.total_input} output={budget.total_output})")

    return status


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
