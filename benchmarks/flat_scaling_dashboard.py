"""
flat_scaling_dashboard.py — Compact scaling view for flat-query scenarios.

Shows topics_flat, topic_events, comment_events, and overfetch_partial across
scales 25 → 100 → 500 topics for both Local and Render environments in a 2×2
grid so all four scenarios can be compared at a glance.

Run:
  uv run streamlit run flat_scaling_dashboard.py
"""

import csv
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

st.set_page_config(
    page_title="Flat Queries — Scaling Dashboard",
    page_icon="📈",
    layout="wide",
)

# ── Constants ──────────────────────────────────────────────────────────────────

TIER_SCALE = {
    "benchmark_small":  25,
    "benchmark_medium": 100,
    "benchmark_large":  500,
}

FLAT_SCENARIOS = ["topics_flat", "topic_events", "comment_events", "overfetch_partial"]

SCENARIO_LABELS = {
    "topics_flat":       "Topics (flat)",
    "topic_events":      "Topic Events",
    "comment_events":    "Comment Events",
    "overfetch_partial": "Over-fetch (2 fields)",
}

SCENARIO_DESCRIPTIONS = {
    "topics_flat":       "All topics — identical field set to REST",
    "topic_events":      "BCF 3.0 topic audit log (Section 3.9)",
    "comment_events":    "BCF 3.0 comment audit log (Section 3.10)",
    "overfetch_partial": "2-field GraphQL query vs full REST topic payload",
}

# Environment → display name, line styles per API
ENVS = {
    "local":  {"label": "Local",  "gql_color": "#2563EB", "rest_color": "#DC2626", "dash": "solid"},
    "render": {"label": "Render", "gql_color": "#0891B2", "rest_color": "#EA580C", "dash": "dot"},
}

RESULTS_DIR = Path(__file__).parent.parent / "results"


# ── Data loading ───────────────────────────────────────────────────────────────

@st.cache_data
def load_summary(env_name: str) -> list[dict]:
    path = RESULTS_DIR / f"benchmark_results_summary_{env_name}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["scale"] = TIER_SCALE.get(r["tier"])
        for col in ("median_ms", "ci_low_ms", "ci_high_ms", "median_bytes"):
            try:
                r[col] = float(r[col])
            except (ValueError, TypeError):
                r[col] = None
    return rows


def get_series(summary: list[dict], scenario: str, api: str) -> tuple[list[int], list[float], list[float], list[float]]:
    """Return (scales, medians, ci_lows, ci_highs) sorted by scale for a given scenario+api."""
    rows = [
        r for r in summary
        if r["scenario"] == scenario
        and r["api"] == api
        and r["scale"] is not None
        and r["median_ms"] is not None
        and r["tier"] in TIER_SCALE
    ]
    rows.sort(key=lambda r: r["scale"])
    scales   = [r["scale"]    for r in rows]
    medians  = [r["median_ms"]  for r in rows]
    ci_lows  = [r["ci_low_ms"]  for r in rows]
    ci_highs = [r["ci_high_ms"] for r in rows]
    return scales, medians, ci_lows, ci_highs


def get_bytes_series(summary: list[dict], scenario: str, api: str) -> tuple[list[int], list[float]]:
    """Return (scales, kb_values) sorted by scale for a given scenario+api."""
    rows = [
        r for r in summary
        if r["scenario"] == scenario
        and r["api"] == api
        and r["scale"] is not None
        and r.get("median_bytes") is not None
        and r["tier"] in TIER_SCALE
    ]
    rows.sort(key=lambda r: r["scale"])
    scales = [r["scale"] for r in rows]
    kb     = [float(r["median_bytes"]) / 1024 for r in rows]
    return scales, kb


# ── Load data ──────────────────────────────────────────────────────────────────

available_envs = [name for name in ENVS if (RESULTS_DIR / f"benchmark_results_summary_{name}.csv").exists()]

if not available_envs:
    st.error("No benchmark result files found in `results/`. Run the benchmark first.")
    st.stop()

env_data = {name: load_summary(name) for name in available_envs}


# ── Sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.title("Options")

show_ci = st.sidebar.checkbox("Show 95% CI bands", value=True)
show_gql = st.sidebar.checkbox("Show GraphQL", value=True)
show_rest = st.sidebar.checkbox("Show REST", value=True)
log_y = st.sidebar.checkbox("Log Y-axis", value=False)

selected_envs = st.sidebar.multiselect(
    "Environments",
    options=available_envs,
    default=available_envs,
    format_func=lambda e: ENVS[e]["label"],
)

st.sidebar.divider()
st.sidebar.markdown("**Scale axis**")
st.sidebar.markdown("25 = Small · 100 = Medium · 500 = Large")


# ── Header ─────────────────────────────────────────────────────────────────────

st.title("Flat Queries — Scaling Dashboard")
st.markdown(
    "Median latency for the four **flat / single-request** scenarios across dataset scales. "
    "Each chart shares the same X-axis (topic count: 25 → 100 → 500), making it easy to "
    "compare how each scenario scales and how Local vs Render differ at each size. "
    "Dashed lines = Render · Solid lines = Local. "
    "Blue tones = GraphQL · Red/orange tones = REST."
)

# Colour-legend strip
cols = st.columns(4)
for col, (env, spec) in zip(cols, [(e, ENVS[e]) for e in available_envs]):
    col.markdown(
        f"<span style='color:{spec['gql_color']}'>▬</span> **{spec['label']} GQL**&nbsp;&nbsp;"
        f"<span style='color:{spec['rest_color']}'>▬</span> **{spec['label']} REST**",
        unsafe_allow_html=True,
    )

st.divider()


# ── Tabs ───────────────────────────────────────────────────────────────────────

if not selected_envs:
    st.warning("Select at least one environment in the sidebar.")
    st.stop()

tab_latency, tab_payload = st.tabs(["Latency", "Payload"])


# ── Tab: Latency ──────────────────────────────────────────────────────────────

with tab_latency:
    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=[SCENARIO_LABELS[s] for s in FLAT_SCENARIOS],
        shared_xaxes=False,
        vertical_spacing=0.15,
        horizontal_spacing=0.10,
    )

    positions = {
        "topics_flat":       (1, 1),
        "topic_events":      (1, 2),
        "comment_events":    (2, 1),
        "overfetch_partial": (2, 2),
    }

    legend_added: set[str] = set()

    for scenario in FLAT_SCENARIOS:
        row, col = positions[scenario]
        for env_name in selected_envs:
            summary = env_data[env_name]
            spec = ENVS[env_name]
            for api, color, show in [
                ("graphql", spec["gql_color"], show_gql),
                ("rest",    spec["rest_color"], show_rest),
            ]:
                if not show:
                    continue
                scales, medians, ci_lows, ci_highs = get_series(summary, scenario, api)
                if not scales:
                    continue
                legend_key = f"{env_name}_{api}"
                show_legend = legend_key not in legend_added
                if show_legend:
                    legend_added.add(legend_key)
                api_label = "GraphQL" if api == "graphql" else "REST"
                if show_ci and ci_lows and ci_highs and any(v is not None for v in ci_lows):
                    x_band = scales + scales[::-1]
                    y_band = (
                        [h if h is not None else m for h, m in zip(ci_highs, medians)]
                        + [l if l is not None else m for l, m in zip(ci_lows[::-1], medians[::-1])]
                    )
                    fig.add_trace(
                        go.Scatter(
                            x=x_band, y=y_band, fill="toself",
                            fillcolor=color, opacity=0.12,
                            line=dict(width=0), showlegend=False, hoverinfo="skip",
                        ),
                        row=row, col=col,
                    )
                fig.add_trace(
                    go.Scatter(
                        x=scales, y=medians,
                        mode="lines+markers",
                        name=f"{spec['label']} {api_label}",
                        line=dict(color=color, width=2.2, dash=spec["dash"]),
                        marker=dict(size=7, symbol="circle"),
                        legendgroup=legend_key,
                        showlegend=show_legend,
                        customdata=list(zip(
                            [env_name.upper()] * len(scales),
                            [api_label] * len(scales),
                            ci_lows if ci_lows else [None] * len(scales),
                            ci_highs if ci_highs else [None] * len(scales),
                        )),
                        hovertemplate=(
                            "<b>%{customdata[0]} %{customdata[1]}</b><br>"
                            "Topics: %{x}<br>Median: %{y:.1f} ms<br>"
                            "95% CI: [%{customdata[2]:.1f}, %{customdata[3]:.1f}] ms"
                            "<extra></extra>"
                        ),
                    ),
                    row=row, col=col,
                )

    axis_kw = dict(showgrid=True, gridcolor="#E5E7EB", zeroline=False,
                   tickvals=[25, 100, 500], ticktext=["25", "100", "500"])
    yaxis_kw = dict(showgrid=True, gridcolor="#E5E7EB", zeroline=False,
                    title_text="Latency (ms)", **({"type": "log"} if log_y else {}))
    for i in range(1, 5):
        fig.update_xaxes(title_text="Topic count", **axis_kw, row=(i - 1) // 2 + 1, col=(i - 1) % 2 + 1)
        fig.update_yaxes(**yaxis_kw, row=(i - 1) // 2 + 1, col=(i - 1) % 2 + 1)

    fig.update_layout(
        height=680, plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.03, xanchor="center", x=0.5, font=dict(size=12)),
        margin=dict(t=100, b=60, l=60, r=30), font=dict(size=12),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Numbers at a glance")
    rows_out = []
    for scenario in FLAT_SCENARIOS:
        for env_name in available_envs:
            summary = env_data[env_name]
            for api in ("graphql", "rest"):
                scales, medians, ci_lows, ci_highs = get_series(summary, scenario, api)
                for s, m, lo, hi in zip(scales, medians, ci_lows, ci_highs):
                    rows_out.append({
                        "Scenario":     SCENARIO_LABELS[scenario],
                        "Environment":  ENVS[env_name]["label"],
                        "API":          "GraphQL" if api == "graphql" else "REST",
                        "Topics":       s,
                        "Median (ms)":  round(m, 1) if m is not None else None,
                        "CI low (ms)":  round(lo, 1) if lo is not None else None,
                        "CI high (ms)": round(hi, 1) if hi is not None else None,
                    })
    rows_out.sort(key=lambda r: (
        FLAT_SCENARIOS.index(next(k for k, v in SCENARIO_LABELS.items() if v == r["Scenario"])),
        r["Topics"], r["Environment"], r["API"],
    ))
    st.dataframe(rows_out, use_container_width=True, height=380)
    st.caption(
        "Render data only goes up to 100 topics (medium tier). "
        "Local data covers 25 → 100 → 500. CI = 95% bootstrap CI on the median."
    )


# ── Tab: Payload ───────────────────────────────────────────────────────────────

with tab_payload:
    st.markdown(
        "Uncompressed response bytes per request across scales. "
        "The **over-fetch** panel shows how REST always sends the full topic object "
        "while GraphQL returns only the 2 requested fields — the gap widens with dataset size. "
        "Dashed lines = Render · Solid lines = Local (payload is server-side, so lines overlap if "
        "the same server is used; any difference reflects environment-specific data)."
    )

    # 2×2 grid — KB lines per scenario
    fig_p = make_subplots(
        rows=2, cols=2,
        subplot_titles=[SCENARIO_LABELS[s] for s in FLAT_SCENARIOS],
        shared_xaxes=False,
        vertical_spacing=0.15,
        horizontal_spacing=0.10,
    )

    positions_p = {
        "topics_flat":       (1, 1),
        "topic_events":      (1, 2),
        "comment_events":    (2, 1),
        "overfetch_partial": (2, 2),
    }

    legend_added_p: set[str] = set()

    for scenario in FLAT_SCENARIOS:
        row, col = positions_p[scenario]
        for env_name in selected_envs:
            summary = env_data[env_name]
            spec = ENVS[env_name]
            for api, color, show in [
                ("graphql", spec["gql_color"], show_gql),
                ("rest",    spec["rest_color"], show_rest),
            ]:
                if not show:
                    continue
                scales, kb = get_bytes_series(summary, scenario, api)
                if not scales:
                    continue
                legend_key_p = f"p_{env_name}_{api}"
                show_legend_p = legend_key_p not in legend_added_p
                if show_legend_p:
                    legend_added_p.add(legend_key_p)
                api_label = "GraphQL" if api == "graphql" else "REST"
                fig_p.add_trace(
                    go.Scatter(
                        x=scales, y=kb,
                        mode="lines+markers",
                        name=f"{spec['label']} {api_label}",
                        line=dict(color=color, width=2.2, dash=spec["dash"]),
                        marker=dict(size=7, symbol="circle"),
                        legendgroup=legend_key_p,
                        showlegend=show_legend_p,
                        customdata=[[env_name.upper(), api_label]] * len(scales),
                        hovertemplate=(
                            "<b>%{customdata[0]} %{customdata[1]}</b><br>"
                            "Topics: %{x}<br>Payload: %{y:.1f} KB"
                            "<extra></extra>"
                        ),
                    ),
                    row=row, col=col,
                )

    axis_kw_p = dict(showgrid=True, gridcolor="#E5E7EB", zeroline=False,
                     tickvals=[25, 100, 500], ticktext=["25", "100", "500"])
    for i in range(1, 5):
        fig_p.update_xaxes(title_text="Topic count", **axis_kw_p,
                           row=(i - 1) // 2 + 1, col=(i - 1) % 2 + 1)
        fig_p.update_yaxes(showgrid=True, gridcolor="#E5E7EB", zeroline=False,
                           title_text="Payload (KB)",
                           row=(i - 1) // 2 + 1, col=(i - 1) % 2 + 1)

    fig_p.update_layout(
        height=680, plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.03, xanchor="center", x=0.5, font=dict(size=12)),
        margin=dict(t=100, b=60, l=60, r=30), font=dict(size=12),
    )
    st.plotly_chart(fig_p, use_container_width=True)

    # Over-fetch ratio chart (REST KB / GraphQL KB) across scales
    st.divider()
    st.subheader("Over-fetch ratio — REST payload ÷ GraphQL payload")
    st.markdown(
        "Values above **1×** mean REST is sending more data than GraphQL for the same operation. "
        "The `overfetch_partial` scenario shows the starkest ratio because GraphQL requests only "
        "2 fields while REST always returns the entire topic object."
    )

    fig_r = go.Figure()
    ratio_legend_added: set[str] = set()

    for env_name in selected_envs:
        summary = env_data[env_name]
        spec = ENVS[env_name]
        for scenario in FLAT_SCENARIOS:
            gql_scales, gql_kb = get_bytes_series(summary, scenario, "graphql")
            rst_scales, rst_kb = get_bytes_series(summary, scenario, "rest")
            common = [(s, g, r) for (s, g), (s2, r) in zip(zip(gql_scales, gql_kb), zip(rst_scales, rst_kb))
                      if s == s2 and g and g > 0]
            if not common:
                continue
            sc_r, ratio_vals = zip(*[(s, r / g) for s, g, r in common])
            label = f"{spec['label']} — {SCENARIO_LABELS[scenario]}"
            fig_r.add_trace(go.Scatter(
                x=list(sc_r), y=list(ratio_vals),
                mode="lines+markers",
                name=label,
                line=dict(width=2, dash=spec["dash"]),
                marker=dict(size=6),
                hovertemplate="<b>" + label + "</b><br>Topics: %{x}<br>Ratio: %{y:.2f}×<extra></extra>",
            ))

    fig_r.add_hline(y=1.0, line_dash="dash", line_color="#6B7280",
                    annotation_text="Equal payload (1×)", annotation_position="top right")
    fig_r.update_layout(
        height=380, plot_bgcolor="white", paper_bgcolor="white",
        xaxis=dict(showgrid=True, gridcolor="#E5E7EB", zeroline=False,
                   tickvals=[25, 100, 500], ticktext=["25", "100", "500"],
                   title_text="Topic count"),
        yaxis=dict(showgrid=True, gridcolor="#E5E7EB", zeroline=False,
                   rangemode="tozero", title_text="REST KB / GraphQL KB"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5, font=dict(size=11)),
        margin=dict(t=80, b=60, l=60, r=30), font=dict(size=12),
    )
    st.plotly_chart(fig_r, use_container_width=True)
    st.caption(
        "Payload size is determined by the server response and does not vary by environment, "
        "so Local and Render lines should overlap. Any divergence reflects different dataset state."
    )

    # Payload table
    st.divider()
    st.subheader("Payload numbers at a glance")
    pay_rows = []
    for scenario in FLAT_SCENARIOS:
        for env_name in available_envs:
            summary = env_data[env_name]
            for api in ("graphql", "rest"):
                scales, kb = get_bytes_series(summary, scenario, api)
                for s, k in zip(scales, kb):
                    pay_rows.append({
                        "Scenario":    SCENARIO_LABELS[scenario],
                        "Environment": ENVS[env_name]["label"],
                        "API":         "GraphQL" if api == "graphql" else "REST",
                        "Topics":      s,
                        "Payload (KB)": round(k, 1),
                    })
    pay_rows.sort(key=lambda r: (
        FLAT_SCENARIOS.index(next(k for k, v in SCENARIO_LABELS.items() if v == r["Scenario"])),
        r["Topics"], r["Environment"], r["API"],
    ))
    st.dataframe(pay_rows, use_container_width=True, height=340)
    st.caption("Payload = uncompressed response bytes. GraphQL transfers only requested fields.")
