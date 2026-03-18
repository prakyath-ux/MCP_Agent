"""
POC Dashboard — View agent run reports and usage analytics.
Run: streamlit run dashboard.py
"""

import re
import os
from pathlib import Path
from datetime import datetime

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# ── Paths ──────────────────────────────────────────────────────────────────────
RUNS_DIR = Path(__file__).parent / "state" / "runs"
USAGE_FILE = RUNS_DIR / "usage_log.xlsx"

# ── Helpers ────────────────────────────────────────────────────────────────────

def list_run_files() -> list[Path]:
    """Return .txt output files sorted newest-first."""
    return sorted(RUNS_DIR.glob("output_*.txt"), reverse=True)


def parse_header(text: str) -> dict:
    """Extract the key=value header block from a run output file."""
    header = {}
    for line in text.splitlines()[:8]:
        if ":" in line and "=" * 5 not in line:
            key, _, val = line.partition(":")
            header[key.strip()] = val.strip()
    return header


def parse_timestamp_from_filename(path: Path) -> str:
    """Extract a human-readable timestamp from filename like output_poc_20260227_172310.txt."""
    m = re.search(r"(\d{8})_(\d{6})", path.name)
    if m:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%d  %H:%M:%S")
    return path.name


def load_usage_log() -> pd.DataFrame | None:
    """Load usage_log.xlsx into a DataFrame."""
    if not USAGE_FILE.exists():
        return None
    df = pd.read_excel(USAGE_FILE, engine="openpyxl")
    return df


# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="QA Agent — POC Dashboard",
    page_icon="🔍",
    layout="wide",
)

st.title("QA Agent — POC Dashboard")

# ── Sidebar: run selector ─────────────────────────────────────────────────────
run_files = list_run_files()

if not run_files:
    st.warning("No run files found in state/runs/")
    st.stop()

st.sidebar.header("Run History")
labels = {f: parse_timestamp_from_filename(f) for f in run_files}
selected_file = st.sidebar.radio(
    "Select a run",
    run_files,
    format_func=lambda f: labels[f],
)

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab_report, tab_usage = st.tabs(["Run Report", "Usage Analytics"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Run Report
# ══════════════════════════════════════════════════════════════════════════════
with tab_report:
    raw = selected_file.read_text()
    header = parse_header(raw)

    # ── Metric cards ──
    cost_str = header.get("Real Cost", header.get("Cost", "—"))
    cache_str = header.get("Cache Hit", "—")
    cols = st.columns(6)
    cols[0].metric("Model", header.get("Model", "—"))
    cols[1].metric("Turns", header.get("Turns", "—"))
    cols[2].metric("Cost", cost_str.split("(")[0].strip())
    cols[3].metric("Cost (INR)", cost_str.split("(")[1].rstrip(")") if "(" in cost_str else "—")
    cols[4].metric("Cache Hit", cache_str)
    cols[5].metric("Duration", header.get("Duration", "—"))

    st.divider()

    # ── Body content ──
    body = raw.split("=" * 60, 1)[-1].strip() if "=" * 60 in raw else raw

    # ── Parse structured sections (## RESULTS, ## XPATHS, ## ISSUES) ──
    def extract_section(text: str, header: str) -> str:
        """Extract content between a ## header and the next ## header or end."""
        pattern = rf"^## {re.escape(header)}\s*\n(.*?)(?=^## |\Z)"
        m = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        return m.group(1).strip() if m else ""

    results_raw = extract_section(body, "RESULTS")
    xpaths_raw = extract_section(body, "XPATHS")
    issues_raw = extract_section(body, "ISSUES")

    # ── Parse RESULTS markdown table ──
    results_rows = []
    if results_raw:
        for line in results_raw.splitlines():
            line = line.strip()
            # Skip table header and separator rows
            if not line or line.startswith("|--") or line.startswith("| Field"):
                continue
            if line.startswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 3:
                    results_rows.append({
                        "Field": cells[0],
                        "Value": cells[1],
                        "Status": cells[2],
                        "Notes": cells[3] if len(cells) > 3 else "",
                    })

    # ── Parse XPATHS section ──
    xpath_map: dict[str, str] = {}
    if xpaths_raw:
        for line in xpaths_raw.splitlines():
            line = line.strip()
            if ":" in line and "//" in line:
                field, _, xpath = line.partition(":")
                xpath_map[field.strip()] = xpath.strip()

    # ── Merge XPaths into results table ──
    if results_rows:
        for row in results_rows:
            field_lower = row["Field"].lower().replace(" ", "")
            for key, xpath in xpath_map.items():
                if key.lower().replace(" ", "") == field_lower or key.lower() in field_lower:
                    row["XPath"] = xpath
                    break
            if "XPath" not in row:
                row["XPath"] = "—"

    # ── Display RESULTS ──
    if results_rows:
        st.subheader("Fields Tested")
        results_df = pd.DataFrame(results_rows)
        results_df.index = range(1, len(results_df) + 1)
        st.table(results_df)

    # ── Display XPATHS ──
    if xpath_map:
        st.subheader("XPaths Extracted")
        xpath_df = pd.DataFrame(
            [{"Field": k, "XPath": v} for k, v in xpath_map.items()]
        )
        xpath_df.index = range(1, len(xpath_df) + 1)
        st.table(xpath_df)

    # ── Display ISSUES ──
    if issues_raw:
        st.subheader("Issues Found")
        st.markdown(issues_raw)

    # ── Agent Reasoning (everything before ## sections) ──
    reasoning = re.split(r"^## RESULTS", body, maxsplit=1, flags=re.MULTILINE)[0].strip()
    if reasoning:
        st.subheader("Agent Reasoning")
        with st.expander("Show full reasoning log", expanded=False):
            st.code(reasoning, language="markdown", line_numbers=True)

    # ── Full raw output (collapsed) ──
    with st.expander("Raw Output", expanded=False):
        st.code(raw, language="markdown", line_numbers=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Usage Analytics
# ══════════════════════════════════════════════════════════════════════════════
with tab_usage:
    df = load_usage_log()
    if df is None:
        st.warning("usage_log.xlsx not found")
        st.stop()

    # ── Color palette ──
    MODEL_COLORS = {
        "gpt-4o-mini": "#636EFA",
        "gpt-5":       "#F4A261",
        "gpt-4o":      "#00CC96",
        "gpt-4.1":     "#AB63FA",
        "gpt-5-mini":  "#2EC4B6",
        "gpt-5-nano":  "#EF553B",
    }
    CHART_HEIGHT = 340

    def common_layout(fig: go.Figure, **overrides) -> go.Figure:
        defaults = dict(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="Inter, system-ui, sans-serif", size=11, color="#555"),
            title_font=dict(size=13, color="#333"),
            margin=dict(l=44, r=16, t=44, b=40),
            height=CHART_HEIGHT,
            xaxis=dict(showgrid=False, tickfont=dict(size=10)),
            yaxis=dict(gridcolor="rgba(128,128,128,0.1)", gridwidth=0.5, tickfont=dict(size=10)),
            hoverlabel=dict(bgcolor="rgba(30,30,30,0.85)", font_size=11, font_color="white"),
            bargap=0.45,
            bargroupgap=0.15,
        )
        defaults.update(overrides)
        fig.update_layout(**defaults)
        return fig

    # Prep shared data
    cdf = df.copy()
    cdf["Timestamp"] = pd.to_datetime(cdf["Timestamp"])
    cdf["Run"] = cdf["Timestamp"].dt.strftime("%m/%d %H:%M")
    cdf["Color"] = cdf["Model"].map(MODEL_COLORS).fillna("#888")

    # ── Summary metrics ──
    total_cost = cdf["Real Cost ($)"].sum()
    total_tokens = cdf["Total Tokens"].sum()
    total_runs = len(cdf)
    avg_duration = cdf["Duration (sec)"].mean()
    avg_cache_hit = cdf["Cache Hit %"].mean()
    total_savings = cdf["Savings ($)"].sum()

    cols = st.columns(6)
    cols[0].metric("Total Runs", total_runs)
    cols[1].metric("Total Cost", f"${total_cost:.4f}")
    cols[2].metric("Total Tokens", f"{total_tokens:,.0f}")
    cols[3].metric("Avg Cache Hit", f"{avg_cache_hit:.1f}%")
    cols[4].metric("Total Savings", f"${total_savings:.4f}")
    cols[5].metric("Avg Duration", f"{avg_duration:.1f}s")

    st.divider()

    # ── Row 1: Cost per Run + Cumulative Cost ──
    c1, c2 = st.columns(2)

    with c1:
        fig = px.bar(
            cdf, x="Run", y="Real Cost ($)", color="Model",
            color_discrete_map=MODEL_COLORS,
            text=cdf["Real Cost ($)"].apply(lambda v: f"${v:.3f}"),
            title="Cost per Run (with Caching)",
        )
        fig.update_traces(
            textposition="outside", textfont_size=9,
            marker_line_width=0, opacity=0.8,
        )
        common_layout(fig, yaxis_title="USD", showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=cdf["Run"], y=cdf["Cumulative Real ($)"],
            mode="lines+markers+text",
            text=cdf["Cumulative Real ($)"].apply(lambda v: f"${v:.2f}"),
            textposition="top center", textfont_size=9,
            line=dict(color="#636EFA", width=2, shape="spline"),
            marker=dict(size=6, color="#636EFA", line=dict(width=1, color="white")),
            fill="tozeroy", fillcolor="rgba(99,110,250,0.06)",
            hovertemplate="<b>%{x}</b><br>Cumulative: $%{y:.4f}<extra></extra>",
        ))
        common_layout(fig, title="Cumulative Cost", yaxis_title="USD")
        st.plotly_chart(fig, use_container_width=True)

    # ── Row 2: Token Usage + Duration ──
    c3, c4 = st.columns(2)

    with c3:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=cdf["Run"], y=cdf["Input Tokens"],
            name="Input", marker_color="#636EFA", opacity=0.75,
            hovertemplate="Input: %{y:,.0f}<extra></extra>",
        ))
        fig.add_trace(go.Bar(
            x=cdf["Run"], y=cdf["Output Tokens"],
            name="Output", marker_color="#F4A261", opacity=0.75,
            hovertemplate="Output: %{y:,.0f}<extra></extra>",
        ))
        common_layout(
            fig, title="Token Usage (Input / Output)", yaxis_title="Tokens",
            barmode="stack", bargap=0.5,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                        font=dict(size=10)),
        )
        st.plotly_chart(fig, use_container_width=True)

    with c4:
        fig = px.bar(
            cdf, x="Run", y="Duration (sec)", color="Model",
            color_discrete_map=MODEL_COLORS,
            text=cdf["Duration (sec)"].apply(lambda v: f"{v:.0f}s"),
            title="Duration per Run",
        )
        fig.update_traces(
            textposition="outside", textfont_size=9,
            marker_line_width=0, opacity=0.8,
        )
        common_layout(fig, yaxis_title="Seconds", showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    # ── Row 3: Model Comparison ──
    st.divider()
    st.subheader("Model Comparison")

    model_stats = cdf.groupby("Model").agg(
        Runs=("Model", "count"),
        Avg_Cost=("Real Cost ($)", "mean"),
        Total_Cost=("Real Cost ($)", "sum"),
        Avg_Cache_Hit=("Cache Hit %", "mean"),
        Avg_Tokens=("Total Tokens", "mean"),
        Total_Tokens=("Total Tokens", "sum"),
        Avg_Duration=("Duration (sec)", "mean"),
        Avg_Turns=("Turns Used", "mean"),
    ).round(4)
    model_stats.columns = [
        "Runs", "Avg Cost ($)", "Total Cost ($)", "Avg Cache Hit %",
        "Avg Tokens", "Total Tokens", "Avg Duration (s)", "Avg Turns",
    ]

    mc1, mc2 = st.columns(2)

    with mc1:
        fig = go.Figure()
        for model in model_stats.index:
            row = model_stats.loc[model]
            fig.add_trace(go.Bar(
                x=["Avg Cost ($)", "Total Cost ($)"],
                y=[row["Avg Cost ($)"], row["Total Cost ($)"]],
                name=model,
                marker_color=MODEL_COLORS.get(model, "#888"),
                opacity=0.8,
                text=[f"${row['Avg Cost ($)']:.4f}", f"${row['Total Cost ($)']:.4f}"],
                textposition="outside", textfont_size=9,
            ))
        common_layout(
            fig, title="Cost by Model", barmode="group", yaxis_title="USD",
            bargap=0.35, bargroupgap=0.1,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                        font=dict(size=10)),
        )
        st.plotly_chart(fig, use_container_width=True)

    with mc2:
        fig = go.Figure()
        for model in model_stats.index:
            row = model_stats.loc[model]
            fig.add_trace(go.Bar(
                x=["Avg Duration (s)", "Avg Turns"],
                y=[row["Avg Duration (s)"], row["Avg Turns"]],
                name=model,
                marker_color=MODEL_COLORS.get(model, "#888"),
                opacity=0.8,
                text=[f"{row['Avg Duration (s)']:.0f}s", f"{row['Avg Turns']:.1f}"],
                textposition="outside", textfont_size=9,
            ))
        common_layout(
            fig, title="Performance by Model", barmode="group",
            bargap=0.35, bargroupgap=0.1,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                        font=dict(size=10)),
        )
        st.plotly_chart(fig, use_container_width=True)

    # Detailed table
    st.table(model_stats)

    # ── Run Log (at bottom) ──
    st.divider()
    st.subheader("Run Log")
    st.dataframe(df, width="stretch", hide_index=True)
