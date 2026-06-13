"""
dashboard.py — Interactive benchmark dashboard for BCF GraphQL vs REST results.

Run:
  uv run streamlit run dashboard.py

Then open http://localhost:8501 in your browser.
Requires benchmark_results_raw.csv produced by benchmark.py.
"""

import csv
import statistics
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import streamlit as st
from scipy.stats import bootstrap as scipy_bootstrap

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="BCF Benchmark — GraphQL vs REST",
    page_icon="📊",
    layout="wide",
)

TOPIC_TIER_ORDER = ["benchmark_small", "benchmark_medium", "benchmark_large"]
IFC_TIER_ORDER   = ["benchmark_ifc_s1", "benchmark_ifc_s3", "benchmark_ifc_s5"]

TIER_LABELS = {
    "benchmark_small":   "Small (25 topics)",
    "benchmark_medium":  "Medium (100 topics)",
    "benchmark_large":   "Large (500 topics)",
    "benchmark_ifc_s1":  "1 element/viewpoint",
    "benchmark_ifc_s3":  "3 elements/viewpoint",
    "benchmark_ifc_s5":  "5 elements/viewpoint",
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

# Flat = both APIs make 1 request (control / baseline scenarios)
# N+1  = REST must chain N additional requests per topic
FLAT_SCENARIOS = ["topics_flat", "topic_events", "comment_events", "overfetch_partial"]
N1_SCENARIOS   = ["topics_nested", "project_comments", "topic_full"]

SCENARIO_LABELS = {
    "topics_flat":         "Topics (flat)",
    "topics_nested":       "Topics (nested)",
    "topic_events":        "Topic Events",
    "comment_events":      "Comment Events",
    "project_comments":    "Project Comments",
    "topic_full":          "Topic Full",
    "overfetch_partial":   "Over-fetch (2 fields)",
    "ifc_element_scaling": "IFC Element Scaling",
}

API_COLORS = {"graphql": "#2563EB", "rest": "#DC2626"}

SCENARIO_COLORS = {
    "topics_flat":         "#06B6D4",
    "topics_nested":       "#F97316",
    "topic_events":        "#8B5CF6",
    "comment_events":      "#EC4899",
    "project_comments":    "#22C55E",
    "topic_full":          "#EF4444",
    "overfetch_partial":   "#6B7280",
    "ifc_element_scaling": "#A16207",
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


# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("Filters")

# Find all raw CSV files in the current directory
available_csvs = sorted((Path(__file__).parent.parent / "results").glob("benchmark_results_raw*.csv"))

if not available_csvs:
    st.error(
        "**No benchmark_results_raw*.csv files found.**\n\n"
        "Run `uv run python benchmark.py` first to generate results."
    )
    st.stop()

csv_labels = {p: p.name for p in available_csvs}
raw_path = st.sidebar.selectbox(
    "Results file",
    options=available_csvs,
    format_func=lambda p: p.name,
)

raw, summary = load_and_summarise(str(raw_path))

# ── Viewpoint-scaling data (optional) ────────────────────────────────────────
VP_TIER_ORDER       = ["benchmark_vp_v1", "benchmark_vp_v3", "benchmark_vp_v5"]
VP_VIEWPOINT_COUNTS = {"benchmark_vp_v1": 1, "benchmark_vp_v3": 3, "benchmark_vp_v5": 5}

vp_rows = [r for r in summary if r["tier"] in VP_TIER_ORDER and r["scenario"] == "viewpoint_scaling"]

all_topic_tiers     = [t for t in TOPIC_TIER_ORDER    if any(r["tier"]     == t for r in summary)]
all_ifc_tiers       = [t for t in IFC_TIER_ORDER      if any(r["tier"]     == t for r in summary)]
all_topic_scenarios = [s for s in TOPIC_SCENARIO_ORDER if any(r["scenario"] == s for r in summary)]
all_ifc_scenarios   = [s for s in IFC_SCENARIO_ORDER   if any(r["scenario"] == s for r in summary)]

st.sidebar.markdown("**Topic-scaling tiers**")
selected_topic_tiers = st.sidebar.multiselect(
    "Topic tiers",
    options=all_topic_tiers, default=all_topic_tiers,
    format_func=lambda t: TIER_LABELS.get(t, t),
)
st.sidebar.markdown("**Topic-scaling scenarios**")
selected_topic_scenarios = st.sidebar.multiselect(
    "Scenarios",
    options=all_topic_scenarios, default=all_topic_scenarios,
    format_func=lambda s: SCENARIO_LABELS.get(s, s),
)
st.sidebar.divider()
st.sidebar.markdown("**IFC element-scaling tiers**")
selected_ifc_tiers = st.sidebar.multiselect(
    "IFC tiers",
    options=all_ifc_tiers, default=all_ifc_tiers,
    format_func=lambda t: TIER_LABELS.get(t, t),
)

filtered = [
    r for r in summary
    if (r["tier"] in selected_topic_tiers and r["scenario"] in selected_topic_scenarios)
    or (r["tier"] in selected_ifc_tiers   and r["scenario"] in all_ifc_scenarios)
]
filtered_raw = [
    r for r in raw
    if (r["tier"] in selected_topic_tiers and r["scenario"] in selected_topic_scenarios)
    or (r["tier"] in selected_ifc_tiers   and r["scenario"] in all_ifc_scenarios)
]

topic_filtered = [r for r in filtered if r["tier_group"] == "topic"]
ifc_filtered   = [r for r in filtered if r["tier_group"] == "ifc"]


# ── Header ────────────────────────────────────────────────────────────────────

st.title("BCF Benchmark — GraphQL vs REST")
st.markdown(
    "This dashboard compares **GraphQL** and **REST** performance for BCF (BIM Collaboration Format) data. "
    "Each scenario measures median latency, HTTP request count, and payload size across three dataset sizes. "
    "The core thesis claim is that GraphQL eliminates the **N+1 request problem** and avoids **over-fetching** "
    "inherent to REST — both of which compound as dataset size grows."
)

if not filtered:
    st.warning("No data matches the current filters.")
    st.stop()


# ── Dataset & scenario guide ──────────────────────────────────────────────────

with st.expander("Dataset tiers — what changes between tiers?", expanded=False):
    st.markdown("Three independent scaling dimensions were tested. In each group **only one variable changes** — everything else is fixed.")

    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown("**Topic-scaling tiers**")
        st.markdown("Variable: **topic count**")
        st.markdown("Fixed: 1 viewpoint/topic · 3–8 comments/topic · 1–6 IFC elements/viewpoint (random)")
        st.table([
            {"Tier": "Small",  "Topics": 25,  "REST reqs (nested)": "1 + 2×25 = 51"},
            {"Tier": "Medium", "Topics": 100, "REST reqs (nested)": "1 + 2×100 = 201"},
            {"Tier": "Large",  "Topics": 500, "REST reqs (nested)": "1 + 2×500 = 1001"},
        ])

    with c2:
        st.markdown("**IFC element-scaling tiers**")
        st.markdown("Variable: **IFC elements per viewpoint**")
        st.markdown("Fixed: 50 topics · 1 viewpoint/topic · 3 comments/topic")
        st.table([
            {"Tier": "s1", "Elements/viewpoint": 1, "REST reqs": 101},
            {"Tier": "s3", "Elements/viewpoint": 3, "REST reqs": 101},
            {"Tier": "s5", "Elements/viewpoint": 5, "REST reqs": 101},
        ])
        st.caption("Request count stays at 101 — only payload size changes.")

    with c3:
        st.markdown("**Viewpoint-scaling tiers**")
        st.markdown("Variable: **viewpoints per topic**")
        st.markdown("Fixed: 50 topics · 2 IFC elements/viewpoint · 3 comments/topic")
        st.table([
            {"Tier": "v1", "Viewpoints/topic": 1, "REST reqs": "1+50+50×1 = 101"},
            {"Tier": "v3", "Viewpoints/topic": 3, "REST reqs": "1+50+50×3 = 201"},
            {"Tier": "v5", "Viewpoints/topic": 5, "REST reqs": "1+50+50×5 = 301"},
        ])

with st.expander("Scenario guide — what does each scenario measure?", expanded=False):
    st.markdown("""
| Scenario | What it fetches | REST requests | GraphQL requests | Effect measured |
|---|---|---|---|---|
| **Topics (flat)** | All topic fields, no sub-resources | 1 | 1 | Baseline / serialisation overhead |
| **Topics (nested)** | Topics + viewpoints + IFC component GUIDs | 1 + 2N | 1 | N+1 problem (2 extra calls per topic) |
| **Topic Events** | Full audit log of topic field changes | 1 | 1 | Event history query performance |
| **Comment Events** | Full audit log of comment changes | 1 | 1 | Event history query performance |
| **Project Comments** | All topics + all their comments | 1 + N | 1 | N+1 (1 extra call per topic) |
| **Topic Full** | Topics + comments + files + viewpoints + selection | 1 + 3N + N×V | 1 | Worst-case N+1 chain |
| **Over-fetch (2 fields)** | GraphQL: 2 fields · REST: all 20 fields | 1 | 1 | Over-fetching cost |
| **IFC Element Scaling** | Topics + viewpoints + IFC GUIDs (50 topics fixed) | 1 + 2×50 = 101 | 1 | Effect of element density |
""")
    st.caption("N = number of topics. V = viewpoints per topic. GraphQL always uses 1 POST request regardless of query complexity.")

st.divider()


# ── KPI strip ─────────────────────────────────────────────────────────────────

gql_rows  = [r for r in topic_filtered if r["api"] == "graphql"]
rest_rows = [r for r in topic_filtered if r["api"] == "rest"]

if gql_rows and rest_rows:
    gql_latencies  = [r["median_ms"]   for r in gql_rows  if r["median_ms"]   is not None]
    rest_latencies = [r["median_ms"]   for r in rest_rows if r["median_ms"]   is not None]
    gql_reqs       = [r["median_reqs"] for r in gql_rows  if r["median_reqs"] is not None]
    rest_reqs_list = [r["median_reqs"] for r in rest_rows if r["median_reqs"] is not None]

    if gql_latencies and rest_latencies and gql_reqs and rest_reqs_list:
        avg_gql  = sum(gql_latencies)  / len(gql_latencies)
        avg_rest = sum(rest_latencies) / len(rest_latencies)
        speedup  = avg_rest / avg_gql if avg_gql else 0
        max_rest_reqs = max(rest_reqs_list)
        max_gql_reqs  = max(gql_reqs)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Avg GraphQL latency", f"{avg_gql:.1f} ms")
        c2.metric("Avg REST latency",    f"{avg_rest:.1f} ms",
                  delta=f"{avg_rest - avg_gql:+.1f} ms", delta_color="inverse")
        c3.metric("GraphQL speedup",     f"{speedup:.1f}×")
        c4.metric("Max REST requests",   f"{max_rest_reqs:.0f}",
                  delta=f"vs {max_gql_reqs:.0f} GQL", delta_color="inverse")

st.divider()


# ── Tab layout ────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Latency",
    "Speedup",
    "Request Count",
    "Payload Size",
    "IFC Element Scaling",
    "Viewpoint Scaling",
])


# ── Tab 1: Latency — split flat (control) vs N+1 (relational) ─────────────────

def _latency_bar(tier_data, scenarios, title, height=400) -> go.Figure:
    """Grouped bar chart for a subset of scenarios against one tier."""
    scenario_order = [s for s in scenarios if s in selected_topic_scenarios]
    fig = go.Figure()
    for api, color in API_COLORS.items():
        sc_map      = {r["scenario"]: r["median_ms"]  for r in tier_data if r["api"] == api}
        ci_low_map  = {r["scenario"]: r["ci_low_ms"]  for r in tier_data if r["api"] == api}
        ci_high_map = {r["scenario"]: r["ci_high_ms"] for r in tier_data if r["api"] == api}
        ys       = [sc_map.get(s)      or 0 for s in scenario_order]
        ci_lows  = [ci_low_map.get(s)  or 0 for s in scenario_order]
        ci_highs = [ci_high_map.get(s) or 0 for s in scenario_order]
        labels   = [SCENARIO_LABELS.get(s, s) for s in scenario_order]
        fig.add_trace(go.Bar(
            name=api.upper(), x=labels, y=ys,
            marker_color=color, opacity=0.88,
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
                "<extra>" + api.upper() + "</extra>"
            ),
        ))
    # DNF annotations
    for api in ("graphql", "rest"):
        fail_map = {
            r["scenario"]: (r.get("timeout_count") or 0) + (r.get("error_count") or 0)
            for r in tier_data
            if r["api"] == api
            and (r.get("timeout_count") or 0) + (r.get("error_count") or 0) > 0
        }
        med_map = {r["scenario"]: r["median_ms"] for r in tier_data if r["api"] == api}
        for sc_name, n_fail in fail_map.items():
            if sc_name not in scenario_order:
                continue
            fig.add_annotation(
                x=SCENARIO_LABELS.get(sc_name, sc_name),
                y=med_map.get(sc_name) or 0,
                text=f"⚠ {n_fail} DNF",
                showarrow=True, arrowhead=2, ax=0, ay=-30,
                font=dict(color="#B45309", size=11),
            )
    fig.update_layout(
        title=title, barmode="group", yaxis_title="Latency (ms)",
        legend_title="API", plot_bgcolor="white", height=height,
    )
    return fig


with tab1:
    st.subheader("Median Latency — GraphQL vs REST")
    st.markdown(
        "**Blue = GraphQL** (always 1 request regardless of dataset size). "
        "**Red = REST** (1 + N requests for relational scenarios). "
        "Error bars show the 95 % bootstrap CI on the median. "
        "Scenarios are split: left panel shows flat queries where both APIs should be similar "
        "(the control condition); right panel shows relational N+1 queries where GraphQL's "
        "structural advantage becomes visible."
    )

    tier_choice = st.radio(
        "Tier", options=selected_topic_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True, key="latency_tier",
    )
    tier_desc = {
        "benchmark_small":  "25 topics — baseline",
        "benchmark_medium": "100 topics — 4× larger",
        "benchmark_large":  "500 topics — 20× larger, N+1 cost fully visible",
    }
    st.caption(tier_desc.get(tier_choice, ""))

    tier_data = [r for r in topic_filtered if r["tier"] == tier_choice]

    fig_all = _latency_bar(tier_data, FLAT_SCENARIOS + N1_SCENARIOS, "", height=500)
    st.plotly_chart(fig_all, use_container_width=True)
    st.caption(
        "Error bars = 95 % bootstrap CI on the median. Lower is better. "
        "⚠ DNF = run timed out or errored — excluded from median."
    )


# ── Tab 2: Speedup ratio ──────────────────────────────────────────────────────

with tab2:
    st.subheader("GraphQL Speedup over REST")
    st.markdown(
        "Speedup = REST median latency ÷ GraphQL median latency. "
        "**1× = parity** (dashed line). Above 1× means GraphQL is faster by that multiple. "
        "Left chart shows the speedup for every scenario at the selected tier. "
        "Right chart shows how speedup **grows with dataset size** for N+1 scenarios — "
        "this is the core thesis result: GraphQL's advantage compounds as N increases."
    )

    tier_choice_sp = st.radio(
        "Tier (left chart)", options=selected_topic_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True, key="speedup_tier",
    )

    col_sp_bar, col_sp_line = st.columns(2)

    with col_sp_bar:
        st.markdown("##### Speedup per scenario")
        tier_sp_data = [r for r in topic_filtered if r["tier"] == tier_choice_sp]
        gql_map = {r["scenario"]: r["median_ms"] for r in tier_sp_data if r["api"] == "graphql" and r["median_ms"]}
        rest_map = {r["scenario"]: r["median_ms"] for r in tier_sp_data if r["api"] == "rest"    and r["median_ms"]}
        sc_order = [s for s in TOPIC_SCENARIO_ORDER if s in selected_topic_scenarios
                    and s in gql_map and s in rest_map]

        ratios = [rest_map[s] / gql_map[s] for s in sc_order]
        labels = [SCENARIO_LABELS.get(s, s) for s in sc_order]
        colors = [
            "#22C55E" if r >= 2 else "#86EFAC" if r >= 1.2 else "#D1D5DB"
            for r in ratios
        ]

        fig_sp = go.Figure()
        fig_sp.add_trace(go.Bar(
            x=labels, y=ratios,
            marker_color=colors, opacity=0.9,
            hovertemplate="<b>%{x}</b><br>Speedup: %{y:.2f}×<extra></extra>",
        ))
        fig_sp.add_hline(
            y=1.0, line_dash="dash", line_color="#6B7280",
            annotation_text="Parity (1×)", annotation_position="top right",
        )
        fig_sp.update_layout(
            yaxis_title="REST latency / GQL latency", yaxis_rangemode="tozero",
            plot_bgcolor="white", height=420,
            xaxis_tickangle=-30,
        )
        st.plotly_chart(fig_sp, use_container_width=True)
        st.caption(
            "Green bars = GraphQL faster. Grey ≈ parity. "
            "Flat scenarios (Topics flat, Events, Over-fetch) should be near 1×. "
            "N+1 scenarios grow as dataset size increases."
        )

    with col_sp_line:
        st.markdown("##### Speedup trend across tiers (N+1 scenarios)")
        fig_trend = go.Figure()
        tier_labels_ordered = [TIER_LABELS.get(t, t) for t in TOPIC_TIER_ORDER if t in selected_topic_tiers]
        tiers_ordered = [t for t in TOPIC_TIER_ORDER if t in selected_topic_tiers]

        for sc in N1_SCENARIOS:
            if sc not in selected_topic_scenarios:
                continue
            ratios_by_tier = []
            for tier in tiers_ordered:
                gql_row  = next((r for r in topic_filtered if r["tier"] == tier and r["scenario"] == sc and r["api"] == "graphql"), None)
                rest_row = next((r for r in topic_filtered if r["tier"] == tier and r["scenario"] == sc and r["api"] == "rest"),    None)
                if gql_row and rest_row and gql_row["median_ms"] and rest_row["median_ms"]:
                    ratios_by_tier.append(rest_row["median_ms"] / gql_row["median_ms"])
                else:
                    ratios_by_tier.append(None)

            fig_trend.add_trace(go.Scatter(
                x=tier_labels_ordered, y=ratios_by_tier,
                mode="lines+markers",
                name=SCENARIO_LABELS.get(sc, sc),
                line=dict(color=SCENARIO_COLORS.get(sc), width=2.5),
                marker=dict(size=10),
                hovertemplate="<b>" + SCENARIO_LABELS.get(sc, sc) + "</b><br>%{x}: %{y:.2f}×<extra></extra>",
            ))

        fig_trend.add_hline(
            y=1.0, line_dash="dash", line_color="#6B7280",
            annotation_text="Parity (1×)", annotation_position="top right",
        )
        fig_trend.update_layout(
            xaxis_title="Dataset size",
            yaxis_title="REST latency / GQL latency",
            yaxis_rangemode="tozero",
            legend_title="Scenario",
            plot_bgcolor="white", height=420,
        )
        st.plotly_chart(fig_trend, use_container_width=True)
        st.caption(
            "Rising lines confirm that GraphQL's advantage compounds as dataset size grows. "
            "Flat at 1× = parity. Steeply rising = REST gets proportionally slower. "
            "Topic Full grows fastest because REST must chain 1+3N+NV requests."
        )


# ── Tab 3: Request count ──────────────────────────────────────────────────────

with tab3:
    st.subheader("HTTP Request Count — The N+1 Problem")

    st.markdown(
        "GraphQL always uses **1 HTTP request** regardless of how many topics, viewpoints, or comments "
        "are in the dataset. REST must make **1 + N additional calls** for every scenario that requires "
        "sub-resources (viewpoints, comments, files). As topic count grows from 25 → 100 → 500, "
        "REST request count grows proportionally — GraphQL does not."
    )

    tier_choice2 = st.radio(
        "Select tier", options=selected_topic_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True, key="req_tier",
    )

    tier_req_desc = {
        "benchmark_small":  "25 topics → topics_nested REST makes 1 + 2×25 = 51 requests",
        "benchmark_medium": "100 topics → topics_nested REST makes 1 + 2×100 = 201 requests",
        "benchmark_large":  "500 topics → topics_nested REST makes 1 + 2×500 = 1001 requests",
    }
    st.caption(tier_req_desc.get(tier_choice2, ""))

    tier_data2     = [r for r in topic_filtered if r["tier"] == tier_choice2]
    scenario_order = [s for s in TOPIC_SCENARIO_ORDER if s in selected_topic_scenarios]

    fig2 = go.Figure()
    for api, color in API_COLORS.items():
        sc_map = {r["scenario"]: r["median_reqs"] for r in tier_data2 if r["api"] == api}
        ys     = [sc_map.get(s, 0) for s in scenario_order]
        labels = [SCENARIO_LABELS.get(s, s) for s in scenario_order]
        fig2.add_trace(go.Bar(
            name=api.upper(), x=labels, y=ys,
            marker_color=color, opacity=0.88,
            hovertemplate="<b>%{x}</b><br>Requests: %{y:.0f}<extra>" + api.upper() + "</extra>",
        ))

    fig2.update_layout(barmode="group", yaxis_title="Median request count",
                       legend_title="API", plot_bgcolor="white", height=440)
    st.plotly_chart(fig2, use_container_width=True)
    st.caption(
        "Scenarios where REST shows a tall bar and GraphQL shows 1 are the N+1 scenarios. "
        "The bar height equals the number of round-trips the client must wait for — each adds network latency."
    )


# ── Tab 4: Payload size ───────────────────────────────────────────────────────

with tab4:
    st.subheader("Response Payload Size")
    st.markdown(
        "Total bytes transferred across all HTTP requests in a scenario. "
        "For N+1 scenarios REST accumulates bytes from multiple calls — each sub-request adds its own "
        "HTTP headers and JSON envelope overhead on top of the actual data. "
        "The **Over-fetch (2 fields)** scenario is the clearest demonstration: "
        "GraphQL returns only the 2 requested fields while REST always returns all 20, "
        "even though the client discards the other 18."
    )

    tier_choice3   = st.radio(
        "Select tier", options=selected_topic_tiers,
        format_func=lambda t: TIER_LABELS.get(t, t),
        horizontal=True, key="payload_tier",
    )
    tier_data3     = [r for r in topic_filtered if r["tier"] == tier_choice3]
    scenario_order = [s for s in TOPIC_SCENARIO_ORDER if s in selected_topic_scenarios]

    fig3 = go.Figure()
    for api, color in API_COLORS.items():
        sc_map = {r["scenario"]: r["median_bytes"] / 1024 for r in tier_data3
                  if r["api"] == api and r["median_bytes"] is not None}
        ys     = [sc_map.get(s, 0) for s in scenario_order]
        labels = [SCENARIO_LABELS.get(s, s) for s in scenario_order]
        fig3.add_trace(go.Bar(
            name=api.upper(), x=labels, y=ys,
            marker_color=color, opacity=0.88,
            hovertemplate="<b>%{x}</b><br>%{y:.1f} KB<extra>" + api.upper() + "</extra>",
        ))

    fig3.update_layout(barmode="group", yaxis_title="Median payload (KB)",
                       legend_title="API", plot_bgcolor="white", height=440)
    st.plotly_chart(fig3, use_container_width=True)
    st.caption(
        "Payload is measured as raw uncompressed bytes (Accept-Encoding: identity) "
        "so compression cannot hide REST's over-fetching cost."
    )


# ── Tab 5: IFC element scaling ────────────────────────────────────────────────

with tab5:
    st.subheader("IFC Element Scaling — Effect of Element Density")
    st.markdown(
        "Topic count is fixed at **50**. Viewpoints per topic fixed at **1**. "
        "Only the number of IFC elements referenced per viewpoint changes: **1 → 3 → 5**. "
        "This isolates element density as an independent variable. "
        "REST makes the same **101 requests** in all three tiers — request count does not change "
        "because each viewpoint still needs exactly one `/selection` call. "
        "Only the `/selection` response body grows with more elements. "
        "GraphQL returns more data in its single response but latency barely changes."
    )

    if not ifc_filtered:
        st.info("No IFC element-scaling data yet. Run `generate_benchmark_data.py` and `benchmark.py` first.")
    else:
        ifc_summary = [r for r in ifc_filtered if r["scenario"] == "ifc_element_scaling"]

        col_lat, col_pay = st.columns(2)

        with col_lat:
            fig_ifc_lat = go.Figure()
            for api, color in API_COLORS.items():
                api_rows = sorted(
                    [r for r in ifc_summary if r["api"] == api],
                    key=lambda r: IFC_TIER_ORDER.index(r["tier"]) if r["tier"] in IFC_TIER_ORDER else 99,
                )
                xs = [TIER_LABELS.get(r["tier"], r["tier"]) for r in api_rows]
                ys = [r["median_ms"] for r in api_rows]
                fig_ifc_lat.add_trace(go.Scatter(
                    x=xs, y=ys, mode="lines+markers", name=api.upper(),
                    line=dict(color=color, width=2.2), marker=dict(size=9),
                    hovertemplate=f"<b>{api.upper()}</b><br>%{{x}}: %{{y:.1f}} ms<extra></extra>",
                ))
            fig_ifc_lat.update_layout(
                xaxis_title="IFC elements per viewpoint (topic count fixed at 50)",
                yaxis_title="Median latency (ms)",
                title="Latency vs IFC Element Count",
                plot_bgcolor="white", height=380, legend_title="API",
            )
            st.plotly_chart(fig_ifc_lat, use_container_width=True)
            st.caption(
                "REST latency stays nearly flat because the bottleneck is 101 round-trips, "
                "not payload size. GraphQL latency also stays flat — 1 request regardless."
            )

        with col_pay:
            fig_ifc_pay = go.Figure()
            for api, color in API_COLORS.items():
                api_rows = sorted(
                    [r for r in ifc_summary if r["api"] == api],
                    key=lambda r: IFC_TIER_ORDER.index(r["tier"]) if r["tier"] in IFC_TIER_ORDER else 99,
                )
                xs = [TIER_LABELS.get(r["tier"], r["tier"]) for r in api_rows]
                ys = [r["median_bytes"] / 1024 for r in api_rows]
                fig_ifc_pay.add_trace(go.Scatter(
                    x=xs, y=ys, mode="lines+markers", name=api.upper(),
                    line=dict(color=color, width=2.2), marker=dict(size=9),
                    hovertemplate=f"<b>{api.upper()}</b><br>%{{x}}: %{{y:.1f}} KB<extra></extra>",
                ))
            fig_ifc_pay.update_layout(
                xaxis_title="IFC elements per viewpoint (topic count fixed at 50)",
                yaxis_title="Median payload (KB)",
                title="Payload vs IFC Element Count",
                plot_bgcolor="white", height=380, legend_title="API",
            )
            st.plotly_chart(fig_ifc_pay, use_container_width=True)
            st.caption(
                "Both APIs show growing payload — more elements means more data. "
                "REST payload is larger because it also includes repeated HTTP headers and "
                "JSON envelopes from 101 separate responses."
            )


# ── Tab 6: Viewpoint scaling ──────────────────────────────────────────────────

with tab6:
    st.subheader("Viewpoint Scaling — Effect of Viewpoints per Topic")
    st.markdown(
        "Topic count is fixed at **50**. IFC elements per viewpoint fixed at **2**. "
        "Only the number of viewpoints per topic changes: **1 → 3 → 5**. "
        "Each additional viewpoint forces REST to make one more `/viewpoints/{guid}/selection` call "
        "per topic, so request count grows as **1 + 50 + 50 × N_vp**. "
        "GraphQL still uses **1 request** and resolves all viewpoints server-side."
    )

    if not vp_rows:
        st.info(
            "No viewpoint-scaling data found. Generate and run:\n\n"
            "```\nuv run python generate_benchmark_data.py\nuv run python benchmark.py\n```"
        )
    else:
        x_labels = ["1 viewpoint\n(101 REST reqs)", "3 viewpoints\n(201 REST reqs)", "5 viewpoints\n(301 REST reqs)"]

        col_lat, col_req, col_pay = st.columns(3)

        with col_lat:
            fig_vp_lat = go.Figure()
            for api, color in API_COLORS.items():
                api_rows = sorted(
                    [r for r in vp_rows if r["api"] == api],
                    key=lambda r: VP_TIER_ORDER.index(r["tier"]) if r["tier"] in VP_TIER_ORDER else 99,
                )
                ys = [r["median_ms"] for r in api_rows]
                fig_vp_lat.add_trace(go.Scatter(
                    x=x_labels[:len(ys)], y=ys, mode="lines+markers",
                    name=api.upper(), line=dict(color=color, width=2.2), marker=dict(size=9),
                    hovertemplate=f"<b>{api.upper()}</b><br>%{{x}}: %{{y:.1f}} ms<extra></extra>",
                ))
            fig_vp_lat.update_layout(
                xaxis_title="Viewpoints per topic",
                yaxis_title="Median latency (ms)",
                title="Latency vs Viewpoint Count",
                plot_bgcolor="white", height=380, legend_title="API",
            )
            st.plotly_chart(fig_vp_lat, use_container_width=True)
            st.caption("REST latency grows linearly with viewpoints. GraphQL stays flat.")

        with col_req:
            fig_vp_req = go.Figure()
            for api, color in API_COLORS.items():
                api_rows = sorted(
                    [r for r in vp_rows if r["api"] == api],
                    key=lambda r: VP_TIER_ORDER.index(r["tier"]) if r["tier"] in VP_TIER_ORDER else 99,
                )
                ys = [r["median_reqs"] for r in api_rows]
                fig_vp_req.add_trace(go.Scatter(
                    x=x_labels[:len(ys)], y=ys, mode="lines+markers",
                    name=api.upper(), line=dict(color=color, width=2.2), marker=dict(size=9),
                    hovertemplate=f"<b>{api.upper()}</b><br>%{{x}}: %{{y:.0f}} reqs<extra></extra>",
                ))
            fig_vp_req.update_layout(
                xaxis_title="Viewpoints per topic",
                yaxis_title="HTTP request count",
                title="Request Count vs Viewpoint Count",
                plot_bgcolor="white", height=380, legend_title="API",
            )
            st.plotly_chart(fig_vp_req, use_container_width=True)
            st.caption("REST: 1 + 50 + 50×N_vp requests. GraphQL: always 1.")

        with col_pay:
            fig_vp_pay = go.Figure()
            for api, color in API_COLORS.items():
                api_rows = sorted(
                    [r for r in vp_rows if r["api"] == api],
                    key=lambda r: VP_TIER_ORDER.index(r["tier"]) if r["tier"] in VP_TIER_ORDER else 99,
                )
                ys = [r["median_bytes"] / 1024 for r in api_rows]
                fig_vp_pay.add_trace(go.Scatter(
                    x=x_labels[:len(ys)], y=ys, mode="lines+markers",
                    name=api.upper(), line=dict(color=color, width=2.2), marker=dict(size=9),
                    hovertemplate=f"<b>{api.upper()}</b><br>%{{x}}: %{{y:.1f}} KB<extra></extra>",
                ))
            fig_vp_pay.update_layout(
                xaxis_title="Viewpoints per topic",
                yaxis_title="Median payload (KB)",
                title="Payload vs Viewpoint Count",
                plot_bgcolor="white", height=380, legend_title="API",
            )
            st.plotly_chart(fig_vp_pay, use_container_width=True)
            st.caption("Both APIs carry more data as viewpoints grow, but REST also multiplies round-trips.")


# ── Per-run distribution (box plot) ──────────────────────────────────────────

with st.expander("Per-run latency distribution (box plot)"):
    st.caption("Shows the spread of individual runs — useful for checking measurement stability.")
    all_selectable_scenarios = selected_topic_scenarios + all_ifc_scenarios
    sc_choice = st.selectbox(
        "Scenario", options=all_selectable_scenarios,
        format_func=lambda s: SCENARIO_LABELS.get(s, s),
    )
    fig_box = go.Figure()
    all_selected_tiers = selected_topic_tiers + selected_ifc_tiers
    for tier in all_selected_tiers:
        for api, color in API_COLORS.items():
            runs = [r["elapsed_ms"] for r in filtered_raw
                    if r["scenario"] == sc_choice and r["tier"] == tier and r["api"] == api]
            if not runs:
                continue
            fig_box.add_trace(go.Box(
                y=runs,
                name=f"{TIER_LABELS.get(tier, tier)} / {api.upper()}",
                marker_color=color, opacity=0.8,
            ))
    fig_box.update_layout(yaxis_title="Latency (ms)", plot_bgcolor="white", height=400)
    st.plotly_chart(fig_box, use_container_width=True)


# ── Raw data table ────────────────────────────────────────────────────────────

with st.expander("Summary table"):
    display_rows = [
        {
            "Tier":           r["tier_label"],
            "Scenario":       r["scenario_label"],
            "API":            r["api"].upper(),
            "Median (ms)":    r["median_ms"],
            "CI low (ms)":    r["ci_low_ms"],
            "CI high (ms)":   r["ci_high_ms"],
            "Requests":       r["median_reqs"],
            "Bytes (KB)":     round(r["median_bytes"] / 1024, 1) if r["median_bytes"] is not None else None,
            "DNF":            (r.get("timeout_count", 0) or 0) + (r.get("error_count", 0) or 0),
        }
        for r in sorted(filtered, key=lambda r: (
            (TOPIC_TIER_ORDER + IFC_TIER_ORDER).index(r["tier"])
            if r["tier"] in TOPIC_TIER_ORDER + IFC_TIER_ORDER else 99,
            (TOPIC_SCENARIO_ORDER + IFC_SCENARIO_ORDER).index(r["scenario"])
            if r["scenario"] in TOPIC_SCENARIO_ORDER + IFC_SCENARIO_ORDER else 99,
            r["api"],
        ))
    ]
    st.dataframe(display_rows, use_container_width=True)
