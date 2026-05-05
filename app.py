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

from dotenv import load_dotenv
load_dotenv()

from qa.knowledge.store import KnowledgeStore

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
    # start_new_session so we can kill the whole process tree (including npx MCP children)
    return subprocess.Popen(
        full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd, text=True, bufsize=1, start_new_session=True,
    )


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Kill the subprocess and all its children (npx, node, adb, etc.)."""
    import os
    import signal
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=3)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


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

    # Main actions — two big primary buttons
    col1, col2 = st.columns(2, gap="medium")
    with col1:
        if st.button("New Run", use_container_width=True, type="primary"):
            st.session_state.page = "new_run"
            st.rerun()
    with col2:
        if st.button("💬 Chat with Agent", use_container_width=True, type="primary"):
            st.session_state.page = "chat"
            st.rerun()

    # Past runs — small secondary button
    st.markdown("")
    spacer, col_past = st.columns([3, 1])
    with col_past:
        if st.button("Past Runs →", use_container_width=True):
            st.session_state.page = "past_runs"
            st.rerun()

    # Recent runs — collapsed by default
    if result_files:
        with st.expander(f"Browse Recent Runs ({len(result_files)} total)", expanded=False):
            for path in result_files[:10]:
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
    import threading
    from queue import Queue, Empty

    cmd = st.session_state.run_cmd
    cwd = st.session_state.run_cwd
    platform = st.session_state.get("run_platform", "mobile")

    st.markdown('<div class="section-header">Running Agent</div>', unsafe_allow_html=True)

    # Kill button at the top — always accessible
    if st.button("⛔ Kill Run", key="kill_btn", type="secondary"):
        proc = st.session_state.get("_run_process")
        if proc and proc.poll() is None:
            _kill_process_tree(proc)
        st.session_state.running = False
        st.session_state.pop("_run_process", None)
        st.warning("Run killed.")
        st.rerun()

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
    st.session_state["_run_process"] = process

    # Background thread reads stdout into a queue — non-blocking
    output_queue: Queue = Queue()

    def _reader():
        try:
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    output_queue.put(line)
            remaining = process.stdout.read()
            if remaining:
                for rem in remaining.splitlines():
                    output_queue.put(rem)
            output_queue.put(None)  # sentinel
        except Exception as exc:
            output_queue.put(f"[reader error: {exc}]")
            output_queue.put(None)

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()

    output_lines: list[str] = []
    last_screenshot_time = time.time()
    skip_keywords = ["USAGE SUMMARY", "Real cost", "No-cache cost", "Savings:", "Cost:", "cost:", "Budget:"]
    done = False

    try:
        while not done:
            # Drain queue — non-blocking
            got_new = False
            while True:
                try:
                    line = output_queue.get_nowait()
                except Empty:
                    break
                if line is None:
                    done = True
                    break
                stripped = str(line).rstrip()
                if any(kw in stripped for kw in skip_keywords):
                    continue
                output_lines.append(stripped)
                got_new = True

            if got_new:
                progress_bar.progress(min(len(output_lines) / 100, 0.95))
                terminal_output.code("\n".join(output_lines[-25:]), language="text")

            # Update screenshot
            if screenshot_placeholder and platform == "mobile":
                now = time.time()
                if now - last_screenshot_time > 2.0:
                    img = _take_adb_screenshot()
                    if img:
                        screenshot_placeholder.image(img, width=380)
                    last_screenshot_time = now

            # Check if kill was requested
            if not st.session_state.get("running", True):
                if process.poll() is None:
                    _kill_process_tree(process)
                break

            # Check if subprocess died
            if process.poll() is not None and output_queue.empty():
                done = True

            time.sleep(0.3)

    except Exception as e:
        st.error(f"Error: {e}")

    progress_bar.progress(1.0)
    if process.returncode == 0:
        status_text.markdown("**Run completed successfully.**")
    elif process.returncode is None:
        status_text.markdown("**Run was killed.**")
    else:
        status_text.markdown(f"**Run finished with exit code {process.returncode}.**")

    if screenshot_placeholder and platform == "mobile":
        img = _take_adb_screenshot()
        if img:
            screenshot_placeholder.image(img, width=380)

    st.session_state.running = False
    st.session_state.pop("_run_process", None)

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

    def _matches_platform(path: Path) -> bool:
        # Old-style paths have "mobile" or "web" in the path
        path_str = str(path).lower()
        if platform in path_str:
            return True
        # New qa pipeline result files — read "Platform:" line from header
        try:
            for line in path.read_text().splitlines()[:10]:
                if line.lower().startswith("platform:"):
                    return platform in line.lower()
        except Exception:
            pass
        # Fallback: mobile pipeline results live under artifacts/results (no platform in name)
        # Treat as mobile by default since that's our primary platform
        return platform == "mobile"

    filtered = [r for r in runs if _matches_platform(r)]

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


# ── PAGE: Chat ───────────────────────────────────────────────────────────────

def page_chat():
    render_header()

    st.markdown('<div class="breadcrumb"><a href="#">Home</a> / <strong>Chat with Agent</strong></div>', unsafe_allow_html=True)

    if st.button("Back"):
        st.session_state.page = "home"
        st.rerun()

    # ── Context setup (one-time per session) ─────────────────────
    if "chat_context" not in st.session_state:
        st.session_state.chat_context = None
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    if st.session_state.chat_context is None:
        st.markdown('<div class="section-header">Setup — Tell me about the app</div>', unsafe_allow_html=True)
        st.caption("One-time setup. After this, you can chat in plain English to test your app.")

        col1, col2 = st.columns(2)
        with col1:
            ctx_app_name = st.text_input("App Name", value="Bank App (B2U)")
            ctx_platform = st.selectbox("Platform", ["mobile", "web"])
            ctx_target = st.text_input("Package / URL", value="net.impacto.B2U", help="Package name (mobile) or URL (web)")
        with col2:
            ctx_device = ""
            if ctx_platform == "mobile":
                ctx_device = st.text_input("Device ID", value="RZCXA21GV9P")
            ctx_model = st.selectbox("Model", ["gpt-5.1", "gpt-5", "openai/gpt-oss-120b"])

        st.markdown("")
        if st.button("Start Chat", type="primary", use_container_width=True):
            st.session_state.chat_context = {
                "app_name": ctx_app_name,
                "platform": ctx_platform,
                "target": ctx_target,
                "device": ctx_device,
                "model": ctx_model,
            }

            # Load knowledge to greet user with what we know
            store = KnowledgeStore()
            kb = store.load_by_name(ctx_target, ctx_platform) or store.load_by_name(ctx_app_name, ctx_platform)
            if kb and kb.screens:
                screens_str = ", ".join(kb.screen_names())
                greeting = (
                    f"Hi! I'm ready to test **{ctx_app_name}** on **{ctx_platform}**.\n\n"
                    f"I have knowledge of these screens: **{screens_str}**.\n\n"
                    f"You can ask me things like:\n"
                    f"- *Test all dropdowns on iTELLER*\n"
                    f"- *Run tests on the Date of Birth field*\n"
                    f"- *Test the LOAN screen*\n\n"
                    f"What would you like me to do?"
                )
            else:
                greeting = (
                    f"Hi! I'm ready for **{ctx_app_name}** on **{ctx_platform}**.\n\n"
                    f"⚠ I don't have knowledge of this app yet. "
                    f"You'll need to run *explore* first or just say *explore the app*."
                )

            st.session_state.chat_messages = [
                {"role": "assistant", "content": greeting}
            ]
            st.rerun()
        return

    # ── Active chat ──────────────────────────────────────────────
    ctx = st.session_state.chat_context

    # Context bar
    st.markdown(f"""
    <div style="background:#fff;border:1px solid #e8e8f0;border-radius:8px;padding:0.75rem 1rem;margin-bottom:1rem;display:flex;justify-content:space-between;align-items:center;">
        <div style="font-size:0.85rem;color:#555;">
            <strong style="color:#1a1a2e;">{ctx['app_name']}</strong>
            <span style="color:#888;"> · {ctx['platform']} · {ctx['model']}</span>
            {' · ' + ctx['device'] if ctx['device'] else ''}
        </div>
    </div>
    """, unsafe_allow_html=True)

    col_clear, col_change = st.columns([1, 1])
    with col_clear:
        if st.button("Clear Chat", use_container_width=True):
            st.session_state.chat_messages = []
            st.rerun()
    with col_change:
        if st.button("Change App", use_container_width=True):
            st.session_state.chat_context = None
            st.session_state.chat_messages = []
            st.rerun()

    st.markdown('<div class="section-header">Chat</div>', unsafe_allow_html=True)

    # Display conversation
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input
    if user_input := st.chat_input("Tell me what to test..."):
        st.session_state.chat_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                response = _handle_chat_message(user_input, ctx)
            st.markdown(response)
        st.session_state.chat_messages.append({"role": "assistant", "content": response})


def _build_explore_report(ctx: dict, output_lines: list[str]) -> str:
    """Post-extract summary shown in chat after an EXPLORE run completes.
    Parses subprocess stdout for which screen(s) were scanned and the
    completeness verdict, then reads the updated KB for authoritative
    element counts and a per-type breakdown."""
    import re
    from qa.models import TargetApp, Platform

    # Parse subprocess stdout for scan + completeness signals.
    scanned_screens: list[str] = []
    completeness_lines: list[str] = []
    for line in output_lines:
        m = re.search(r"Scanning '([^']+)'", line)
        if m:
            scanned_screens.append(m.group(1))
        if "Completeness:" in line:
            cleaned = re.sub(r"^\s*\[form\]\s*", "", line).strip()
            completeness_lines.append(cleaned)

    # Load the updated KB off disk. Using TargetApp lets KnowledgeStore
    # handle the platform-specific path resolution.
    try:
        platform = Platform(ctx["platform"])
        app = TargetApp(
            platform=platform,
            url=ctx["target"] if platform == Platform.WEB else None,
            package_name=ctx["target"] if platform == Platform.MOBILE else None,
            app_name=ctx["app_name"],
        )
        kb = KnowledgeStore().load(app)
    except Exception as e:
        return f"\n\n## ⚠ Extract completed but report failed\n`{e}`"

    if not kb or not kb.screens:
        return (
            "\n\n## ⚠ Extract completed but no knowledge was saved\n"
            "Nothing captured — check the log above for errors "
            "(common causes: blank page, Chrome launch failure, empty snapshot)."
        )

    # Per-type breakdown across the whole KB (not just what we scanned
    # this run — KB persists across runs and merges new screens).
    type_counts: dict[str, int] = {}
    for screen in kb.screens:
        for el in screen.l0:
            type_counts[el.type.value] = type_counts.get(el.type.value, 0) + 1
    total_elements = sum(len(s.l0) for s in kb.screens)

    plural = {
        "text_input": ("text input", "text inputs"),
        "dropdown": ("dropdown", "dropdowns"),
        "date_picker": ("date picker", "date pickers"),
        "file_upload": ("file upload", "file uploads"),
        "radio": ("radio group", "radio groups"),
        "checkbox": ("checkbox", "checkboxes"),
        "button": ("button", "buttons"),
        "link": ("link", "links"),
        "nav_tab": ("nav tab", "nav tabs"),
        "combobox": ("combobox", "comboboxes"),
        "other": ("other", "other"),
    }
    type_breakdown_parts = []
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        singular, plural_form = plural.get(t, (t, t + "s"))
        type_breakdown_parts.append(f"{c} {singular if c == 1 else plural_form}")
    type_summary = ", ".join(type_breakdown_parts) or "none"

    lines: list[str] = ["\n\n## ✓ Extraction complete"]

    if scanned_screens:
        unique_scanned = list(dict.fromkeys(scanned_screens))
        label = "Just extracted" if len(unique_scanned) == 1 else "Screens scanned"
        lines.append(f"**{label}:** {', '.join(unique_scanned)}")

    for cline in completeness_lines[-5:]:
        lines.append(f"- {cline}")

    lines.append("")
    lines.append(f"### Knowledge base: {kb.app.app_name} ({kb.app.platform.value})")
    lines.append(
        f"- **Screens in KB:** {len(kb.screens)} "
        f"({', '.join(s.screen_name for s in kb.screens)})"
    )
    lines.append(f"- **Total elements:** {total_elements} — {type_summary}")
    lines.append(
        f"- **Saved to:** `artifacts/knowledge/{kb.app.platform.value}/`"
    )
    lines.append("")
    lines.append("_Full extraction log above._")
    return "\n".join(lines)


def _summarize_run(report_text: str, user_request: str) -> str:
    """Use an LLM to write a brief, useful summary of a test run for the chat."""
    try:
        from openai import OpenAI
        client = OpenAI()
        excerpt = report_text[:8000]
        prompt = (
            f"The user asked: \"{user_request}\"\n\n"
            f"Here is the QA test execution report:\n\n{excerpt}\n\n"
            f"Write a clear summary for the user in 2-3 short paragraphs separated by blank lines. "
            f"Paragraph 1: what was tested and overall outcome. "
            f"Paragraph 2: specific fields that passed or failed, with values used and error symptoms. "
            f"Paragraph 3 (optional): notable bugs or app issues worth flagging. "
            f"Be specific with field names. Conversational tone, no headers, no bullet lists. "
            f"Use blank lines between paragraphs for readability."
        )
        resp = client.chat.completions.create(
            model="gpt-5.1",
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"_(summary unavailable: {e})_"


def _handle_chat_message(user_input: str, ctx: dict) -> str:
    """Parse user intent and execute the appropriate pipeline."""
    import asyncio
    from qa.chat.intent import parse_intent, ChatAction
    from qa.knowledge.store import KnowledgeStore

    # Load current KB
    store = KnowledgeStore()
    kb = store.load_by_name(ctx["target"], ctx["platform"]) or store.load_by_name(ctx["app_name"], ctx["platform"])

    # Parse intent
    intent = asyncio.run(parse_intent(
        user_message=user_input,
        knowledge=kb,
        chat_history=st.session_state.chat_messages[-6:],
        model=ctx["model"],
    ))

    # Handle each action
    if intent.action == ChatAction.CLARIFY:
        return f"{intent.response_text}\n\n**{intent.clarification_question}**"

    if intent.action == ChatAction.LIST_SCREENS:
        if not kb or not kb.screens:
            return "I don't have any knowledge about this app yet. Try asking me to *explore the app*."
        lines = [f"## Known Screens for {kb.app.app_name}\n"]
        for s in kb.screens:
            testable = [el for el in s.l0 if el.type.value not in ("nav_tab", "other") and "label" not in el.name.lower()]
            lines.append(f"### {s.screen_name}")
            for el in testable:
                opts = f" (options: {', '.join(el.options[:3])}{'...' if len(el.options) > 3 else ''})" if el.options else ""
                lines.append(f"- **{el.name}** — {el.type.value}{opts}")
            lines.append("")
        return "\n".join(lines)

    if intent.action == ChatAction.ANSWER:
        return intent.response_text or "I'm here to help test your app. What would you like me to test?"

    # EXPLORE or EXECUTE — build the CLI command.
    # Web routes to Path B orchestrators (run_execute_flow / form_extract)
    # which give cleaner results than the single-LLM Path A in qa.cli. Mobile
    # continues on qa.cli since the mobile agent is end-to-end reliable there.
    # Note: chat flow omits --wait because the subprocess has no stdin wired
    # from Streamlit. Use URLs that land directly on the target page, or use
    # the terminal CLI for multi-page wizards that need manual navigation.
    is_web = ctx.get("platform") == "web"

    if is_web and intent.action == ChatAction.EXECUTE:
        cmd_parts = [
            "python", "-m", "qa.orchestrators.run_execute_flow",
            ctx["target"],
            "--app-name", f'"{ctx["app_name"]}"',
            "--model", ctx["model"],
            "--budget", "1.5",
        ]
        if intent.screens:
            cmd_parts.extend(["--screens", f'"{",".join(intent.screens)}"'])
        if intent.element_filter:
            cmd_parts.extend(["--filter", f'"{intent.element_filter}"'])
    elif is_web and intent.action == ChatAction.EXPLORE:
        cmd_parts = [
            "python", "-m", "qa.orchestrators.form_extract",
            ctx["target"],
            "--app-name", f'"{ctx["app_name"]}"',
        ]
        # Pass the user's requested screen name through to form_extract so it
        # updates the existing KB entry for that screen instead of falling
        # back to deriving one from document.title (which can be empty or
        # generic, producing a duplicate "Extracted Form" screen in the KB).
        # form_extract's --screen-name arg takes the first match.
        if intent.screens:
            cmd_parts.extend(["--screen-name", f'"{intent.screens[0]}"'])
    else:
        # Mobile (and anything else): existing qa.cli path
        cmd_parts = ["python", "-m", "qa.cli"]
        if intent.action == ChatAction.EXPLORE:
            cmd_parts.append("explore")
        else:
            cmd_parts.extend(["execute", "--auto-explore"])
        cmd_parts.extend([
            ctx["target"],
            "-p", ctx["platform"],
            "-a", f'"{ctx["app_name"]}"',
            "-m", ctx["model"],
        ])
        if ctx["device"]:
            cmd_parts.extend(["-d", ctx["device"]])
        if intent.screens:
            cmd_parts.extend(["-s", ",".join(intent.screens)])
        if intent.element_filter:
            cmd_parts.extend(["-f", f'"{intent.element_filter}"'])
        if getattr(intent, "test_values", []):
            cmd_parts.extend(["--values", f'"{",".join(intent.test_values)}"'])
        if getattr(intent, "use_different_values", False):
            cmd_parts.append("--avoid-recent")

    cmd = " ".join(cmd_parts)

    import threading
    from queue import Queue, Empty

    # Run the command — stream output and (for mobile) live phone screenshot side-by-side
    is_mobile = ctx.get("platform") == "mobile"
    if is_mobile:
        col_term, col_screen = st.columns([2, 1])
        with col_term:
            output_box = st.empty()
        with col_screen:
            screenshot_box = st.empty()
            initial_img = _take_adb_screenshot()
            if initial_img:
                screenshot_box.image(initial_img, width=320)
    else:
        output_box = st.empty()
        screenshot_box = None
        # Web cold-start is 3-5s (npx + Chrome DevTools MCP + Chrome launch).
        # Show a clear placeholder so the pane isn't blank during that gap —
        # the first real output line will replace it.
        if is_web:
            output_box.info(
                "🔄 Launching Chrome — cold start takes a few seconds, "
                "then the agent begins testing. A new browser window will open."
            )

    # Capture the subprocess start time so we can filter result files to
    # only those produced by THIS run — a failed/early-exit subprocess
    # leaves no new file, and we must NOT display the pre-existing latest
    # one (often an unrelated mobile run).
    run_start_time = time.time()
    process = _run_agent_subprocess(cmd, str(ROOT))
    output_lines: list[str] = []
    skip_keywords = ["USAGE SUMMARY", "Real cost", "No-cache cost", "Savings:", "Cost:", "cost:", "Budget:"]

    output_queue: Queue = Queue()

    def _reader():
        try:
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    output_queue.put(line)
            remaining = process.stdout.read()
            if remaining:
                for rem in remaining.splitlines():
                    output_queue.put(rem)
            output_queue.put(None)
        except Exception as exc:
            output_queue.put(f"[reader error: {exc}]")
            output_queue.put(None)

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()

    last_screenshot_time = time.time()
    done = False
    while not done:
        got_new = False
        while True:
            try:
                line = output_queue.get_nowait()
            except Empty:
                break
            if line is None:
                done = True
                break
            stripped = str(line).rstrip()
            if any(kw in stripped for kw in skip_keywords):
                continue
            output_lines.append(stripped)
            got_new = True

        if got_new:
            output_box.code("\n".join(output_lines[-20:]), language="text")

        if screenshot_box is not None:
            now = time.time()
            if now - last_screenshot_time > 0.5:
                img = _take_adb_screenshot()
                if img:
                    screenshot_box.image(img, width=320)
                last_screenshot_time = now

        if process.poll() is not None and output_queue.empty():
            done = True

        time.sleep(0.1)

    if screenshot_box is not None:
        final_img = _take_adb_screenshot()
        if final_img:
            screenshot_box.image(final_img, width=320)

    # Find the latest result file
    result_msg = ""
    if intent.action == ChatAction.EXECUTE:
        results_dir = ROOT / "artifacts" / "results"
        if results_dir.exists():
            # Two result shapes are produced depending on the orchestrator:
            #  • Mobile / legacy Path A → result_*.txt (pipe-table format)
            #  • Web Path B (run_execute_flow) → *_flow_*.json + matching .txt
            # Filter by mtime so we never surface pre-existing files from
            # an unrelated prior run. Prefer JSON when available — its
            # summary counts are authoritative (structured, not parsed).
            # rglob to pick up date-bucketed subfolders (artifacts/results/YYYY-MM-DD/*).
            fresh_json = [
                p for p in results_dir.rglob("*_flow_*.json")
                if p.stat().st_mtime >= run_start_time
            ]
            fresh_txt = [
                p for p in results_dir.rglob("result_*.txt")
                if p.stat().st_mtime >= run_start_time
            ]
            latest_json = max(fresh_json, key=lambda p: p.stat().st_mtime, default=None)
            latest_txt = max(fresh_txt, key=lambda p: p.stat().st_mtime, default=None)

            if latest_json:
                # Web Path B — structured results.
                try:
                    data = json.loads(latest_json.read_text())
                    smry = data.get("summary", {}) or {}
                    counts = {
                        "PASS": smry.get("passed", 0),
                        "FAIL": smry.get("failed", 0),
                        "SKIP": smry.get("skipped", 0),
                        "BLOCKED": smry.get("blocked", 0),
                    }
                    # Feed the human-readable TXT sibling (if written) to the
                    # LLM summarizer — better narrative than raw JSON.
                    txt_sibling = latest_json.with_suffix(".txt")
                    narrative_source = (
                        txt_sibling.read_text()
                        if txt_sibling.exists()
                        else json.dumps(data, indent=2)
                    )
                    narrative = _summarize_run(narrative_source, user_input)
                    result_msg = (
                        f"\n\n## ✓ Done\n"
                        f"- **Passed:** {counts['PASS']}  •  "
                        f"**Failed:** {counts['FAIL']}  •  "
                        f"**Blocked:** {counts['BLOCKED']}  •  "
                        f"**Skipped:** {counts['SKIP']}\n\n"
                        f"{narrative}\n\n"
                        f"_Full report saved — check **Past Runs** for details._"
                    )
                except Exception as e:
                    result_msg = (
                        f"\n\n## ⚠ Run complete but summary parse failed\n"
                        f"`{e}` — see {latest_json.name} for raw results."
                    )
            elif latest_txt:
                # Legacy Path A (mobile) — pipe-table TXT.
                from_text = latest_txt.read_text()
                test_df = parse_test_results(from_text)
                if test_df is not None:
                    counts = count_statuses(test_df)
                    summary = _summarize_run(from_text, user_input)
                    result_msg = (
                        f"\n\n## ✓ Done\n"
                        f"- **Passed:** {counts['PASS']}  •  **Failed:** {counts['FAIL']}  •  **Skipped:** {counts['SKIP']}\n\n"
                        f"{summary}\n\n"
                        f"_Full report saved — check **Past Runs** for details._"
                    )
            elif process.returncode not in (0, None):
                # Subprocess exited with error and produced no result file
                # — show the error context instead of a misleading summary.
                result_msg = (
                    f"\n\n## ⚠ Run did not complete\n"
                    f"The agent subprocess exited (code {process.returncode}) "
                    f"before producing a result file. Check the output above "
                    f"for the specific error (common causes: missing KB, "
                    f"unreachable URL, Chrome launch failure)."
                )
    elif intent.action == ChatAction.EXPLORE:
        # Post-extract summary — reads the updated KB and the subprocess
        # stdout to produce a concise report of what was captured.
        if process.returncode in (0, None):
            result_msg = _build_explore_report(ctx, output_lines)
        else:
            result_msg = (
                f"\n\n## ⚠ Extraction did not complete\n"
                f"The agent subprocess exited (code {process.returncode}) "
                f"before a knowledge base could be saved. Check the output "
                f"above for the specific error (common causes: Chrome "
                f"launch failure, unreachable URL, MCP server disconnect)."
            )

    return (
        f"{intent.response_text}\n\n"
        f"```bash\n{cmd}\n```\n"
        f"{result_msg}"
    )


# ── Router ───────────────────────────────────────────────────────────────────

page = st.session_state.page
match page:
    case "home": page_home()
    case "new_run": page_new_run()
    case "past_runs": page_past_runs()
    case "knowledge": page_knowledge()
    case "chat": page_chat()
    case _: page_home()
