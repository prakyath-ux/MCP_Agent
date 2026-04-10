"""
QA Suite — AI-Powered Testing Platform
Run: streamlit run app.py
"""

import json
import subprocess
import time
from pathlib import Path

import streamlit as st
import pandas as pd

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
QA_RESULTS = ROOT / "artifacts" / "results"
QA_KNOWLEDGE = ROOT / "artifacts" / "knowledge"
MOBILE_PASS1 = ROOT / "mobile_version" / "runs" / "pass1"
MOBILE_PASS2 = ROOT / "mobile_version" / "runs" / "pass2"

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="QA Suite", page_icon="", layout="wide", initial_sidebar_state="collapsed")

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    .block-container { padding: 0 3rem 2rem 3rem; max-width: 100%; }
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
    .stApp { background: linear-gradient(180deg, #0f0f1a 0%, #1a1a2e 5%, #f5f5fa 5.1%, #f5f5fa 100%); }

    #MainMenu, footer, header { visibility: hidden; }

    /* Header bar */
    .header-bar {
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #334155 100%);
        padding: 2.5rem 3rem 2rem 3rem; border-radius: 0 0 16px 16px; margin: 0 -3rem 2rem -3rem;
    }
    .header-top { display: flex; justify-content: space-between; align-items: flex-start; }
    div.header-bar h1, div.header-bar h1 * { color: #ffffff !important; font-size: 1.6rem; font-weight: 700; margin: 0; letter-spacing: -0.3px; }
    div.header-bar p.subtitle, div.header-bar p.subtitle * { color: #cbd5e1 !important; font-size: 0.85rem; margin-top: 0.4rem; line-height: 1.5; max-width: 600px; }
    span.header-badge { background: rgba(255,255,255,0.12); color: #e2e8f0 !important; padding: 0.3rem 0.8rem; border-radius: 4px; font-size: 0.65rem; font-weight: 600; letter-spacing: 1.5px; border: 1px solid rgba(255,255,255,0.15); }
    div.header-tags { margin-top: 1.2rem; }
    span.htag { display: inline-block; padding: 0.2rem 0.55rem; margin: 0.15rem 0.1rem; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); border-radius: 3px; font-size: 0.65rem; color: #94a3b8 !important; letter-spacing: 0.2px; }

    /* Section headers */
    .section-header {
        font-size: 1.15rem; font-weight: 700; color: #1a1a2e; margin: 1.5rem 0 0.75rem 0;
        padding-bottom: 0.5rem; border-bottom: 2px solid #e8e8f0;
    }

    /* Feature card */
    .feature-card {
        background: #fff; border: 1px solid #e8e8f0; border-radius: 12px;
        padding: 2rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .feature-card h3 { color: #1a1a2e; font-size: 1.2rem; font-weight: 700; margin: 0 0 0.5rem 0; }
    .feature-card p { color: #555; font-size: 0.9rem; line-height: 1.6; margin: 0; }

    /* Tags */
    .tag {
        display: inline-block; padding: 0.25rem 0.7rem; margin: 0.2rem 0.15rem;
        background: #f0f0f8; border: 1px solid #e0e0ec; border-radius: 6px;
        font-size: 0.72rem; color: #4c1d95; font-weight: 500;
    }

    /* Metric boxes */
    .metric-row { display: flex; gap: 1rem; margin: 1rem 0; }
    .metric-box {
        background: #fff; border: 1px solid #e8e8f0; border-radius: 10px;
        padding: 1.2rem; flex: 1; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .metric-box .value { font-size: 1.6rem; font-weight: 800; color: #1a1a2e; }
    .metric-box .label { font-size: 0.72rem; color: #888; margin-top: 0.3rem; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }

    /* Status pills */
    .status-row { display: flex; gap: 0.75rem; margin: 1rem 0; }
    .status-pill {
        padding: 0.8rem 1.2rem; border-radius: 10px; text-align: center; flex: 1;
    }
    .status-pill .count { font-size: 1.5rem; font-weight: 800; }
    .status-pill .label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; margin-top: 0.2rem; }
    .pill-pass { background: #ecfdf5; border: 1px solid #a7f3d0; color: #059669; }
    .pill-fail { background: #fef2f2; border: 1px solid #fecaca; color: #dc2626; }
    .pill-skip { background: #fefce8; border: 1px solid #fde68a; color: #ca8a04; }

    /* Run cards */
    .run-item {
        background: #fff; border: 1px solid #e8e8f0; border-radius: 10px;
        padding: 1rem 1.25rem; margin-bottom: 0.5rem;
        display: flex; justify-content: space-between; align-items: center;
        box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    }
    .run-item .title { color: #1a1a2e; font-weight: 600; font-size: 0.9rem; }
    .run-item .meta { color: #888; font-size: 0.78rem; margin-top: 0.2rem; }
    .run-item .badge {
        padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.7rem; font-weight: 600;
        background: #ecfdf5; color: #059669; border: 1px solid #a7f3d0;
    }

    /* Pipeline info card */
    .pipe-info {
        background: #f8f7ff; border: 1px solid #e8e0f8; border-radius: 10px;
        padding: 1.5rem; height: 100%;
    }
    .pipe-info h4 { color: #4c1d95; font-size: 1rem; margin: 0 0 0.5rem 0; }
    .pipe-info p { color: #666; font-size: 0.85rem; line-height: 1.5; }

    /* Breadcrumb */
    .breadcrumb { font-size: 0.8rem; color: #888; margin-bottom: 1rem; }
    .breadcrumb a { color: #4c1d95; text-decoration: none; }
</style>
""", unsafe_allow_html=True)


# ── Data Functions ───────────────────────────────────────────────────────────

def list_runs(run_type: str = "results") -> list[Path]:
    if run_type == "results":
        paths = list(QA_RESULTS.rglob("result_*.txt")) if QA_RESULTS.exists() else []
        if MOBILE_PASS2.exists():
            paths += list(MOBILE_PASS2.rglob("output_*.txt"))
            paths += list(MOBILE_PASS2.rglob("report_multi_*.txt"))
        return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    elif run_type == "explore":
        paths = []
        if QA_KNOWLEDGE.exists():
            paths += list(QA_KNOWLEDGE.rglob("*.json"))
        if MOBILE_PASS1.exists():
            paths += list(MOBILE_PASS1.rglob("output_*.txt"))
        return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    return []


def parse_run_header(text: str) -> dict:
    header = {}
    for line in text.splitlines()[:15]:
        if ":" in line and not line.startswith("=") and not line.startswith("#"):
            key, _, val = line.partition(":")
            header[key.strip()] = val.strip()
    return header


def parse_test_results(text: str) -> pd.DataFrame | None:
    """Parse ALL test result tables from the text (supports multi-screen reports)."""
    lines = text.splitlines()
    rows = []
    current_screen = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect screen headers like "# SCREEN: iTeller" or "## Screen: LOAN"
        if ("SCREEN:" in line or "Screen:" in line) and "#" in line:
            current_screen = line.split(":")[-1].strip().rstrip("#").strip()
        # Detect test result table header (flexible whitespace matching)
        stripped = " ".join(line.split())  # normalize whitespace
        if "| # |" in stripped and "Field" in stripped and "Test Case" in stripped:
            i += 2  # Skip header + separator
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].split("|")[1:-1]]
                if len(cells) >= 7:
                    row = {
                        "#": cells[0], "Field": cells[1], "Test Case": cells[2],
                        "Input": cells[3], "Expected": cells[4],
                        "Actual": cells[5], "Status": cells[6],
                        "Notes": cells[7] if len(cells) > 7 else "",
                    }
                    if current_screen:
                        row["Screen"] = current_screen
                    rows.append(row)
                i += 1
            continue
        i += 1
    return pd.DataFrame(rows) if rows else None


def parse_bugs(text: str) -> pd.DataFrame | None:
    """Parse ALL bug tables from the text (supports multi-screen reports)."""
    lines = text.splitlines()
    rows = []
    current_screen = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if ("SCREEN:" in line or "Screen:" in line) and ("#" in line):
            current_screen = line.split(":")[-1].strip().rstrip("#").strip()
        stripped = " ".join(line.split())
        if "| # |" in stripped and "Description" in stripped:
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].split("|")[1:-1]]
                if len(cells) >= 4:
                    row = {
                        "#": cells[0], "Field": cells[1],
                        "Description": cells[2], "Severity": cells[3],
                        "Evidence": cells[4] if len(cells) > 4 else "",
                    }
                    if current_screen:
                        row["Screen"] = current_screen
                    rows.append(row)
                i += 1
            continue
        i += 1
    return pd.DataFrame(rows) if rows else None


def count_statuses(df: pd.DataFrame) -> dict:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    if "Status" in df.columns:
        for val in df["Status"]:
            upper = str(val).upper().strip().lstrip("*")
            if "PASS" in upper:
                counts["PASS"] += 1
            elif "FAIL" in upper:
                counts["FAIL"] += 1
            else:
                counts["SKIP"] += 1
    return counts


def friendly_name(path: Path) -> str:
    name = path.stem
    # Detect type
    if "report_multi" in name:
        run_type = "Multi-Screen"
    elif "result_" in name:
        run_type = "Pipeline Run"
    elif "testcase" in name:
        run_type = "Test Case"
    elif "poc" in name:
        run_type = "Exploration"
    elif "safe_test" in name:
        run_type = "Safe Test"
    else:
        run_type = "Run"

    # Extract model
    model = ""
    for m in ["gpt5.1", "gpt5", "gptoss120b"]:
        if m in name:
            model = m.replace("gpt", "GPT-").replace("oss", "oss-")
            break

    # Extract date/time
    parts = name.split("_")
    for i, p in enumerate(parts):
        if len(p) == 8 and p.isdigit():
            date = f"{p[6:]}/{p[4:6]}/{p[:4]}"
            time_str = parts[i+1][:2] + ":" + parts[i+1][2:4] if i+1 < len(parts) and len(parts[i+1]) >= 4 else ""
            return f"{run_type}  ·  {model}  ·  {date} {time_str}"

    return name


def _take_adb_screenshot() -> bytes | None:
    try:
        result = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, timeout=5)
        if result.returncode == 0 and len(result.stdout) > 100:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _run_agent_subprocess(cmd: str, cwd: str) -> subprocess.Popen:
    venv_python = str(ROOT / "venv" / "bin" / "python")
    full_cmd = cmd.replace("python ", f"{venv_python} ", 1)
    return subprocess.Popen(
        full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd, text=True, bufsize=1,
    )


# ── Session State ────────────────────────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state.page = "home"
if "running" not in st.session_state:
    st.session_state.running = False


# ── Header ───────────────────────────────────────────────────────────────────

def render_header():
    tags = ["Explore", "Plan", "Execute", "Layered KB", "Multi-Screen", "Auto-Explore",
            "Chrome DevTools", "Android / ADB", "DOM Inspection", "Accessibility Tree",
            "CSS Selectors", "Delta Detection"]
    tags_html = " ".join(
        f'<span style="display:inline-block;padding:3px 8px;margin:2px;background:rgba(99,102,241,0.2);border:1px solid rgba(99,102,241,0.35);border-radius:3px;font-size:11px;color:#ffffff;">{t}</span>'
        for t in tags
    )
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 50%,#334155 100%);padding:28px 32px 22px 32px;border-radius:0 0 16px 16px;margin:0 -3rem 2rem -3rem;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
                <h1 style="color:#ffffff;font-size:1.6rem;font-weight:700;margin:0;letter-spacing:-0.3px;">QA Suite</h1>
                <p style="color:#d1d5db;font-size:0.85rem;margin-top:8px;line-height:1.5;max-width:620px;">
                    Autonomous regression testing for web and mobile applications.
                    Three independent pipelines — explore, plan, execute — with a layered
                    knowledge base and multi-screen support.
                </p>
            </div>
            <span style="background:rgba(255,255,255,0.1);color:#d1d5db;padding:4px 10px;border-radius:4px;font-size:11px;font-weight:600;letter-spacing:1.5px;border:1px solid rgba(255,255,255,0.15);">BETA</span>
        </div>
        <div style="margin-top:14px;">
            {tags_html}
        </div>
    </div>
    """, unsafe_allow_html=True)


# ── PAGE: Home ───────────────────────────────────────────────────────────────

def page_home():
    render_header()

    result_files = list_runs("results")
    kb_files = list(QA_KNOWLEDGE.rglob("*.json")) if QA_KNOWLEDGE.exists() else []

    # Metrics
    st.markdown(f"""
    <div class="metric-row">
        <div class="metric-box"><div class="value">{len(result_files)}</div><div class="label">Test Runs</div></div>
        <div class="metric-box"><div class="value">{len(kb_files)}</div><div class="label">Knowledge Bases</div></div>
        <div class="metric-box"><div class="value">3</div><div class="label">Pipelines</div></div>
        <div class="metric-box"><div class="value">2</div><div class="label">Platforms</div></div>
    </div>
    """, unsafe_allow_html=True)

    # Action buttons
    col1, col2, col3 = st.columns(3, gap="medium")
    with col1:
        if st.button("New Run", use_container_width=True, type="primary"):
            st.session_state.page = "new_run"
            st.rerun()
    with col2:
        if st.button("Past Runs", use_container_width=True):
            st.session_state.page = "past_runs"
            st.rerun()
    with col3:
        if st.button("Knowledge Base", use_container_width=True):
            st.session_state.page = "knowledge"
            st.rerun()

    # Recent runs
    if result_files:
        st.markdown('<div class="section-header">Recent Runs</div>', unsafe_allow_html=True)
        for path in result_files[:5]:
            header = parse_run_header(path.read_text())
            model = header.get("Model", "")
            turns = header.get("Turns", header.get("Total Turns", ""))
            dur = header.get("Duration", header.get("Total Duration", ""))
            st.markdown(f"""
            <div class="run-item">
                <div>
                    <div class="title">{friendly_name(path)}</div>
                    <div class="meta">{model}  ·  {turns} turns  ·  {dur}</div>
                </div>
                <div class="badge">Completed</div>
            </div>
            """, unsafe_allow_html=True)


# ── PAGE: New Run ────────────────────────────────────────────────────────────

def page_new_run():
    render_header()

    st.markdown('<div class="breadcrumb"><a href="#">Home</a> / <strong>New Run</strong></div>', unsafe_allow_html=True)

    if st.button("Back"):
        st.session_state.page = "home"
        st.rerun()

    st.markdown('<div class="section-header">Run Configuration</div>', unsafe_allow_html=True)

    col1, col2 = st.columns([1.2, 1])

    with col1:
        pipeline = st.selectbox("Pipeline", ["explore", "plan", "execute", "full"])
        platform = st.selectbox("Platform", ["mobile", "web"])
        target = st.text_input("Target", value="net.impacto.B2U", help="Package name (mobile) or URL (web)")
        app_name = st.text_input("App Name", value="Bank App (B2U)")
        screens = st.text_input("Screens", value="", placeholder="e.g., iTELLER,LOAN,MORE")
        model = st.selectbox("Model", ["gpt-5.1", "gpt-5", "openai/gpt-oss-120b"])
        device_id = ""
        if platform == "mobile":
            device_id = st.text_input("Device ID", value="RZCXA21GV9P")

        cmd = f"python -m qa.cli {pipeline} {target} -p {platform} -a \"{app_name}\" -m {model}"
        if screens:
            cmd += f" -s {screens}"
        if device_id:
            cmd += f" -d {device_id}"
        if pipeline == "execute":
            cmd += " --auto-explore"

    with col2:
        info = {
            "explore": ("Explore / Discovery", "Discovers all screens and elements. Builds the layered knowledge base (L0/L1/L2). Required before planning or execution."),
            "plan": ("Test Planning", "Generates test cases from knowledge. Pure AI reasoning — no device needed. Fastest pipeline."),
            "execute": ("Test Execution", "Executes test cases on the app. Auto-explores if no knowledge exists. Multi-screen support."),
            "full": ("Full Suite", "Runs all 3 pipelines in sequence: Explore, Plan, Execute. End-to-end automation."),
        }
        title, desc = info.get(pipeline, ("", ""))
        st.markdown(f"""
        <div class="pipe-info">
            <h4>{title}</h4>
            <p>{desc}</p>
        </div>
        """, unsafe_allow_html=True)

        if pipeline == "execute":
            st.info("Auto-explore enabled: will discover elements first if no knowledge exists.")
        if pipeline == "full":
            st.success("Will run: Explore → Plan → Execute automatically.")

    st.markdown("---")

    if st.button("Run Test", type="primary", use_container_width=True):
        st.session_state.running = True
        st.session_state.run_cmd = cmd
        st.session_state.run_cwd = str(ROOT)
        st.session_state.run_platform = platform
        st.rerun()

    if st.session_state.get("running"):
        _run_active()


def _run_active():
    cmd = st.session_state.run_cmd
    cwd = st.session_state.run_cwd
    platform = st.session_state.get("run_platform", "mobile")

    st.markdown('<div class="section-header">Running Agent</div>', unsafe_allow_html=True)

    if platform == "mobile":
        col_term, col_screen = st.columns([1.2, 1])
    else:
        col_term = st.container()
        col_screen = None

    with col_term:
        status_text = st.empty()
        progress_bar = st.progress(0.0)
        terminal_output = st.empty()

    screenshot_placeholder = None
    if col_screen and platform == "mobile":
        with col_screen:
            screenshot_placeholder = st.empty()
            img = _take_adb_screenshot()
            if img:
                screenshot_placeholder.image(img, width=380)

    status_text.markdown(f"**Running...**")
    process = _run_agent_subprocess(cmd, cwd)

    output_lines = []
    last_screenshot_time = time.time()

    try:
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                stripped = line.rstrip()
                # Hide cost/usage lines from client view
                skip_keywords = ["USAGE SUMMARY", "Real cost", "No-cache cost", "Savings:", "Cost:", "cost:", "Budget:"]
                if any(kw in stripped for kw in skip_keywords):
                    continue
                output_lines.append(stripped)
                progress_bar.progress(min(len(output_lines) / 100, 0.95))
                terminal_output.code("\n".join(output_lines[-25:]), language="text")

            if screenshot_placeholder and platform == "mobile":
                now = time.time()
                if now - last_screenshot_time > 2.0:
                    img = _take_adb_screenshot()
                    if img:
                        screenshot_placeholder.image(img, width=380)
                    last_screenshot_time = now

        remaining = process.stdout.read()
        if remaining:
            for rem_line in remaining.splitlines():
                if not any(kw in rem_line for kw in ["USAGE SUMMARY", "Real cost", "No-cache", "Savings:", "Cost:"]):
                    output_lines.append(rem_line)
            terminal_output.code("\n".join(output_lines[-30:]), language="text")

    except Exception as e:
        st.error(f"Error: {e}")

    progress_bar.progress(1.0)
    if process.returncode == 0:
        status_text.markdown("**Run completed successfully.**")
    else:
        status_text.markdown(f"**Run finished with exit code {process.returncode}.**")

    if screenshot_placeholder and platform == "mobile":
        img = _take_adb_screenshot()
        if img:
            screenshot_placeholder.image(img, width=380)

    st.session_state.running = False

    st.markdown("---")
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("View Results", type="primary", use_container_width=True):
            st.session_state.page = "past_runs"
            st.rerun()
    with col_b:
        if st.button("Run Another Test", use_container_width=True):
            st.rerun()


# ── PAGE: Past Runs ──────────────────────────────────────────────────────────

def page_past_runs():
    render_header()

    st.markdown('<div class="breadcrumb"><a href="#">Home</a> / <strong>Past Runs</strong></div>', unsafe_allow_html=True)

    if st.button("Back"):
        st.session_state.page = "home"
        st.rerun()

    st.markdown('<div class="section-header">Test Results</div>', unsafe_allow_html=True)

    tab_mobile, tab_web = st.tabs(["Mobile", "Web"])

    with tab_mobile:
        _render_run_list("mobile")

    with tab_web:
        _render_run_list("web")


def _render_run_list(platform: str):
    runs = list_runs("results")
    filtered = [r for r in runs if platform in r.name.lower() or platform in str(r.parent).lower()]

    if not filtered:
        st.info(f"No {platform} runs found yet. Start a new run first.")
        return

    selected = st.selectbox(
        "Select run",
        range(len(filtered)),
        format_func=lambda i: friendly_name(filtered[i]),
        key=f"run_{platform}",
    )

    run_path = filtered[selected]
    text = run_path.read_text()

    if run_path.suffix == ".json":
        st.json(json.loads(text))
        return

    header = parse_run_header(text)

    # Metrics — NO COST shown
    turns = header.get("Turns", header.get("Total Turns", "?"))
    turns_val = turns.split("/")[0].strip() if "/" in str(turns) else turns
    dur = header.get("Duration", header.get("Total Duration", "?"))
    model = header.get("Model", "?")
    cache = header.get("Cache Hit", "")

    metrics_html = f"""
    <div class="metric-row">
        <div class="metric-box"><div class="value">{turns_val}</div><div class="label">Turns</div></div>
        <div class="metric-box"><div class="value">{dur}</div><div class="label">Duration</div></div>
        <div class="metric-box"><div class="value">{model}</div><div class="label">Model</div></div>
    """
    if cache:
        metrics_html += f'<div class="metric-box"><div class="value">{cache}</div><div class="label">Cache Hit</div></div>'
    metrics_html += "</div>"
    st.markdown(metrics_html, unsafe_allow_html=True)

    # Test results + bugs — grouped by screen
    test_df = parse_test_results(text)
    bugs_df = parse_bugs(text)

    if test_df is not None and not test_df.empty:
        # Check if multi-screen
        has_screens = "Screen" in test_df.columns and test_df["Screen"].nunique() > 1

        # Overall totals
        total_statuses = count_statuses(test_df)
        total_tests = total_statuses["PASS"] + total_statuses["FAIL"] + total_statuses["SKIP"]
        screen_count = test_df["Screen"].nunique() if has_screens else 1

        st.markdown(f'<div class="section-header">Overall Results — {total_tests} tests across {screen_count} screen{"s" if screen_count > 1 else ""}</div>', unsafe_allow_html=True)
        st.markdown(f"""
        <div class="status-row">
            <div class="status-pill pill-pass"><div class="count">{total_statuses['PASS']}</div><div class="label">Passed</div></div>
            <div class="status-pill pill-fail"><div class="count">{total_statuses['FAIL']}</div><div class="label">Failed</div></div>
            <div class="status-pill pill-skip"><div class="count">{total_statuses['SKIP']}</div><div class="label">Skipped</div></div>
        </div>
        """, unsafe_allow_html=True)

        if has_screens:
            st.markdown(f'<div class="section-header">Results by Screen</div>', unsafe_allow_html=True)

            screen_names = test_df["Screen"].unique().tolist()
            screen_tabs = st.tabs(screen_names)

            for tab, screen in zip(screen_tabs, screen_names):
                with tab:
                    screen_df = test_df[test_df["Screen"] == screen].drop(columns=["Screen"])
                    screen_statuses = count_statuses(screen_df)

                    st.markdown(f"""
                    <div class="status-row">
                        <div class="status-pill pill-pass"><div class="count">{screen_statuses['PASS']}</div><div class="label">Passed</div></div>
                        <div class="status-pill pill-fail"><div class="count">{screen_statuses['FAIL']}</div><div class="label">Failed</div></div>
                        <div class="status-pill pill-skip"><div class="count">{screen_statuses['SKIP']}</div><div class="label">Skipped</div></div>
                    </div>
                    """, unsafe_allow_html=True)

                    st.dataframe(screen_df, use_container_width=True, hide_index=True, height=min(len(screen_df) * 40 + 40, 400))

                    # Bugs for this screen
                    if bugs_df is not None and "Screen" in bugs_df.columns:
                        screen_bugs = bugs_df[bugs_df["Screen"] == screen].drop(columns=["Screen"])
                        if not screen_bugs.empty:
                            st.markdown(f'<div class="section-header">Bugs — {screen} ({len(screen_bugs)})</div>', unsafe_allow_html=True)
                            st.dataframe(screen_bugs, use_container_width=True, hide_index=True)
        else:
            # Single screen — flat table
            st.markdown('<div class="section-header">Test Case Results</div>', unsafe_allow_html=True)
            display_df = test_df.drop(columns=["Screen"], errors="ignore")
            st.dataframe(display_df, use_container_width=True, hide_index=True, height=min(len(display_df) * 40 + 40, 500))

            if bugs_df is not None and not bugs_df.empty:
                st.markdown(f'<div class="section-header">Bugs Found ({len(bugs_df)})</div>', unsafe_allow_html=True)
                display_bugs = bugs_df.drop(columns=["Screen"], errors="ignore")
                st.dataframe(display_bugs, use_container_width=True, hide_index=True)

    # Bugs without test results (edge case)
    elif bugs_df is not None and not bugs_df.empty:
        st.markdown(f'<div class="section-header">Bugs Found ({len(bugs_df)})</div>', unsafe_allow_html=True)
        st.dataframe(bugs_df, use_container_width=True, hide_index=True)

    # Full output
    with st.expander("Full Agent Output"):
        # Strip cost lines from display
        clean_lines = []
        for line in text.splitlines():
            if any(kw in line for kw in ["Real Cost", "No-Cache Cost", "Savings:", "real_cost", "nocache_cost"]):
                continue
            clean_lines.append(line)
        st.text("\n".join(clean_lines))


# ── PAGE: Knowledge Base ─────────────────────────────────────────────────────

def page_knowledge():
    render_header()

    st.markdown('<div class="breadcrumb"><a href="#">Home</a> / <strong>Knowledge Base</strong></div>', unsafe_allow_html=True)

    if st.button("Back"):
        st.session_state.page = "home"
        st.rerun()

    st.markdown('<div class="section-header">Knowledge Base</div>', unsafe_allow_html=True)

    tab_mobile, tab_web = st.tabs(["Mobile", "Web"])

    for tab, platform_dir in [(tab_mobile, "mobile"), (tab_web, "web")]:
        with tab:
            kb_dir = QA_KNOWLEDGE / platform_dir
            if not kb_dir.exists() or not list(kb_dir.glob("*.json")):
                st.info(f"No {platform_dir} knowledge bases found. Run an explore pipeline first.")
                continue

            for f in sorted(kb_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text())
                    app_name = data.get("app", {}).get("app_name", f.stem)
                    screens = [s.get("screen_name", "?") for s in data.get("screens", [])]
                    total_elements = sum(len(s.get("l0", [])) for s in data.get("screens", []))
                    updated = data.get("updated_at", "")[:10]

                    st.markdown(f"""
                    <div class="feature-card">
                        <h3>{app_name}</h3>
                        <p><strong>{len(screens)}</strong> screen(s): {', '.join(screens)}<br>
                        <strong>{total_elements}</strong> elements discovered  ·  Last updated: {updated}</p>
                    </div>
                    """, unsafe_allow_html=True)

                    with st.expander(f"View element details — {f.name}"):
                        for screen in data.get("screens", []):
                            st.markdown(f"**{screen.get('screen_name', '?')}**")
                            l0 = screen.get("l0", [])
                            if l0:
                                l0_df = pd.DataFrame([{
                                    "Element": el.get("name", ""),
                                    "Type": el.get("type", ""),
                                    "Required": "Yes" if el.get("required") else "",
                                    "Behavior": el.get("behavior", "")[:60],
                                    "Options": ", ".join(el.get("options", []))[:40],
                                } for el in l0])
                                st.dataframe(l0_df, use_container_width=True, hide_index=True)

                except (json.JSONDecodeError, KeyError):
                    st.warning(f"Could not parse {f.name}")


# ── Router ───────────────────────────────────────────────────────────────────

page = st.session_state.page
match page:
    case "home": page_home()
    case "new_run": page_new_run()
    case "past_runs": page_past_runs()
    case "knowledge": page_knowledge()
    case _: page_home()
