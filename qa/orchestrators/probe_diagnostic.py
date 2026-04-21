# qa/orchestrators/probe_diagnostic.py — one-shot Wall 2.0 viability probe.
#
# After the Phase 3 revert we need two facts before deciding whether
# Wall 2.0 is salvageable or scoped to a11y-compliant apps:
#
#   1. Does Chrome DevTools MCP's take_snapshot expose the Oxd / Vue
#      custom dropdown elements? If yes, we can use real MCP click (via
#      the uid), which dispatches a trusted CDP mouse event — the only
#      thing that reliably opens Vue components.
#
#   2. What tools does this MCP actually expose? Is there any
#      alternative click path (click_by_selector, coordinate click)?
#
# This script takes ZERO actions. It just navigates, reads, and reports.
#
# Usage:
#   python -m qa.orchestrators.probe_diagnostic \
#     "https://opensource-demo.orangehrmlive.com/web/index.php/pim/viewPersonalDetails/empNumber/7" \
#     --app-name OrangeHRM --wait

import argparse
import asyncio
import re
import sys

from dotenv import load_dotenv

load_dotenv()

from qa.adapters import make_adapter
from qa.models import Platform, TargetApp


async def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Diagnostic: what can Chrome DevTools MCP see on this app?",
    )
    ap.add_argument("url")
    ap.add_argument("--app-name", required=True)
    ap.add_argument("--wait", action="store_true",
                    help="Pause after browser launch for manual navigation")
    args = ap.parse_args()

    app = TargetApp(platform=Platform.WEB, url=args.url, app_name=args.app_name)
    adapter = make_adapter(Platform.WEB)
    await adapter.launch(app)

    if args.wait:
        print()
        print("=" * 60)
        print("  PAUSE: navigate to a form-heavy page (log in if needed).")
        print("  Press Enter when the form is fully visible.")
        print("=" * 60)
        try:
            input("  >>> Press Enter to diagnose... ")
        except EOFError:
            pass

    server = adapter.get_mcp_server()

    # ── Q2: what tools does the MCP expose? ────────────────────────
    print("\n" + "=" * 60)
    print("  Q2: Chrome DevTools MCP available tools")
    print("=" * 60)
    try:
        tools = await server.list_tools()
    except Exception as e:
        print(f"  list_tools failed: {type(e).__name__}: {e}")
        tools = []

    click_related = []
    interaction_related = []
    for t in tools:
        name = getattr(t, "name", str(t))
        desc = getattr(t, "description", "") or ""
        short_desc = desc.split("\n")[0][:100] if desc else ""
        if any(k in name.lower() for k in ("click", "tap", "press", "hover")):
            click_related.append((name, short_desc))
        elif any(k in name.lower() for k in ("fill", "type", "select", "upload",
                                              "drag", "scroll")):
            interaction_related.append((name, short_desc))

    print(f"  Total tools: {len(tools)}")
    print(f"\n  Click/tap/press tools:")
    for name, desc in click_related:
        print(f"    {name:<35} {desc}")
    print(f"\n  Other interaction tools:")
    for name, desc in interaction_related:
        print(f"    {name:<35} {desc}")

    print(f"\n  All tool names: {sorted(getattr(t, 'name', str(t)) for t in tools)}")

    # ── Q1: does the snapshot include Oxd / Vue dropdowns? ──────────
    print("\n" + "=" * 60)
    print("  Q1: snapshot coverage for Vue / Oxd dropdowns")
    print("=" * 60)
    snap_result = await server.call_tool("take_snapshot", {})
    snap_text = ""
    if snap_result.content:
        snap_text = snap_result.content[0].text or ""

    lines = snap_text.split("\n")
    print(f"  Snapshot size: {len(snap_text)} chars, {len(lines)} lines")

    # Count unique roles (first word after `uid=X`)
    role_re = re.compile(r"uid=\S+\s+(\S+)")
    role_counts: dict[str, int] = {}
    for line in lines:
        m = role_re.search(line)
        if m:
            role_counts[m.group(1)] = role_counts.get(m.group(1), 0) + 1
    print(f"\n  Roles observed (top 15):")
    for role, n in sorted(role_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"    {role:<25} {n}")

    # Show lines mentioning the likely dropdown labels
    needles = ["nationality", "marital", "blood type", "department",
               "oxd-select", "select-text", "select-wrapper"]
    print(f"\n  Lines matching dropdown-related needles:")
    for needle in needles:
        hits = [(i, l) for i, l in enumerate(lines)
                if needle in l.lower()][:3]
        if not hits:
            print(f"    {needle!r:<30} 0 hits")
        else:
            print(f"    {needle!r:<30} {len(hits)} hit(s) (first 3):")
            for i, l in hits:
                print(f"      L{i}: {l.strip()[:130]}")

    # For the 4 known dropdown labels, print the surrounding 5 lines
    # (± 5 around the match). The interactive element — whatever Oxd
    # exposes in the a11y tree — is almost certainly adjacent to the
    # label StaticText. Seeing those neighbours tells us exactly which
    # uid to click.
    print(f"\n  Label context (± 5 lines around each label hit):")
    context_labels = ["Nationality", "Marital Status", "Blood Type",
                      "Department Type"]
    for target in context_labels:
        for i, l in enumerate(lines):
            if f'"{target}"' in l:
                start = max(0, i - 5)
                end = min(len(lines), i + 6)
                print(f"\n    ── {target!r} (L{i}) ──")
                for j in range(start, end):
                    marker = "→" if j == i else " "
                    print(f"    {marker} L{j:>3}: {lines[j].rstrip()[:140]}")
                break

    # ── Verdict: does the snapshot give us uids we could click? ─────
    print("\n" + "=" * 60)
    print("  VERDICT")
    print("=" * 60)
    has_clickable_label_lines = any(
        re.search(r"uid=\S+\s+\S+\s+\"([^\"]*(?:nationality|marital|blood|department)[^\"]*)\"",
                  l, re.IGNORECASE)
        for l in lines
    )
    if has_clickable_label_lines:
        print("  ✓ Snapshot contains uid-tagged lines mentioning dropdown labels.")
        print("    Path A viable: expand snapshot parser to use these uids with MCP click.")
    else:
        print("  ✗ No uid-tagged snapshot lines match the dropdown labels.")
        print("    Path A likely not viable for this app.")
        print("    Check tool list above for a coordinate-based click alternative.")

    await adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
