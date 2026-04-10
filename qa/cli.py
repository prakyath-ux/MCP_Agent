# qa/cli.py — Unified CLI for the QA testing suite
# Usage:
#   python -m qa.cli explore net.impacto.B2U -p mobile -d RZCXA21GV9P -s iTELLER,LOAN
#   python -m qa.cli plan "Bank App (B2U)"
#   python -m qa.cli execute net.impacto.B2U -p mobile --auto-explore
#   python -m qa.cli full net.impacto.B2U -p mobile -d RZCXA21GV9P

import argparse
import asyncio

from qa.models import Platform, TargetApp, ExploreInput, PlanInput, ExecuteInput
from qa.knowledge.store import KnowledgeStore


def main() -> None:
    parser = argparse.ArgumentParser(description="QA Testing Suite", prog="qa")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── explore ──────────────────────────────────────────────
    p_explore = sub.add_parser("explore", help="Pipeline 1: Discover screens and elements")
    p_explore.add_argument("target", help="URL (web) or package name (mobile)")
    p_explore.add_argument("--platform", "-p", choices=["web", "mobile"], required=True)
    p_explore.add_argument("--app-name", "-a", default="")
    p_explore.add_argument("--device", "-d", default="")
    p_explore.add_argument("--screens", "-s", default="", help="Comma-separated screen names")
    p_explore.add_argument("--model", "-m", default="gpt-5.1")
    p_explore.add_argument("--provider", default="")
    p_explore.add_argument("--budget", type=float, default=2.0)
    p_explore.add_argument("--max-turns", type=int, default=20)

    # ── plan ─────────────────────────────────────────────────
    p_plan = sub.add_parser("plan", help="Pipeline 2: Generate test cases from knowledge")
    p_plan.add_argument("target", help="App name or package name (to find knowledge)")
    p_plan.add_argument("--platform", "-p", default="", help="Filter by platform")
    p_plan.add_argument("--model", "-m", default="gpt-5.1")
    p_plan.add_argument("--provider", default="")
    p_plan.add_argument("--max-cases", type=int, default=30)
    p_plan.add_argument("--screens", "-s", default="", help="Comma-separated screen names")

    # ── execute ──────────────────────────────────────────────
    p_exec = sub.add_parser("execute", help="Pipeline 3: Execute test cases")
    p_exec.add_argument("target", help="URL or package name")
    p_exec.add_argument("--platform", "-p", choices=["web", "mobile"], required=True)
    p_exec.add_argument("--app-name", "-a", default="")
    p_exec.add_argument("--device", "-d", default="")
    p_exec.add_argument("--screens", "-s", default="", help="Comma-separated screen names")
    p_exec.add_argument("--auto-explore", action="store_true", help="Run explore first if no knowledge")
    p_exec.add_argument("--model", "-m", default="gpt-5.1")
    p_exec.add_argument("--provider", default="")
    p_exec.add_argument("--budget", type=float, default=2.0)
    p_exec.add_argument("--max-turns", type=int, default=15)

    # ── full ─────────────────────────────────────────────────
    p_full = sub.add_parser("full", help="Run all 3 pipelines end-to-end")
    p_full.add_argument("target", help="URL or package name")
    p_full.add_argument("--platform", "-p", choices=["web", "mobile"], required=True)
    p_full.add_argument("--app-name", "-a", default="")
    p_full.add_argument("--device", "-d", default="")
    p_full.add_argument("--screens", "-s", default="", help="Comma-separated screen names")
    p_full.add_argument("--model", "-m", default="gpt-5.1")
    p_full.add_argument("--provider", default="")
    p_full.add_argument("--budget", type=float, default=5.0)

    args = parser.parse_args()

    match args.command:
        case "explore":
            asyncio.run(_cmd_explore(args))
        case "plan":
            asyncio.run(_cmd_plan(args))
        case "execute":
            asyncio.run(_cmd_execute(args))
        case "full":
            asyncio.run(_cmd_full(args))


async def _cmd_explore(args) -> None:
    from qa.pipelines.explore import run_explore

    app = _build_app(args)
    screens = [s.strip() for s in args.screens.split(",") if s.strip()]

    inp = ExploreInput(
        app=app,
        screens=screens,
        model=args.model,
        provider=args.provider,
        max_turns=args.max_turns,
        budget=args.budget,
    )

    result = await run_explore(inp)
    print(f"\n  Done. {len(result.knowledge.screens)} screen(s), "
          f"${result.cost_usd:.4f}, {result.duration_sec:.1f}s")


async def _cmd_plan(args) -> None:
    from qa.pipelines.plan import run_plan

    store = KnowledgeStore()
    kb = store.load_by_name(args.target, args.platform)

    if not kb:
        print(f"  ERROR: No knowledge found for '{args.target}'")
        print(f"  Run 'explore' first.")
        return

    screens = [s.strip() for s in args.screens.split(",") if s.strip()] if args.screens else []

    inp = PlanInput(
        knowledge=kb,
        screen_names=screens,
        max_total_cases=args.max_cases,
        model=args.model,
        provider=args.provider,
    )

    result = await run_plan(inp)
    print(f"\n  Done. {len(result.test_cases)} test cases generated, {result.duration_sec:.1f}s")


async def _cmd_execute(args) -> None:
    from qa.pipelines.execute import run_execute

    app = _build_app(args)
    screens = [s.strip() for s in args.screens.split(",") if s.strip()]

    store = KnowledgeStore()
    kb = store.load_by_name(app.app_name, app.platform.value) or KnowledgeBase(app=app)

    inp = ExecuteInput(
        app=app,
        knowledge=kb,
        auto_explore=args.auto_explore,
        screens=screens,
        model=args.model,
        provider=args.provider,
        max_turns_per_screen=args.max_turns,
        budget=args.budget,
    )

    result = await run_execute(inp)
    print(f"\n  Done. ${result.cost_usd:.4f}, {result.turns_used} turns, {result.duration_sec:.1f}s")


async def _cmd_full(args) -> None:
    """Run all 3 pipelines: explore → plan → execute."""
    from qa.pipelines.explore import run_explore
    from qa.pipelines.plan import run_plan
    from qa.pipelines.execute import run_execute

    app = _build_app(args)
    screens = [s.strip() for s in args.screens.split(",") if s.strip()]

    print(f"\n{'='*60}")
    print(f"  FULL SUITE: explore → plan → execute")
    print(f"  Target: {args.target}")
    print(f"  Platform: {args.platform}")
    print(f"  Model: {args.model}")
    print(f"{'='*60}")

    # Pipeline 1: Explore
    explore_result = await run_explore(ExploreInput(
        app=app,
        screens=screens,
        model=args.model,
        provider=args.provider,
        budget=args.budget / 3,
    ))

    # Pipeline 2: Plan
    plan_result = await run_plan(PlanInput(
        knowledge=explore_result.knowledge,
        model=args.model,
        provider=args.provider,
    ))

    # Pipeline 3: Execute
    exec_result = await run_execute(ExecuteInput(
        app=app,
        test_cases=plan_result.test_cases,
        knowledge=explore_result.knowledge,
        screens=screens,
        model=args.model,
        provider=args.provider,
        budget=args.budget * 2 / 3,
    ))

    total_cost = explore_result.cost_usd + plan_result.cost_usd + exec_result.cost_usd
    print(f"\n{'='*60}")
    print(f"  FULL SUITE COMPLETE")
    print(f"  Explore: ${explore_result.cost_usd:.4f} ({explore_result.turns_used} turns)")
    print(f"  Plan:    ${plan_result.cost_usd:.4f}")
    print(f"  Execute: ${exec_result.cost_usd:.4f} ({exec_result.turns_used} turns)")
    print(f"  Total:   ${total_cost:.4f}")
    print(f"{'='*60}")


def _build_app(args) -> TargetApp:
    """Build TargetApp from CLI args."""
    platform = Platform(args.platform)
    app_name = args.app_name or args.target

    if platform == Platform.WEB:
        return TargetApp(platform=platform, url=args.target, app_name=app_name)
    else:
        return TargetApp(
            platform=platform,
            package_name=args.target,
            app_name=app_name,
            device_id=getattr(args, "device", ""),
        )


# Need this import for _cmd_execute when KB doesn't exist
from qa.models import KnowledgeBase


if __name__ == "__main__":
    main()
