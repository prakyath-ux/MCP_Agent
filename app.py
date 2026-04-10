"""
MCP QA Agent — Unified Dashboard
Run: streamlit run app.py
Controls both web (version_2) and mobile (mobile_version) agents.
"""

import os
import json
import subprocess
import threading
import time
import base64
from pathlib import Path

import streamlit as st
import pandas as pd

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
WEB_PASS1 = ROOT / "version_2" / "runs" / "pass1"
WEB_PASS2 = ROOT / "version_2" / "runs" / "pass2"
MOBILE_PASS1 = ROOT / "mobile_version" / "runs" / "pass1"
MOBILE_PASS2 = ROOT / "mobile_version" / "runs" / "pass2"
# QA Suite (new pipeline architecture)
QA_KNOWLEDGE = ROOT / "artifacts" / "knowledge"
QA_RESULTS = ROOT / "artifacts" / "results"
QA_PLANS = ROOT / "artifacts" / "plans"

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MCP QA Agent",
    page_icon="",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Theme CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    /* Global — deep blue/violet theme, full width */
    .block-container { padding: 1.5rem 3rem; max-width: 100%; }
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
    .stApp { background: linear-gradient(180deg, #0f0f1a 0%, #1a1a2e 5%, #f0f0f8 5.1%, #f0f0f8 100%); }

    /* Hide streamlit branding */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; }

    /* Dark header bar — deep violet, full stretch */
    .header-bar {
        background: linear-gradient(135deg, #1e1145 0%, #2d1b69 40%, #4c1d95 100%);
        padding: 1.6rem 2.5rem;
        border-radius: 14px;
        margin-bottom: 2rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        box-shadow: 0 6px 25px rgba(76, 29, 149, 0.35);
    }
    .header-bar h1 {
        color: #ffffff;
        font-size: 1.9rem;
        font-weight: 800;
        margin: 0;
        letter-spacing: -0.5px;
    }
    .header-bar .subtitle {
        color: #c4b5fd;
        font-size: 0.85rem;
        margin: 0;
    }
    .header-badge {
        background: #a78bfa;
        color: #1e1145;
        padding: 4px 14px;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.5px;
    }

    /* Platform cards — violet accent */
    .platform-card {
        background: #ffffff;
        border: 2px solid #e9e5f5;
        border-radius: 16px;
        padding: 2rem;
        text-align: center;
        transition: all 0.25s ease;
        cursor: pointer;
        min-height: 280px;
    }
    .platform-card:hover {
        border-color: #7c3aed;
        box-shadow: 0 8px 30px rgba(124, 58, 237, 0.18);
        transform: translateY(-3px);
    }
    .platform-card .icon {
        font-size: 2.8rem;
        font-weight: 800;
        margin-bottom: 1.2rem;
        display: block;
        color: #4c1d95;
        letter-spacing: 3px;
    }
    .platform-card h3 {
        font-size: 1.5rem;
        font-weight: 700;
        color: #1e1145;
        margin-bottom: 0.5rem;
    }
    .platform-card p {
        color: #6b7280;
        font-size: 0.9rem;
        line-height: 1.5;
    }
    .platform-card .tag {
        display: inline-block;
        background: #ede9fe;
        color: #5b21b6;
        padding: 4px 11px;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 600;
        margin: 2px;
    }

    /* Metric cards — violet tint */
    .metric-row {
        display: flex;
        gap: 1rem;
        margin-bottom: 1.5rem;
    }
    .metric-box {
        flex: 1;
        background: linear-gradient(135deg, #faf5ff 0%, #ede9fe 100%);
        border: 1px solid #ddd6fe;
        border-radius: 14px;
        padding: 1.5rem;
        text-align: center;
    }
    .metric-box .value {
        font-size: 2.2rem;
        font-weight: 800;
        color: #2d1b69;
        line-height: 1.2;
    }
    .metric-box .label {
        font-size: 0.78rem;
        color: #7c3aed;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-top: 4px;
    }

    /* Status pills */
    .status-row {
        display: flex;
        gap: 1rem;
        margin-bottom: 1.5rem;
    }
    .status-pill {
        flex: 1;
        border-radius: 12px;
        padding: 1rem;
        text-align: center;
    }
    .status-pill .count {
        font-size: 2.8rem;
        font-weight: 800;
        line-height: 1.2;
    }
    .status-pill .label {
        font-size: 0.85rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .pill-pass { background: #dcfce7; }
    .pill-pass .count { color: #16a34a; }
    .pill-pass .label { color: #166534; }
    .pill-fail { background: #fee2e2; }
    .pill-fail .count { color: #dc2626; }
    .pill-fail .label { color: #991b1b; }
    .pill-skip { background: #fef9c3; }
    .pill-skip .count { color: #ca8a04; }
    .pill-skip .label { color: #854d0e; }
    .pill-blocked { background: #f1f5f9; }
    .pill-blocked .count { color: #64748b; }
    .pill-blocked .label { color: #475569; }

    /* Section headers — violet accent line */
    .section-header {
        font-size: 1.3rem;
        font-weight: 700;
        color: #2d1b69;
        margin: 2rem 0 1rem 0;
        padding-bottom: 0.6rem;
        border-bottom: 3px solid #7c3aed;
    }

    /* Action cards — violet hover */
    .action-card {
        background: #ffffff;
        border: 2px solid #e9e5f5;
        border-radius: 14px;
        padding: 2rem;
        text-align: center;
        min-height: 180px;
        transition: all 0.25s ease;
    }
    .action-card:hover {
        border-color: #7c3aed;
        box-shadow: 0 6px 20px rgba(124, 58, 237, 0.12);
        transform: translateY(-2px);
    }
    .action-card h3 {
        font-size: 1.2rem;
        font-weight: 700;
        color: #1e1145;
        margin-bottom: 0.5rem;
    }
    .action-card p {
        color: #6b7280;
        font-size: 0.9rem;
    }

    /* Mode info box — violet tint */
    .mode-info {
        background: linear-gradient(135deg, #faf5ff 0%, #ede9fe 100%);
        border: 1px solid #c4b5fd;
        border-radius: 12px;
        padding: 1.2rem;
        margin-top: 1rem;
    }
    .mode-info h4 {
        color: #5b21b6;
        font-weight: 700;
        margin: 0 0 0.3rem 0;
    }
    .mode-info p {
        color: #4c1d95;
        font-size: 0.9rem;
        margin: 0;
    }

    /* Nav breadcrumb — violet links */
    .breadcrumb {
        color: #9ca3af;
        font-size: 0.85rem;
        margin-bottom: 1rem;
    }
    .breadcrumb a {
        color: #7c3aed;
        text-decoration: none;
        font-weight: 600;
    }

    /* Streamlit button overrides — violet */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #5b21b6 0%, #7c3aed 100%);
        border: none;
        color: white;
        font-weight: 700;
        border-radius: 10px;
        padding: 0.6rem 1.5rem;
        transition: all 0.2s ease;
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(135deg, #4c1d95 0%, #6d28d9 100%);
        box-shadow: 0 4px 15px rgba(124, 58, 237, 0.3);
        transform: translateY(-1px);
    }
    .stButton > button {
        border-radius: 10px;
        font-weight: 600;
        border-color: #ddd6fe;
        color: #5b21b6;
    }
    .stButton > button:hover {
        border-color: #7c3aed;
        color: #4c1d95;
    }

    /* Selectbox / input styling */
    .stSelectbox > div > div, .stTextInput > div > div > input {
        border-radius: 10px;
        border-color: #ddd6fe;
    }
    .stSelectbox > div > div:focus-within, .stTextInput > div > div > input:focus {
        border-color: #7c3aed;
        box-shadow: 0 0 0 1px #7c3aed;
    }

    /* Radio buttons */
    .stRadio > div { gap: 0.5rem; }
    .stRadio > div > label > div:first-child {
        border-color: #c4b5fd;
    }

    /* Table styling */
    .stDataFrame { border-radius: 12px; overflow: hidden; border: 1px solid #e9e5f5; }

    /* Expander styling */
    .streamlit-expanderHeader {
        font-weight: 600;
        color: #5b21b6;
    }

    /* Info/warning boxes */
    .stAlert { border-radius: 10px; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ──────────────────────────────────────────────────────────────────

def list_runs(platform: str, pass_type: str) -> list[Path]:
    if platform == "Web":
        base = WEB_PASS1 if pass_type == "Pass 1" else WEB_PASS2
    elif platform == "QA Suite":
        # QA Suite results from artifacts/
        if pass_type == "Pass 1":
            base = QA_KNOWLEDGE
        else:
            base = QA_RESULTS
        if not base.exists():
            return []
        return sorted(base.rglob("*.json" if pass_type == "Pass 1" else "result_*.txt"), reverse=True)
    else:
        base = MOBILE_PASS1 if pass_type == "Pass 1" else MOBILE_PASS2
    if not base.exists():
        return []
    # Search recursively (date subfolders like 2026-04-07/)
    return sorted(base.rglob("output_*.txt"), reverse=True)


def parse_run_header(text: str) -> dict:
    header = {}
    for line in text.splitlines()[:15]:
        if ":" in line and not line.startswith("=") and not line.startswith("#"):
            key, _, val = line.partition(":")
            header[key.strip()] = val.strip()
    return header


def parse_test_results(text: str) -> pd.DataFrame | None:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "| # |" in line and "Field" in line:
            start = i + 2
            break
    if start is None:
        return None
    rows = []
    for line in lines[start:]:
        if not line.strip().startswith("|"):
            break
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) >= 7:
            rows.append({
                "#": cells[0],
                "Field": cells[1],
                "Test Case": cells[2],
                "Input": cells[3],
                "Expected": cells[4],
                "Actual": cells[5],
                "Status": cells[6],
                "Notes": cells[7] if len(cells) > 7 else "",
            })
    return pd.DataFrame(rows) if rows else None


def parse_bugs(text: str) -> pd.DataFrame | None:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "| # |" in line and "Description" in line:
            start = i + 2
            break
    if start is None:
        return None
    rows = []
    for line in lines[start:]:
        if not line.strip().startswith("|"):
            break
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) >= 4:
            rows.append({
                "#": cells[0],
                "Field": cells[1],
                "Description": cells[2],
                "Severity": cells[3],
                "Evidence": cells[4] if len(cells) > 4 else "",
            })
    return pd.DataFrame(rows) if rows else None


def parse_results_table(text: str) -> pd.DataFrame | None:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "| Field" in line and "Value" in line and "Status" in line:
            start = i + 2
            break
    if start is None:
        return None
    rows = []
    for line in lines[start:]:
        if not line.strip().startswith("|"):
            break
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) >= 3:
            rows.append({
                "Field": cells[0],
                "Value": cells[1],
                "Status": cells[2],
                "Notes": cells[3] if len(cells) > 3 else "",
            })
    return pd.DataFrame(rows) if rows else None


def count_statuses(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"PASS": 0, "FAIL": 0, "SKIP": 0, "BLOCKED": 0}
    counts = df["Status"].value_counts().to_dict()
    return {
        "PASS": counts.get("PASS", 0),
        "FAIL": counts.get("FAIL", 0),
        "SKIP": counts.get("SKIP", 0),
        "BLOCKED": counts.get("BLOCKED", 0),
    }


def _split_output_sections(raw: str) -> list[tuple[str, str]]:
    """Split agent output into clean sections for display."""
    import re
    sections = []
    # Split on markdown headers or THOUGHT/ACTION/OBSERVATION markers
    parts = re.split(r'((?:^|\n)(?:##?\s+.+|THOUGHT:|ACTION:|OBSERVATION:|KNOWLEDGE\n|RESULTS\n|ELEMENTS\n|ISSUES\n))', raw)

    current_title = ""
    current_body = ""

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Check if this is a section header
        if part.startswith("## ") or part.startswith("# "):
            # Save previous section
            if current_title or current_body:
                sections.append((current_title, current_body.strip()))
            current_title = part.lstrip("#").strip()
            current_body = ""
        elif part.strip() in ("THOUGHT:", "ACTION:", "OBSERVATION:", "KNOWLEDGE", "RESULTS", "ELEMENTS", "ISSUES"):
            if current_title or current_body:
                sections.append((current_title, current_body.strip()))
            current_title = part.strip().rstrip(":")
            current_body = ""
        else:
            current_body += "\n" + part

    # Don't forget the last section
    if current_title or current_body:
        sections.append((current_title, current_body.strip()))

    # Filter out empty sections and clean up
    cleaned = []
    for title, body in sections:
        if not body and not title:
            continue
        # Skip sections already shown as dataframes or saved for collapsible
        if title in ("TEST CASE RESULTS", "BUGS FOUND", "TEST SUMMARY", "KNOWLEDGE"):
            continue
        cleaned.append((title, body))

    return cleaned


def friendly_run_name(path: Path) -> str:
    name = path.stem
    name = name.replace("output_v2_", "").replace("output_mobile_", "")
    parts = name.split("_")
    if len(parts) >= 3:
        mode = parts[0]
        date = parts[1] if len(parts) > 1 else ""
        time_str = parts[2] if len(parts) > 2 else ""
        if len(date) == 8 and len(time_str) == 6:
            formatted_date = f"{date[6:]}/{date[4:6]}/{date[:4]}"
            formatted_time = f"{time_str[:2]}:{time_str[2:4]}"
            return f"{mode.upper()}  |  {formatted_date}  {formatted_time}"
    return name


def render_header():
    st.markdown("""
    <div class="header-bar">
        <div>
            <h1>MCP QA Agent</h1>
            <p class="subtitle">AI-Powered Regression Testing Platform</p>
        </div>
        <div>
            <span class="header-badge">ACTIVE</span>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ── Session State ────────────────────────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state.page = "home"
if "platform" not in st.session_state:
    st.session_state.platform = None
if "running" not in st.session_state:
    st.session_state.running = False


# ── PAGE: Home ───────────────────────────────────────────────────────────────

def page_home():
    render_header()

    st.markdown("")
    st.markdown("")

    col1, col2, col3 = st.columns(3, gap="large")

    with col1:
        st.markdown("""
        <div class="platform-card">
            <span class="icon">WEB</span>
            <h3>Web Testing</h3>
            <p>Test web applications via Chrome DevTools MCP. Navigate pages, fill forms, inspect DOM, run JavaScript.</p>
            <div style="margin-top: 1rem">
                <span class="tag">Chrome DevTools</span>
                <span class="tag">DOM Inspection</span>
                <span class="tag">CSS Selectors</span>
                <span class="tag">XPath Extraction</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Launch Web Agent", use_container_width=True, type="primary", key="web_btn"):
            st.session_state.platform = "Web"
            st.session_state.page = "action"
            st.rerun()

    with col2:
        st.markdown("""
        <div class="platform-card">
            <span class="icon">MOBILE</span>
            <h3>Mobile Testing</h3>
            <p>Test Android apps on real devices via mobile-mcp + ADB. Tap, swipe, type on native app screens.</p>
            <div style="margin-top: 1rem">
                <span class="tag">Android / ADB</span>
                <span class="tag">Accessibility Tree</span>
                <span class="tag">Coordinate Taps</span>
                <span class="tag">Screen Recording</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Launch Mobile Agent", use_container_width=True, type="primary", key="mobile_btn"):
            st.session_state.platform = "Mobile"
            st.session_state.page = "action"
            st.rerun()

    with col3:
        st.markdown("""
        <div class="platform-card">
            <span class="icon">QA SUITE</span>
            <h3>QA Suite (Pipeline)</h3>
            <p>Unified 3-pipeline architecture: Explore, Plan, Execute. Layered KB, platform-agnostic, multi-screen.</p>
            <div style="margin-top: 1rem">
                <span class="tag">3 Pipelines</span>
                <span class="tag">Layered KB</span>
                <span class="tag">Web + Mobile</span>
                <span class="tag">Auto-Explore</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Launch QA Suite", use_container_width=True, type="primary", key="suite_btn"):
            st.session_state.platform = "QA Suite"
            st.session_state.page = "action"
            st.rerun()


# ── PAGE: Action Selection ──────────────────────────────────────────────────

def page_action():
    render_header()
    platform = st.session_state.platform
    tag = "WEB" if platform == "Web" else "MOBILE"

    st.markdown(f'<div class="breadcrumb"><a href="#">Home</a> / <strong>{platform} Agent</strong></div>', unsafe_allow_html=True)

    if st.button("Back to Home", key="back_home"):
        st.session_state.page = "home"
        st.rerun()

    st.markdown("")

    col1, col2 = st.columns(2, gap="large")

    with col1:
        st.markdown("""
        <div class="action-card">
            <h3>New Run</h3>
            <p>Configure and start a new test run on your application</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Start New Run", use_container_width=True, type="primary", key="new_run_btn"):
            st.session_state.page = "new_run"
            st.rerun()

    with col2:
        st.markdown("""
        <div class="action-card">
            <h3>Past Runs</h3>
            <p>Browse test results, view bugs found, and analyze past performance</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("View Past Runs", use_container_width=True, type="primary", key="past_runs_btn"):
            st.session_state.page = "past_runs"
            st.rerun()


# ── PAGE: New Run ────────────────────────────────────────────────────────────

def _take_adb_screenshot() -> bytes | None:
    """Take a screenshot via ADB and return PNG bytes."""
    try:
        result = subprocess.run(
            ["adb", "exec-out", "screencap", "-p"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0 and len(result.stdout) > 100:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _run_agent_subprocess(cmd: str, cwd: str) -> subprocess.Popen:
    """Launch agent as a subprocess using the venv Python."""
    venv_python = str(ROOT / "venv" / "bin" / "python")
    full_cmd = cmd.replace("python ", f"{venv_python} ", 1)
    return subprocess.Popen(
        full_cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        text=True,
        bufsize=1,
    )


def page_new_run():
    render_header()
    platform = st.session_state.platform

    st.markdown(f'<div class="breadcrumb"><a href="#">Home</a> / <a href="#">{platform}</a> / <strong>New Run</strong></div>', unsafe_allow_html=True)

    if st.button("Back", key="back_action"):
        st.session_state.page = "action"
        st.rerun()

    st.markdown('<div class="section-header">Run Configuration</div>', unsafe_allow_html=True)

    col1, col2 = st.columns([1.2, 1])

    with col1:
        if platform == "Web":
            mode = st.selectbox("Mode", ["safe_test", "recon", "poc_short", "poc", "testcase"])
            url = st.text_input("Target URL", value="https://qa-tq-awp.impactodigifin.xyz/newapplication")
            app_name = st.text_input("Application Name", value="TECU Credit Union")
            cmd = f"python run.py {mode}"
            if url != "https://qa-tq-awp.impactodigifin.xyz/newapplication":
                cmd += f' "{url}" "{app_name}"'
            cwd = str(ROOT / "version_2")
        elif platform == "QA Suite":
            pipeline = st.selectbox("Pipeline", ["explore", "plan", "execute", "full"])
            qa_platform = st.selectbox("Target Platform", ["mobile", "web"], index=0)
            target = st.text_input("Target", value="net.impacto.B2U", help="Package name (mobile) or URL (web)")
            app_name = st.text_input("Application Name", value="Bank App (B2U)")
            screen_name = st.text_input("Screen Name(s)", value="", placeholder="e.g., iTELLER,LOAN,MORE")
            model_choice = st.selectbox("Model", ["gpt-5.1", "gpt-5", "openai/gpt-oss-120b"], index=0)
            device_id = st.text_input("Device ID", value="RZCXA21GV9P") if qa_platform == "mobile" else ""
            cmd = f"python -m qa.cli {pipeline} {target} -p {qa_platform} -a \"{app_name}\" -m {model_choice}"
            if screen_name:
                cmd += f" -s {screen_name}"
            if device_id:
                cmd += f" -d {device_id}"
            if pipeline == "execute":
                cmd += " --auto-explore"
            cwd = str(ROOT)
        else:
            mode = st.selectbox("Mode", ["safe_test", "recon", "poc_short", "poc", "testcase"])
            screen_name = st.text_input("Screen Name(s)", value="", placeholder="e.g., iTeller  or  iTeller,LOAN,MORE")
            model_choice = st.selectbox("Model", ["gpt-5.1", "gpt-5", "openai/gpt-oss-120b"], index=0)
            provider_choice = st.selectbox("Provider", ["openai_chat", "groq", "openrouter"], index=0)
            device_id = st.text_input("Device ID", value="RZCXA21GV9P")
            package_name = st.text_input("Package Name", value="net.impacto.B2U")
            app_name = st.text_input("Application Name", value="Bank App (B2U)")
            cmd = f"python run.py {mode} {screen_name} --model {model_choice} --provider {provider_choice}"
            if device_id != "RZCXA21GV9P":
                cmd += f' --device {device_id} --package "{package_name}" --app "{app_name}"'
            cwd = str(ROOT / "mobile_version")

    with col2:
        if platform == "QA Suite":
            mode_info = {
                "explore": ("Explore / Discovery", "Discover all screens and elements. Builds the knowledge base (L0/L1/L2)."),
                "plan": ("Test Planning", "Generate test cases from knowledge. Pure LLM reasoning, no device needed."),
                "execute": ("Test Execution", "Execute test cases on the app. Auto-explores if no knowledge exists."),
                "full": ("Full Suite", "Run all 3 pipelines: Explore, Plan, Execute in sequence."),
            }
            mode = pipeline
        else:
            mode_info = {
                "safe_test": ("Observe Only", "Lists elements without interacting. Zero risk."),
                "recon": ("Reconnaissance", "Discovers all interactive elements. No data entry."),
                "poc_short": ("Quick POC", "Fills fields with limited turns. Good for validation."),
                "poc": ("Full Exploration", "Complete exploration, form filling, and knowledge extraction. Required before test cases."),
                "testcase": ("Test Cases", "Plans and executes regression test cases. Requires a completed exploration run first."),
            }
        title, desc = mode_info.get(mode, ("Unknown", ""))
        st.markdown(f"""
        <div class="mode-info">
            <h4>{title}</h4>
            <p>{desc}</p>
        </div>
        """, unsafe_allow_html=True)

        if mode == "testcase":
            st.markdown("")
            st.warning("Requires a completed Exploration (poc) run first.")

    st.markdown("---")

    # ── Run button ───────────────────────────────────────────────
    if st.button("Run Test", type="primary", use_container_width=True, key="run_btn"):
        st.session_state.running = True
        st.session_state.run_cmd = cmd
        st.session_state.run_cwd = cwd
        st.session_state.run_platform = platform
        st.rerun()

    # ── Active run with live output ──────────────────────────────
    if st.session_state.get("running"):
        cmd = st.session_state.run_cmd
        cwd = st.session_state.run_cwd
        run_platform = st.session_state.run_platform

        st.markdown('<div class="section-header">Running Agent</div>', unsafe_allow_html=True)

        if run_platform == "Mobile":
            # Two columns: terminal output + live screenshot
            col_terminal, col_screen = st.columns([1.2, 1])
        else:
            col_terminal = st.container()
            col_screen = None

        with col_terminal if run_platform == "Web" else col_terminal:
            status_text = st.empty()
            progress_bar = st.progress(0)
            terminal_output = st.empty()

        if col_screen and run_platform == "Mobile":
            with col_screen:
                st.markdown("**Live Device Screen**")
                screenshot_placeholder = st.empty()
                st.markdown('<style>.live-screen img { max-height: 500px; object-fit: contain; }</style>', unsafe_allow_html=True)
        else:
            screenshot_placeholder = None

        # Launch subprocess
        status_text.markdown("**Starting agent...**")
        process = _run_agent_subprocess(cmd, cwd)

        output_lines = []
        turn_count = 0
        max_turns_est = {"safe_test": 5, "recon": 20, "poc_short": 40, "poc": 150, "testcase": 60}
        est_turns = max_turns_est.get(mode, 50)
        last_screenshot_time = 0

        try:
            while process.poll() is None:
                line = process.stdout.readline()
                if line:
                    stripped = line.strip()
                    # Hide USAGE SUMMARY block (contains cost)
                    if "USAGE SUMMARY" in stripped:
                        st.session_state._hide_usage = True
                    if st.session_state.get("_hide_usage"):
                        # Stop hiding after the closing === line
                        if stripped.startswith("===") and st.session_state.get("_usage_started"):
                            st.session_state._hide_usage = False
                            st.session_state._usage_started = False
                        elif stripped.startswith("==="):
                            st.session_state._usage_started = True
                        # Don't append these lines
                    else:
                        output_lines.append(line.rstrip())

                    # Count turns from output
                    if stripped and stripped[0].isdigit() and len(stripped.split()) > 3:
                        turn_count += 1
                        progress = min(turn_count / est_turns, 0.95)
                        progress_bar.progress(progress)
                        status_text.markdown(f"**Turn {turn_count}** — Agent is working...")

                    # Show last 20 lines of terminal output
                    recent = "\n".join(output_lines[-20:])
                    terminal_output.code(recent, language="text")

                # Mobile screenshot feed (every 3 seconds)
                if screenshot_placeholder and run_platform == "Mobile":
                    now = time.time()
                    if now - last_screenshot_time > 1.5:
                        img_bytes = _take_adb_screenshot()
                        if img_bytes:
                            screenshot_placeholder.image(img_bytes, width=380)
                        last_screenshot_time = now

            # Process finished — read remaining output
            remaining = process.stdout.read()
            if remaining:
                in_usage = st.session_state.get("_hide_usage", False)
                for rem_line in remaining.splitlines():
                    if "USAGE SUMMARY" in rem_line:
                        in_usage = True
                    if in_usage:
                        if rem_line.strip().startswith("===") and "USAGE" not in rem_line:
                            in_usage = False
                        continue
                    output_lines.append(rem_line)
                recent = "\n".join(output_lines[-30:])
                terminal_output.code(recent, language="text")
            # Clean up state
            st.session_state.pop("_hide_usage", None)
            st.session_state.pop("_usage_started", None)

        except Exception as e:
            st.error(f"Error: {e}")

        # Done
        progress_bar.progress(1.0)
        exit_code = process.returncode

        if exit_code == 0:
            status_text.markdown("**Run completed successfully.**")
        else:
            status_text.markdown(f"**Run finished with exit code {exit_code}.**")

        # Take final screenshot for mobile
        if screenshot_placeholder and run_platform == "Mobile":
            img_bytes = _take_adb_screenshot()
            if img_bytes:
                screenshot_placeholder.image(img_bytes, width=380)

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

    else:
        # Not running — show helpful info
        st.markdown("")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("Go to Past Runs", use_container_width=True):
                st.session_state.page = "past_runs"
                st.rerun()


# ── PAGE: Past Runs ──────────────────────────────────────────────────────────

def page_past_runs():
    render_header()
    platform = st.session_state.platform

    st.markdown(f'<div class="breadcrumb"><a href="#">Home</a> / <a href="#">{platform}</a> / <strong>Past Runs</strong></div>', unsafe_allow_html=True)

    col_back, col_pass, col_select = st.columns([1, 3, 5])

    with col_back:
        if st.button("Back", key="back_action2"):
            st.session_state.page = "action"
            st.rerun()

    with col_pass:
        run_type = st.radio(
            "Run Type",
            ["Exploration Runs", "Test Case Runs"],
            horizontal=True,
            label_visibility="collapsed",
            help="Exploration = agent discovers elements. Test Cases = agent tests each element with various inputs."
        )
        pass_type = "Pass 1" if run_type == "Exploration Runs" else "Pass 2"

    runs = list_runs(platform, pass_type)

    if not runs:
        st.markdown("")
        st.info(f"No {run_type.lower()} found for {platform}. Start a new run first.")
        return

    with col_select:
        selected = st.selectbox(
            "Run",
            range(len(runs)),
            format_func=lambda i: friendly_run_name(runs[i]),
            label_visibility="collapsed",
        )

    run_path = runs[selected]
    text = run_path.read_text()
    header = parse_run_header(text)

    # ── Metric cards ─────────────────────────────────────────────
    turns = header.get("Turns", "?")
    turns_val = turns.split("/")[0].strip() if "/" in turns else turns
    cache_val = header.get("Cache Hit", "?")
    dur_val = header.get("Duration", "?")
    model_val = header.get("Model", "gpt-5")

    st.markdown(f"""
    <div class="metric-row">
        <div class="metric-box">
            <div class="value">{turns_val}</div>
            <div class="label">Turns</div>
        </div>
        <div class="metric-box">
            <div class="value">{cache_val}</div>
            <div class="label">Cache Hit</div>
        </div>
        <div class="metric-box">
            <div class="value">{dur_val}</div>
            <div class="label">Duration</div>
        </div>
        <div class="metric-box">
            <div class="value">{model_val}</div>
            <div class="label">Model</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Test Results (Test Case Runs) ────────────────────────────
    test_df = parse_test_results(text)
    if test_df is not None and not test_df.empty:
        statuses = count_statuses(test_df)

        st.markdown(f"""
        <div class="status-row">
            <div class="status-pill pill-pass">
                <div class="count">{statuses['PASS']}</div>
                <div class="label">Passed</div>
            </div>
            <div class="status-pill pill-fail">
                <div class="count">{statuses['FAIL']}</div>
                <div class="label">Failed</div>
            </div>
            <div class="status-pill pill-skip">
                <div class="count">{statuses['SKIP']}</div>
                <div class="label">Skipped</div>
            </div>
            <div class="status-pill pill-blocked">
                <div class="count">{statuses['BLOCKED']}</div>
                <div class="label">Blocked</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown('<div class="section-header">Test Case Results</div>', unsafe_allow_html=True)
        st.dataframe(
            test_df,
            column_config={
                "#": st.column_config.TextColumn("#", width="small"),
                "Status": st.column_config.TextColumn("Status", width="small"),
                "Notes": st.column_config.TextColumn("Notes", width="large"),
            },
            use_container_width=True,
            hide_index=True,
            height=min(len(test_df) * 40 + 40, 500),
        )

    # ── Elements Discovered (Exploration Runs) ───────────────────
    results_df = parse_results_table(text)
    if results_df is not None and not results_df.empty and test_df is None:
        st.markdown('<div class="section-header">Elements Discovered</div>', unsafe_allow_html=True)
        st.dataframe(results_df, use_container_width=True, hide_index=True)

    # ── Bugs Found ───────────────────────────────────────────────
    bugs_df = parse_bugs(text)
    if bugs_df is not None and not bugs_df.empty:
        st.markdown(f'<div class="section-header">Bugs Found  ({len(bugs_df)})</div>', unsafe_allow_html=True)
        st.dataframe(
            bugs_df,
            column_config={
                "#": st.column_config.TextColumn("#", width="small"),
                "Severity": st.column_config.TextColumn("Severity", width="small"),
                "Description": st.column_config.TextColumn("Description", width="large"),
                "Evidence": st.column_config.TextColumn("Evidence", width="large"),
            },
            use_container_width=True,
            hide_index=True,
        )

    # ── Agent Output (main output — cleaned up) ─────────────────
    st.markdown('<div class="section-header">Agent Output</div>', unsafe_allow_html=True)

    output_markers = ["THOUGHT:", "## RESULTS", "## ELEMENTS", "RESULTS"]
    output_start = -1
    for marker in output_markers:
        pos = text.find(marker)
        if pos > 0:
            output_start = pos
            break

    if output_start > 0:
        raw_output = text[output_start:]

        # Split into sections and render each cleanly
        sections = _split_output_sections(raw_output)
        for section_title, section_body in sections:
            if section_title:
                st.markdown(f"**{section_title}**")
            # Render markdown tables and text
            if "|" in section_body and "---" not in section_body[:50]:
                st.markdown(section_body)
            else:
                st.markdown(section_body)
    else:
        st.text(text)

    # ── Turn Log (collapsible, at bottom) ─────────────────────────
    turns_path = run_path.parent / run_path.name.replace("output_", "turns_").replace("output_mobile_", "turns_mobile_").replace(".txt", ".json")
    if turns_path.exists():
        with open(turns_path) as f:
            turns_data = json.load(f)

        turns_list = turns_data.get("turns", [])
        if turns_list:
            with st.expander(f"Turn Log ({len(turns_list)} turns) — Detailed action-by-action breakdown"):
                turn_rows = []
                for t in turns_list:
                    action = t["actions"][0]["tool"] if t.get("actions") else "text_output"
                    action = action.replace("mobile_", "").replace("_on_screen", "")[:25]
                    target = t["actions"][0]["target"][:25] if t.get("actions") and t["actions"][0].get("target") else ""
                    cache_pct = f"{t.get('cache_hit_ratio', 0) * 100:.0f}%"
                    turn_rows.append({
                        "Turn": t["turn"],
                        "Action": action,
                        "Target": target,
                        "Input Tokens": f"{t.get('input_tokens', 0):,}",
                        "Cached": f"{t.get('cached_tokens', 0):,}",
                        "Output Tokens": f"{t.get('output_tokens', 0):,}",
                        "Cache %": cache_pct,
                    })

                turn_df = pd.DataFrame(turn_rows)
                st.dataframe(
                    turn_df,
                    use_container_width=True,
                    hide_index=True,
                    height=min(len(turn_df) * 38 + 40, 500),
                )

    # ── Knowledge JSON (collapsible, developer data) ─────────────
    knowledge_start = text.find("## KNOWLEDGE")
    if knowledge_start == -1:
        knowledge_start = text.find("KNOWLEDGE\n")
    if knowledge_start > 0:
        knowledge_section = text[knowledge_start:]
        # Find the JSON block
        brace_start = knowledge_section.find("{")
        if brace_start > 0:
            depth = 0
            brace_end = -1
            for i in range(brace_start, len(knowledge_section)):
                if knowledge_section[i] == "{":
                    depth += 1
                elif knowledge_section[i] == "}":
                    depth -= 1
                    if depth == 0:
                        brace_end = i + 1
                        break
            if brace_end > 0:
                knowledge_json = knowledge_section[brace_start:brace_end]
                with st.expander("Knowledge Data — Raw element data captured during exploration"):
                    try:
                        formatted = json.dumps(json.loads(knowledge_json), indent=2)
                        st.code(formatted, language="json")
                    except json.JSONDecodeError:
                        st.code(knowledge_json)


# ── Router ───────────────────────────────────────────────────────────────────

page = st.session_state.page
if page == "home":
    page_home()
elif page == "action":
    page_action()
elif page == "new_run":
    page_new_run()
elif page == "past_runs":
    page_past_runs()
