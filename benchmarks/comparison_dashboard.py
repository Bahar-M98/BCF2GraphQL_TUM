"""
comparison_dashboard.py — Side-by-side benchmark comparison between two environments.

Auto-discovers benchmark_results_raw_*.csv files and lets the user pick two environments
(e.g. local vs render) to compare latency, speedup, and environment overhead.

Run:
  uv run streamlit run comparison_dashboard.py
"""

import csv
import statistics
from glob import glob
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import streamlit as st
from scipy.stats import bootstrap as scipy_bootstrap

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Benchmark Comparison — Local vs Render",
    page_icon="⚖️",
    layout="wide",
)

# ── Constants (same as dashboard.py) ─────────────────────────────────────────

TOPIC_TIER_ORDER = ["benchmark_small", "benchmark_medium", "benchmark_large"]
IFC_TIER_ORDER   = ["benchmark_ifc_s1", "benchmark_ifc_s3", "benchmark_ifc_s5"]
VP_TIER_ORDER    = ["benchmark_vp_v1", "benchmark_vp_v3", "benchmark_vp_v5"]

TIER_LABELS = {
    "benchmark_small":   "Small (25 topics)",
    "benchmark_medium":  "Medium (100 topics)",
    "benchmark_large":   "Large (500 topics)",
    "benchmark_ifc_s1":  "1 element/viewpoint",
    "benchmark_ifc_s3":  "3 elements/viewpoint",
    "benchmark_ifc_s5":  "5 elements/viewpoint",
    "benchmark_vp_v1":   "1 viewpoint/topic",
    "benchmark_vp_v3":   "3 viewpoints/topic",
    "benchmark_vp_v5":   "5 viewpoints/topic",
}

SCENARIO_LABELS = {
    "topics_flat":         "Topics (flat)",
    "topics_nested":       "Topics (nested)",
    "topic_events":        "Topic Events",
    "comment_events":      "Comment Events",
    "project_comments":    "Project Comments",
    "topic_full":          "Topic Full",
    "overfetch_partial":   "Over-fetch (2 fields)",
    "ifc_element_scaling": "IFC Element Scaling",
    "viewpoint_scaling":   "Viewpoint Scaling",
}

TOPIC_SCENARIO_ORDER = [
    "topics_flat",
    "topics_nested",
    "topic_events",
    "comment_events",
    "project_comments",
    "topic_full",
    "overfetch_partial",
]
IFC_SCENARIO_ORDER = ["ifc_element_scaling"]

FLAT_SCENARIOS = ["topics_flat", "topic_events", "comment_events", "overfetch_partial"]
N1_SCENARIOS   = ["topics_nested", "project_comments", "topic_full"]

API_COLORS = {"graphql": "#2563EB", "rest": "#DC2626"}

# Colors to distinguish environments A and B
ENV_COLORS = {
    "gql_a":  "#2563EB",  # blue
    "rest_a": "#DC2626",  # red
    "gql_b":  "#0891B2",  # cyan
    "rest_b": "#EA580C",  # orange
}


# ── Data loading & aggregation ────────────────────────────────────────────────

@st.cache_data
def load_and_summarise(path: str) -> tuple[list[dict], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    for row in raw:
        row["timed_out"] = row.get("timed_out", "False") == "True"
        row["errored"]   = row.get("errored",   "False") == "True"
        row["failed"]    = row["timed_out"] or row["errored"]
        for key in ("requests", "bytes", "elapsed_ms"):
            row[key] = float(row[key]) if row[key] not in ("", None) else None
        row["tier_label"]     = TIER_LABELS.get(row["tier"],     row["tier"])
        row["scenario_label"] = SCENARIO_LABELS.get(row["scenario"], row["scenario"])
        row["tier_group"]     = "ifc" if row["tier"] in IFC_TIER_ORDER else "topic"

    groups: dict[tuple, list[dict]] = {}
    for row in raw:
        key = (row["tier"], row["scenario"], row["api"])
        groups.setdefault(key, []).append(row)

    def _ci(latencies):
        if len(latencies) < 2:
            v = round(latencies[0], 2) if latencies else None
            return v, v
        try:
            result = scipy_bootstrap(
                (np.array(latencies),), statistic=np.median,
                n_resamples=2000, confidence_level=0.95,
                method="percentile", random_state=42,
            )
            return round(float(result.confidence_interval.low), 2), round(float(result.confidence_interval.high), 2)
        except Exception:
            m = round(statistics.median(latencies), 2)
            return m, m

    summary = []
    for (tier, scenario, api), rows in groups.items():
        completed  = [r for r in rows if not r["failed"] and r["elapsed_ms"] is not None]
        n_timeouts = sum(1 for r in rows if r["timed_out"])
        n_errors   = sum(1 for r in rows if r["errored"])
        latencies  = sorted(r["elapsed_ms"] for r in completed)
        ci_low, ci_high = _ci(latencies) if latencies else (None, None)
        summary.append({
            "tier":           tier,
            "scenario":       scenario,
            "api":            api,
            "tier_label":     TIER_LABELS.get(tier, tier),
            "scenario_label": SCENARIO_LABELS.get(scenario, scenario),
            "tier_group":     "ifc" if tier in IFC_TIER_ORDER else "topic",
            "median_ms":      round(statistics.median(latencies), 2) if latencies else None,
            "ci_low_ms":      ci_low,
            "ci_high_ms":     ci_high,
            "median_reqs":    statistics.median(r["requests"] for r in completed if r["requests"] is not None) if completed else None,
            "median_bytes":   statistics.median(r["bytes"]    for r in completed if r["bytes"]    is not None) if completed else None,
            "timeout_count":  n_timeouts,
            "error_count":    n_errors,
        })

    return raw, summary


# ── File discovery ─────────────────────────────────────────────────────────────

available_files = sorted((Path(__file__).parent.parent / "results").glob("benchmark_results_raw_*.csv"))
available_labels = []
for p in available_files:
    # Extract label: "benchmark_results_raw_local.csv" → "local"
    stem = p.stem  # e.g. "benchmark_results_raw_local"
    prefix = "benchmark_results_raw_"
    if stem.startswith(prefix):
        available_labels.append(stem[len(prefix):])

label_to_path = {label: path for label, path in zip(available_labels, available_files)}

if len(available_labels) < 2:
    st.error(
        "**Need at least 2 benchmark result files to compare.**\n\n"
        "Generate results for two environments first:\n\n"
        "```\n"
        "# Local environment:\n"
        "uv run python benchmark.py --label local\n\n"
        "# Remote/render environment:\n"
        "uv run python benchmark.py --url https://your-app.onrender.com --label render\n"
        "```\n\n"
        f"Currently found: {[str(p) for p in available_files] or 'none'}"
    )
    st.stop()


# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("Environments")

env_a_label = st.sidebar.selectbox(
    "Environment A",
    options=available_labels,
    index=0,
    help="First environment to compare (e.g. local)",
)

env_b_default = available_labels[1] if len(available_labels) > 1 else available_labels[0]
env_b_label = st.sidebar.selectbox(
    "Environment B",
    options=available_labels,
    index=min(1, len(available_labels) - 1),
    help="Second environment to compare (e.g. render)",
)

if env_a_label == env_b_label:
    st.sidebar.warning("Both environments are the same — select different labels to compare.")

# Load both summaries
_, summary_a = load_and_summarise(str(label_to_path[env_a_label]))
_, summary_b = load_and_summarise(str(label_to_path[env_b_label]))

# Tier selector in sidebar
all_tiers_a = [t for t in TOPIC_TIER_ORDER if any(r["tier"] == t for r in summary_a)]
all_tiers_b = [t for t in TOPIC_TIER_ORDER if any(r["tier"] == t for r in summary_b)]
common_tiers = [t for t in TOPIC_TIER_ORDER if t in all_tiers_a and t in all_tiers_b]

st.sidebar.divider()
st.sidebar.markdown("**Tier filter**")
selected_tiers = st.sidebar.multiselect(
    "Topic tiers",
    options=common_tiers if common_tiers else TOPIC_TIER_ORDER,
    default=common_tiers if common_tiers else TOPIC_TIER_ORDER,
    format_func=lambda t: TIER_LABELS.get(t, t),
)


# ── Header ────────────────────────────────────────────────────────────────────

st.title("Benchmark Comparison — Environment A vs Environment B")
st.markdown(
    f"Comparing **{env_a_label.upper()}** (Environment A) vs **{env_b_label.upper()}** (Environment B). "
    "Use this dashboard to understand the overhead introduced by running remotely (e.g. network latency, "
    "cold-start cost on free-tier instances) versus a local server where all latency is computational."
)

color_legend_cols = st.columns(4)
color_legend_cols[0].markdown(
    f"<span style='color:{ENV_COLORS['gql_a']}'>■</span> **{env_a_label.upper()} — GraphQL**",
    unsafe_allow_html=True,
)
color_legend_cols[1].markdown(
    f"<span style='color:{ENV_COLORS['rest_a']}'>■</span> **{env_a_label.upper()} — REST**",
    unsafe_allow_html=True,
)
color_legend_cols[2].markdown(
    f"<span style='color:{ENV_COLORS['gql_b']}'>■</span> **{env_b_label.upper()} — GraphQL**",
    unsafe_allow_html=True,
)
color_legend_cols[3].markdown(
    f"<span style='color:{ENV_COLORS['rest_b']}'>■</span> **{env_b_label.upper()} — REST**",
    unsafe_allow_html=True,
)

st.divider()


# ── KPI strip ─────────────────────────────────────────────────────────────────

def _avg_latency(summary, api):
    vals = [r["median_ms"] for r in summary if r["api"] == api and r["median_ms"] is not None
            and r["tier"] in TOPIC_TIER_ORDER]
    return sum(vals) / len(vals) if vals else None

avg_gql_a  = _avg_latency(summary_a, "graphql")
avg_rest_a = _avg_latency(summary_a, "rest")
avg_gql_b  = _avg_latency(summary_b, "graphql")
avg_rest_b = _avg_latency(summary_b, "rest")

kpi_cols = st.columns(4)
if avg_gql_a is not None:
    kpi_cols[0].metric(
        f"Avg GQL — {env_a_label.upper()}",
        f"{avg_gql_a:.1f} ms",
    )
if avg_rest_a is not None:
    kpi_cols[1].metric(
        f"Avg REST — {env_a_label.upper()}",
        f"{avg_rest_a:.1f} ms",
    )
if avg_gql_b is not None:
    delta_gql = (avg_gql_b - avg_gql_a) if avg_gql_a is not None else None
    kpi_cols[2].metric(
        f"Avg GQL — {env_b_label.upper()}",
        f"{avg_gql_b:.1f} ms",
        delta=f"{delta_gql:+.1f} ms vs {env_a_label}" if delta_gql is not None else None,
        delta_color="inverse",
    )
if avg_rest_b is not None:
    delta_rest = (avg_rest_b - avg_rest_a) if avg_rest_a is not None else None
    kpi_cols[3].metric(
        f"Avg REST — {env_b_label.upper()}",
        f"{avg_rest_b:.1f} ms",
        delta=f"{delta_rest:+.1f} ms vs {env_a_label}" if delta_rest is not None else None,
        delta_color="inverse",
    )

st.divider()


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Latency",
    "Speedup",
    "Payload",
    "Environment Overhead",
    "Raw Table",
])


# ── Tab 1: Latency — grouped bar chart ────────────────────────────────────────

with tab1:
    st.subheader("Median Latency — Both Environments")
    st.markdown(
        f"4 bars per scenario: "
        f"**{env_a_label.upper()} GraphQL** (blue), "
        f"**{env_a_label.upper()} REST** (red), "
        f"**{env_b_label.upper()} GraphQL** (cyan), "
        f"**{env_b_label.upper()} REST** (orange). "
        "Error bars show 95% bootstrap CI on the median."
    )

    tier_choice = st.radio(
        "Tier",
        options=selected_tiers if selected_tiers else common_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True,
        key="latency_tier_cmp",
    )

    all_scenarios = FLAT_SCENARIOS + N1_SCENARIOS
    all_scenario_labels = [SCENARIO_LABELS.get(s, s) for s in all_scenarios]

    def _get_vals(summary, tier, api, scenarios):
        sc_map      = {r["scenario"]: r["median_ms"]  for r in summary if r["tier"] == tier and r["api"] == api}
        ci_low_map  = {r["scenario"]: r["ci_low_ms"]  for r in summary if r["tier"] == tier and r["api"] == api}
        ci_high_map = {r["scenario"]: r["ci_high_ms"] for r in summary if r["tier"] == tier and r["api"] == api}
        ys       = [sc_map.get(s)      or 0 for s in scenarios]
        ci_lows  = [ci_low_map.get(s)  or 0 for s in scenarios]
        ci_highs = [ci_high_map.get(s) or 0 for s in scenarios]
        return ys, ci_lows, ci_highs

    fig_lat = go.Figure()

    for env_label, summary, color_key_gql, color_key_rest in [
        (env_a_label, summary_a, "gql_a", "rest_a"),
        (env_b_label, summary_b, "gql_b", "rest_b"),
    ]:
        for api, color_key in [("graphql", color_key_gql), ("rest", color_key_rest)]:
            ys, ci_lows, ci_highs = _get_vals(summary, tier_choice, api, all_scenarios)
            fig_lat.add_trace(go.Bar(
                name=f"{env_label.upper()} {api.upper()}",
                x=all_scenario_labels,
                y=ys,
                marker_color=ENV_COLORS[color_key],
                opacity=0.88,
                error_y=dict(
                    type="data",
                    array=[h - m for h, m in zip(ci_highs, ys)],
                    arrayminus=[m - l for l, m in zip(ci_lows, ys)],
                    visible=True, color="#555", thickness=1.5,
                ),
                customdata=list(zip(ci_lows, ci_highs)),
                hovertemplate=(
                    "<b>%{x}</b><br>Median: %{y:.1f} ms<br>"
                    "95% CI: [%{customdata[0]:.1f}, %{customdata[1]:.1f}] ms"
                    f"<extra>{env_label.upper()} {api.upper()}</extra>"
                ),
            ))

    fig_lat.update_layout(
        barmode="group",
        yaxis_title="Latency (ms)",
        legend_title="Environment / API",
        plot_bgcolor="white",
        height=500,
        xaxis_tickangle=-30,
    )
    st.plotly_chart(fig_lat, use_container_width=True)
    st.caption(
        "Error bars = 95% bootstrap CI on the median. Lower is better. "
        "Flat scenarios (left) should show similar ratios across environments. "
        "N+1 scenarios (right) show how REST latency compounds with remote overhead."
    )


# ── Tab 2: Speedup — REST/GQL ratio for both environments ────────────────────

with tab2:
    st.subheader("GraphQL Speedup over REST — Both Environments")
    st.markdown(
        "Speedup = REST median latency ÷ GraphQL median latency. "
        "**1× = parity** (dashed line). "
        "Two bars per scenario — one per environment. "
        "If the speedup is higher in Environment B (remote), it means REST is paying a larger "
        "relative penalty for its extra round-trips over the network."
    )

    tier_choice_sp = st.radio(
        "Tier",
        options=selected_tiers if selected_tiers else common_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True,
        key="speedup_tier_cmp",
    )

    def _speedup_map(summary, tier):
        gql_map  = {r["scenario"]: r["median_ms"] for r in summary if r["tier"] == tier and r["api"] == "graphql" and r["median_ms"]}
        rest_map = {r["scenario"]: r["median_ms"] for r in summary if r["tier"] == tier and r["api"] == "rest"    and r["median_ms"]}
        return {s: rest_map[s] / gql_map[s] for s in gql_map if s in rest_map}

    sp_a = _speedup_map(summary_a, tier_choice_sp)
    sp_b = _speedup_map(summary_b, tier_choice_sp)
    common_sc = [s for s in all_scenarios if s in sp_a and s in sp_b]
    sc_labels  = [SCENARIO_LABELS.get(s, s) for s in common_sc]

    fig_sp = go.Figure()
    for env_label, sp_map, color in [
        (env_a_label, sp_a, ENV_COLORS["gql_a"]),
        (env_b_label, sp_b, ENV_COLORS["gql_b"]),
    ]:
        fig_sp.add_trace(go.Bar(
            name=f"{env_label.upper()} speedup",
            x=sc_labels,
            y=[sp_map.get(s, 0) for s in common_sc],
            marker_color=color,
            opacity=0.88,
            hovertemplate=f"<b>%{{x}}</b><br>Speedup ({env_label}): %{{y:.2f}}×<extra></extra>",
        ))

    fig_sp.add_hline(
        y=1.0, line_dash="dash", line_color="#6B7280",
        annotation_text="Parity (1×)", annotation_position="top right",
    )
    fig_sp.update_layout(
        barmode="group",
        yaxis_title="REST latency / GQL latency",
        yaxis_rangemode="tozero",
        legend_title="Environment",
        plot_bgcolor="white",
        height=460,
        xaxis_tickangle=-30,
    )
    st.plotly_chart(fig_sp, use_container_width=True)
    st.caption(
        "Above 1× = GraphQL is faster. "
        "Flat scenarios should be near 1× in both environments. "
        "N+1 scenarios typically show higher speedup in remote environments because each "
        "additional REST round-trip crosses the network."
    )


# ── Tab 3: Payload ────────────────────────────────────────────────────────────

with tab3:
    st.subheader("Payload Size — GraphQL vs REST")
    st.markdown(
        "Uncompressed response bytes per logical operation. "
        "For flat scenarios this shows **over-fetching**: REST always returns the full "
        "resource object even when the client only needs a few fields. "
        "For N+1 scenarios, REST bytes are accumulated across the entire request chain. "
        "Payload size is determined by data content and does not vary by environment, "
        "so Environment A (local) is used as the reference."
    )

    tier_choice_pay = st.radio(
        "Tier",
        options=selected_tiers if selected_tiers else common_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True,
        key="payload_tier_cmp",
    )

    all_payload_scenarios = FLAT_SCENARIOS + N1_SCENARIOS
    all_payload_labels    = [SCENARIO_LABELS.get(s, s) for s in all_payload_scenarios]

    def _get_bytes(summary, tier, api, scenarios):
        b_map = {r["scenario"]: r["median_bytes"] for r in summary if r["tier"] == tier and r["api"] == api}
        return [(b_map.get(s) or 0) / 1024 for s in scenarios]  # convert to KB

    gql_kb_a  = _get_bytes(summary_a, tier_choice_pay, "graphql", all_payload_scenarios)
    rest_kb_a = _get_bytes(summary_a, tier_choice_pay, "rest",    all_payload_scenarios)
    gql_kb_b  = _get_bytes(summary_b, tier_choice_pay, "graphql", all_payload_scenarios)
    rest_kb_b = _get_bytes(summary_b, tier_choice_pay, "rest",    all_payload_scenarios)

    # ── Grouped bar: bytes per scenario ───────────────────────────────────────
    fig_pay = go.Figure()
    for env_label, gql_kb, rest_kb, ck_gql, ck_rest in [
        (env_a_label, gql_kb_a, rest_kb_a, "gql_a", "rest_a"),
        (env_b_label, gql_kb_b, rest_kb_b, "gql_b", "rest_b"),
    ]:
        fig_pay.add_trace(go.Bar(
            name=f"{env_label.upper()} GraphQL",
            x=all_payload_labels,
            y=gql_kb,
            marker_color=ENV_COLORS[ck_gql],
            opacity=0.88,
            hovertemplate="<b>%{x}</b><br>GraphQL: %{y:.1f} KB"
                          f"<extra>{env_label.upper()}</extra>",
        ))
        fig_pay.add_trace(go.Bar(
            name=f"{env_label.upper()} REST",
            x=all_payload_labels,
            y=rest_kb,
            marker_color=ENV_COLORS[ck_rest],
            opacity=0.88,
            hovertemplate="<b>%{x}</b><br>REST: %{y:.1f} KB"
                          f"<extra>{env_label.upper()}</extra>",
        ))

    fig_pay.update_layout(
        barmode="group",
        yaxis_title="Payload size (KB, uncompressed)",
        legend_title="Environment / API",
        plot_bgcolor="white",
        height=500,
        xaxis_tickangle=-30,
    )
    st.plotly_chart(fig_pay, use_container_width=True)
    st.caption(
        "Bytes accumulated across the full N+1 request chain for REST. "
        "GraphQL transfers only the fields explicitly requested."
    )

    # ── Byte ratio: REST / GQL ────────────────────────────────────────────────
    st.markdown("##### Over-fetching ratio (REST bytes ÷ GraphQL bytes)")
    st.markdown(
        "Values above 1× indicate REST is transferring more data than GraphQL for the same "
        "logical operation. In flat scenarios this shows the field-selection saving; "
        "in N+1 scenarios the accumulated byte count includes sub-resource overhead."
    )

    byte_ratio_scenarios = [
        s for s, gql, rest in zip(all_payload_scenarios, gql_kb_a, rest_kb_a)
        if gql and gql > 0
    ]
    byte_ratios_a = [rest_kb_a[i] / gql_kb_a[i] for i, s in enumerate(all_payload_scenarios)
                     if s in byte_ratio_scenarios and gql_kb_a[i] > 0]
    byte_ratios_b = [rest_kb_b[i] / gql_kb_b[i] for i, s in enumerate(all_payload_scenarios)
                     if s in byte_ratio_scenarios and gql_kb_b[i] > 0
                     if rest_kb_b[i] is not None and gql_kb_b[i] > 0]
    ratio_labels  = [SCENARIO_LABELS.get(s, s) for s in byte_ratio_scenarios]

    fig_ratio = go.Figure()
    fig_ratio.add_trace(go.Bar(
        name=f"{env_a_label.upper()}",
        x=ratio_labels,
        y=byte_ratios_a,
        marker_color=ENV_COLORS["gql_a"],
        opacity=0.88,
        hovertemplate="<b>%{x}</b><br>REST/GQL bytes: %{y:.2f}×"
                      f"<extra>{env_a_label.upper()}</extra>",
    ))
    if byte_ratios_b:
        fig_ratio.add_trace(go.Bar(
            name=f"{env_b_label.upper()}",
            x=ratio_labels,
            y=byte_ratios_b,
            marker_color=ENV_COLORS["gql_b"],
            opacity=0.88,
            hovertemplate="<b>%{x}</b><br>REST/GQL bytes: %{y:.2f}×"
                          f"<extra>{env_b_label.upper()}</extra>",
        ))
    fig_ratio.add_hline(
        y=1.0, line_dash="dash", line_color="#6B7280",
        annotation_text="Equal payload (1×)", annotation_position="top right",
    )
    fig_ratio.update_layout(
        barmode="group",
        yaxis_title="REST bytes / GraphQL bytes",
        yaxis_rangemode="tozero",
        legend_title="Environment",
        plot_bgcolor="white",
        height=420,
        xaxis_tickangle=-30,
    )
    st.plotly_chart(fig_ratio, use_container_width=True)
    st.caption(
        "Above 1× = REST transfers more data than GraphQL. "
        "The over-fetch ratio is constant across tiers for flat scenarios "
        "(determined by the fraction of unused fields) and grows with N for N+1 scenarios."
    )


# ── Tab 4: Environment overhead ────────────────────────────────────────────────

with tab4:
    st.subheader(f"Environment Overhead — {env_b_label.upper()} vs {env_a_label.upper()}")
    st.markdown(
        f"Shows the latency ratio **{env_b_label.upper()} ÷ {env_a_label.upper()}** for GraphQL. "
        "A ratio of 1× means identical performance; above 1× means Environment B is slower. "
        "This isolates the cost of the deployment environment (network, cold-start, container overhead) "
        "independent of the API design differences."
    )

    tier_choice_ov = st.radio(
        "Tier",
        options=selected_tiers if selected_tiers else common_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True,
        key="overhead_tier_cmp",
    )

    gql_a_map = {r["scenario"]: r["median_ms"] for r in summary_a if r["tier"] == tier_choice_ov and r["api"] == "graphql" and r["median_ms"]}
    gql_b_map = {r["scenario"]: r["median_ms"] for r in summary_b if r["tier"] == tier_choice_ov and r["api"] == "graphql" and r["median_ms"]}

    overhead_scenarios = [s for s in all_scenarios if s in gql_a_map and s in gql_b_map and gql_a_map[s]]
    overhead_ratios    = [gql_b_map[s] / gql_a_map[s] for s in overhead_scenarios]
    overhead_labels    = [SCENARIO_LABELS.get(s, s) for s in overhead_scenarios]

    bar_colors = [
        "#EF4444" if r > 3.0 else "#F97316" if r > 2.0 else "#FCD34D" if r > 1.3 else "#86EFAC"
        for r in overhead_ratios
    ]

    fig_ov = go.Figure()
    fig_ov.add_trace(go.Bar(
        x=overhead_labels,
        y=overhead_ratios,
        marker_color=bar_colors,
        opacity=0.9,
        hovertemplate=(
            "<b>%{x}</b><br>"
            f"{env_b_label.upper()} GQL / {env_a_label.upper()} GQL: %{{y:.2f}}×"
            "<extra></extra>"
        ),
    ))
    fig_ov.add_hline(
        y=1.0, line_dash="dash", line_color="#6B7280",
        annotation_text="No overhead (1×)", annotation_position="top right",
    )
    fig_ov.update_layout(
        yaxis_title=f"GQL latency ratio ({env_b_label} / {env_a_label})",
        yaxis_rangemode="tozero",
        plot_bgcolor="white",
        height=460,
        xaxis_tickangle=-30,
    )
    st.plotly_chart(fig_ov, use_container_width=True)
    st.caption(
        "Green = little overhead (< 1.3×). Yellow = moderate (1.3–2×). "
        "Orange = significant (2–3×). Red = high overhead (> 3×). "
        "Flat scenarios reveal the base network overhead; N+1 scenarios may be disproportionately "
        "affected if each round-trip crosses a network boundary."
    )

    # Also show REST overhead side by side with GQL overhead
    rest_a_map = {r["scenario"]: r["median_ms"] for r in summary_a if r["tier"] == tier_choice_ov and r["api"] == "rest" and r["median_ms"]}
    rest_b_map = {r["scenario"]: r["median_ms"] for r in summary_b if r["tier"] == tier_choice_ov and r["api"] == "rest" and r["median_ms"]}

    both_overhead_sc = [s for s in all_scenarios if s in gql_a_map and s in gql_b_map and s in rest_a_map and s in rest_b_map and gql_a_map[s] and rest_a_map[s]]
    if both_overhead_sc:
        st.markdown("##### GQL overhead vs REST overhead (side by side)")
        fig_ov2 = go.Figure()
        fig_ov2.add_trace(go.Bar(
            name="GQL overhead",
            x=[SCENARIO_LABELS.get(s, s) for s in both_overhead_sc],
            y=[gql_b_map[s] / gql_a_map[s] for s in both_overhead_sc],
            marker_color=ENV_COLORS["gql_a"], opacity=0.85,
            hovertemplate="<b>%{x}</b><br>GQL overhead: %{y:.2f}×<extra></extra>",
        ))
        fig_ov2.add_trace(go.Bar(
            name="REST overhead",
            x=[SCENARIO_LABELS.get(s, s) for s in both_overhead_sc],
            y=[rest_b_map[s] / rest_a_map[s] for s in both_overhead_sc],
            marker_color=ENV_COLORS["rest_a"], opacity=0.85,
            hovertemplate="<b>%{x}</b><br>REST overhead: %{y:.2f}×<extra></extra>",
        ))
        fig_ov2.add_hline(y=1.0, line_dash="dash", line_color="#6B7280")
        fig_ov2.update_layout(
            barmode="group",
            yaxis_title=f"Latency ratio ({env_b_label} / {env_a_label})",
            yaxis_rangemode="tozero",
            legend_title="API",
            plot_bgcolor="white",
            height=400,
            xaxis_tickangle=-30,
        )
        st.plotly_chart(fig_ov2, use_container_width=True)
        st.caption(
            "If REST overhead is disproportionately higher than GQL overhead, it shows that "
            "N+1 REST chains are especially expensive when each call crosses the network."
        )


# ── Tab 5: Raw table ──────────────────────────────────────────────────────────

with tab5:
    st.subheader("All Numbers — Both Environments")

    def _build_display_rows(summary, env_label):
        rows = []
        for r in summary:
            if r["tier"] not in (TOPIC_TIER_ORDER + IFC_TIER_ORDER + VP_TIER_ORDER):
                continue
            rows.append({
                "Environment": env_label.upper(),
                "Tier":        r["tier_label"],
                "Scenario":    r["scenario_label"],
                "API":         r["api"].upper(),
                "Median (ms)": r["median_ms"],
                "CI low (ms)": r["ci_low_ms"],
                "CI high (ms)":r["ci_high_ms"],
                "Requests":    r["median_reqs"],
                "Bytes (KB)":  round(r["median_bytes"] / 1024, 1) if r["median_bytes"] is not None else None,
                "DNF":         (r.get("timeout_count", 0) or 0) + (r.get("error_count", 0) or 0),
            })
        return rows

    all_display = _build_display_rows(summary_a, env_a_label) + _build_display_rows(summary_b, env_b_label)
    all_display.sort(key=lambda r: (
        (TOPIC_TIER_ORDER + IFC_TIER_ORDER + VP_TIER_ORDER).index(
            next((k for k, v in TIER_LABELS.items() if v == r["Tier"]), r["Tier"])
        ) if any(v == r["Tier"] for v in TIER_LABELS.values()) else 99,
        r["Scenario"],
        r["API"],
        r["Environment"],
    ))

    st.dataframe(all_display, use_container_width=True)
    st.caption(
        f"All {len(all_display)} rows from both environments. "
        "DNF = timed out or errored runs (excluded from median)."
    )
