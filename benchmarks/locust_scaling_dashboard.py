"""
locust_scaling_dashboard.py — Scaling experiment dashboard.

Reads the CSVs produced by locust_scaling.py and shows how latency,
throughput, and p95 change as concurrent users grow from 1 → 5 → 10.

Run:
  uv run streamlit run locust_scaling_dashboard.py
"""

import csv
import re
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="Locust Scaling — GraphQL vs REST",
    page_icon="📈",
    layout="wide",
)

# ── Constants (shared with locust_dashboard.py) ───────────────────────────────

FLAT_SCENARIOS = ["topics_flat", "topic_events", "comment_events", "overfetch_partial"]
N1_SCENARIOS   = ["topics_nested", "project_comments", "topic_full"]
ALL_SCENARIOS  = FLAT_SCENARIOS + N1_SCENARIOS

SCENARIO_LABELS = {
    "topics_flat":       "Topics (flat)",
    "topics_nested":     "Topics (nested)",
    "topic_events":      "Topic Events",
    "comment_events":    "Comment Events",
    "project_comments":  "Project Comments",
    "topic_full":        "Topic Full",
    "overfetch_partial": "Over-fetch (2 fields)",
}

# Colours per scenario for multi-line charts
SCENARIO_COLORS = {
    "topics_flat":       "#06B6D4",
    "topics_nested":     "#F97316",
    "topic_events":      "#8B5CF6",
    "comment_events":    "#EC4899",
    "project_comments":  "#22C55E",
    "topic_full":        "#EF4444",
    "overfetch_partial": "#6B7280",
}

API_COLORS = {"gql": "#2563EB", "rest": "#DC2626"}

USER_LEVELS = [1, 5, 10]

# Maps directory name → (api, users)
DIR_META = {
    "rest_1user":   ("rest", 1),
    "rest_5users":  ("rest", 5),
    "rest_10users": ("rest", 10),
    "gql_1user":    ("gql",  1),
    "gql_5users":   ("gql",  5),
    "gql_10users":  ("gql",  10),
}


# ── Data loading ──────────────────────────────────────────────────────────────

def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_row(row: dict) -> dict | None:
    """Parse one row from a Locust stats CSV into a structured dict."""
    name = row.get("Name", "").strip()
    if name in ("", "Aggregated"):
        return None

    if name.startswith("gql/"):
        api, scenario = "gql", name[4:]
        is_chain, is_sub, sub_name = False, False, None
    elif name.startswith("rest/"):
        rest = name[5:]
        is_chain = rest.endswith(" [chain]")
        if is_chain:
            api, scenario = "rest", rest[: -len(" [chain]")]
            is_sub, sub_name = False, None
        elif "/" in rest:
            parts = rest.split("/", 1)
            api, scenario, is_sub, sub_name = "rest", parts[0], True, parts[1]
            is_chain = False
        else:
            api, scenario = "rest", rest
            is_chain, is_sub, sub_name = False, False, None
    else:
        return None

    return {
        "name":       name,
        "api":        api,
        "scenario":   scenario,
        "is_chain":   is_chain,
        "is_sub":     is_sub,
        "sub_name":   sub_name,
        "median_ms":  _float(row.get("Median Response Time")),
        "avg_ms":     _float(row.get("Average Response Time")),
        "p95_ms":     _float(row.get("95%")),
        "p99_ms":     _float(row.get("99%")),
        "rps":        _float(row.get("Requests/s")),
        "req_count":  _float(row.get("Request Count")),
        "fail_count": _float(row.get("Failure Count")),
    }


@st.cache_data
def load_all_experiments(results_dir: str) -> list[dict]:
    """
    Walk locust_results/ and load every experiment that has a stats_stats.csv.
    Returns a flat list of rows, each tagged with api and users.
    """
    root = Path(results_dir)
    all_rows = []
    for dir_name, (api, users) in DIR_META.items():
        stats_file = root / dir_name / "stats_stats.csv"
        if not stats_file.exists():
            continue
        with open(stats_file, newline="", encoding="utf-8") as f:
            for raw in csv.DictReader(f):
                parsed = _parse_row(raw)
                if parsed:
                    parsed["exp_api"]   = api
                    parsed["exp_users"] = users
                    all_rows.append(parsed)
    return all_rows


# ── Load data ─────────────────────────────────────────────────────────────────

RESULTS_DIR = str(Path(__file__).parent.parent / "locust_results")

all_rows = load_all_experiments(RESULTS_DIR)

st.title("Locust Scaling — GraphQL vs REST")

if not all_rows:
    st.error(
        "**No experiment results found in `locust_results/`.**\n\n"
        "Run the scaling experiment first:\n\n"
        "```\npython locust_scaling.py --host https://bcf2graphql.onrender.com\n```"
    )
    st.stop()

# Which experiments are actually present?
present_levels = sorted({r["exp_users"] for r in all_rows})
present_apis   = sorted({r["exp_api"]   for r in all_rows})
present_scenarios = [s for s in ALL_SCENARIOS
                     if any(r["scenario"] == s for r in all_rows)]

st.markdown(
    f"Results loaded from **`{RESULTS_DIR}/`**. "
    f"Found data for **{len(present_levels)} concurrency level(s)**: "
    f"{', '.join(str(u) + ' user' + ('s' if u > 1 else '') for u in present_levels)}."
)
st.divider()


# ── Sidebar filters ───────────────────────────────────────────────────────────

st.sidebar.title("Filters")
selected_scenarios = st.sidebar.multiselect(
    "Scenarios", options=present_scenarios, default=present_scenarios,
    format_func=lambda s: SCENARIO_LABELS.get(s, s),
)

if not selected_scenarios:
    st.warning("No scenarios selected.")
    st.stop()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(exp_api: str, exp_users: int, scenario: str) -> dict | None:
    """
    Return the comparable row for a given (api, users, scenario) combination.
    For N+1 REST scenarios uses the [chain] row; for flat REST uses the plain row.
    For GQL always uses the plain gql/ row.
    """
    candidates = [
        r for r in all_rows
        if r["exp_api"] == exp_api
        and r["exp_users"] == exp_users
        and r["scenario"] == scenario
        and not r["is_sub"]
    ]
    if exp_api == "gql":
        return next((r for r in candidates if not r["is_chain"]), None)
    # REST
    if scenario in N1_SCENARIOS:
        return next((r for r in candidates if r["is_chain"]), None)
    return next((r for r in candidates if not r["is_chain"]), None)


# ── KPI strip ─────────────────────────────────────────────────────────────────

# Summarise at the highest concurrency level available
top_level = max(present_levels)
gql_meds  = [r["median_ms"] for s in selected_scenarios
             if (r := _get("gql", top_level, s)) and r["median_ms"]]
rest_meds = [r["median_ms"] for s in selected_scenarios
             if (r := _get("rest", top_level, s)) and r["median_ms"]]

if gql_meds and rest_meds:
    avg_g   = sum(gql_meds)  / len(gql_meds)
    avg_r   = sum(rest_meds) / len(rest_meds)
    speedup = avg_r / avg_g if avg_g else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"GQL median @ {top_level} users",  f"{avg_g:.0f} ms")
    c2.metric(f"REST median @ {top_level} users", f"{avg_r:.0f} ms",
              delta=f"{avg_r - avg_g:+.0f} ms", delta_color="inverse")
    c3.metric("Speedup",  f"{speedup:.1f}×")
    c4.metric("Concurrency levels tested", len(present_levels))

st.divider()


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4 = st.tabs([
    "Latency Scaling",
    "Throughput Scaling",
    "GQL vs REST (per level)",
    "Raw Table",
])


# ── Tab 1: Latency scaling ────────────────────────────────────────────────────

with tab1:
    st.subheader("How Latency Grows with Concurrent Users")
    st.markdown(
        "Each line is one scenario. **Solid = GraphQL**, **dashed = REST**. "
        "A steeply rising REST line means that scenario degrades badly under load — "
        "N+1 chains block server connections longer, compounding at higher concurrency."
    )

    metric_key = st.radio("Metric", ["Median", "p95"], horizontal=True, key="scale_metric")
    col_key    = "median_ms" if metric_key == "Median" else "p95_ms"

    fig = go.Figure()
    for sc in selected_scenarios:
        color = SCENARIO_COLORS.get(sc, "#888")
        label = SCENARIO_LABELS.get(sc, sc)

        # GraphQL line (solid)
        gql_ys = [
            (row[col_key] if (row := _get("gql", u, sc)) and row[col_key] else None)
            for u in present_levels
        ]
        if any(v is not None for v in gql_ys):
            fig.add_trace(go.Scatter(
                x=present_levels, y=gql_ys,
                mode="lines+markers",
                name=f"{label} — GQL",
                line=dict(color=color, width=2.5, dash="solid"),
                marker=dict(size=9, symbol="circle"),
                hovertemplate=f"<b>{label} GQL</b><br>%{{x}} users: %{{y:.0f}} ms<extra></extra>",
            ))

        # REST line (dashed)
        rest_ys = [
            (row[col_key] if (row := _get("rest", u, sc)) and row[col_key] else None)
            for u in present_levels
        ]
        if any(v is not None for v in rest_ys):
            fig.add_trace(go.Scatter(
                x=present_levels, y=rest_ys,
                mode="lines+markers",
                name=f"{label} — REST",
                line=dict(color=color, width=2.5, dash="dash"),
                marker=dict(size=9, symbol="diamond"),
                hovertemplate=f"<b>{label} REST</b><br>%{{x}} users: %{{y:.0f}} ms<extra></extra>",
            ))

    fig.update_layout(
        xaxis=dict(title="Concurrent users", tickvals=present_levels),
        yaxis_title=f"{metric_key} latency (ms)",
        legend_title="Scenario — API",
        plot_bgcolor="white",
        height=520,
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Solid line = GraphQL (1 request regardless of users). "
        "Dashed line = REST (flat request or full N+1 chain). "
        "Lines that rise steeply identify scenarios where REST degrades under load."
    )


# ── Tab 2: Throughput scaling ─────────────────────────────────────────────────

with tab2:
    st.subheader("How Throughput Scales with Concurrent Users")
    st.markdown(
        "Requests (or chain completions) per second. Ideally throughput grows with users — "
        "a plateau or drop means the server (or the REST chain length) is the bottleneck."
    )

    fig2 = go.Figure()
    for sc in selected_scenarios:
        color = SCENARIO_COLORS.get(sc, "#888")
        label = SCENARIO_LABELS.get(sc, sc)

        gql_rps = [
            (row["rps"] if (row := _get("gql", u, sc)) and row["rps"] else None)
            for u in present_levels
        ]
        if any(v is not None for v in gql_rps):
            fig2.add_trace(go.Scatter(
                x=present_levels, y=gql_rps,
                mode="lines+markers",
                name=f"{label} — GQL",
                line=dict(color=color, width=2.5, dash="solid"),
                marker=dict(size=9, symbol="circle"),
                hovertemplate=f"<b>{label} GQL</b><br>%{{x}} users: %{{y:.2f}} req/s<extra></extra>",
            ))

        rest_rps = [
            (row["rps"] if (row := _get("rest", u, sc)) and row["rps"] else None)
            for u in present_levels
        ]
        if any(v is not None for v in rest_rps):
            fig2.add_trace(go.Scatter(
                x=present_levels, y=rest_rps,
                mode="lines+markers",
                name=f"{label} — REST",
                line=dict(color=color, width=2.5, dash="dash"),
                marker=dict(size=9, symbol="diamond"),
                hovertemplate=f"<b>{label} REST</b><br>%{{x}} users: %{{y:.2f}} req/s<extra></extra>",
            ))

    fig2.update_layout(
        xaxis=dict(title="Concurrent users", tickvals=present_levels),
        yaxis_title="Completed operations / s",
        legend_title="Scenario — API",
        plot_bgcolor="white",
        height=520,
    )
    st.plotly_chart(fig2, use_container_width=True)
    st.caption(
        "GraphQL throughput should scale more smoothly — each VU completes quickly "
        "and moves to the next task. REST N+1 chains hold a server connection open "
        "for many round-trips, reducing how many chains can complete per second."
    )


# ── Tab 3: GQL vs REST at a chosen concurrency level ─────────────────────────

with tab3:
    st.subheader("GraphQL vs REST — Head-to-Head at a Chosen Concurrency Level")
    st.markdown(
        "Pick a concurrency level to see the direct GraphQL vs REST comparison, "
        "same as the main Locust dashboard but focused on one level at a time."
    )

    level = st.radio(
        "Concurrent users",
        options=present_levels,
        format_func=lambda u: f"{u} user{'s' if u > 1 else ''}",
        horizontal=True,
    )

    metric3    = st.radio("Metric", ["Median", "p95"], horizontal=True, key="cmp_metric")
    col_key3   = "median_ms" if metric3 == "Median" else "p95_ms"

    labels3, gql_vals3, rest_vals3 = [], [], []
    for sc in selected_scenarios:
        g = _get("gql",  level, sc)
        r = _get("rest", level, sc)
        if not g and not r:
            continue
        labels3.append(SCENARIO_LABELS.get(sc, sc))
        gql_vals3.append(g[col_key3]  if g and g[col_key3]  else 0)
        rest_vals3.append(r[col_key3] if r and r[col_key3] else 0)

    fig3 = go.Figure()
    fig3.add_trace(go.Bar(
        name="GraphQL", x=labels3, y=gql_vals3,
        marker_color=API_COLORS["gql"], opacity=0.88,
        hovertemplate="<b>%{x}</b><br>" + metric3 + ": %{y:.0f} ms<extra>GraphQL</extra>",
    ))
    fig3.add_trace(go.Bar(
        name="REST", x=labels3, y=rest_vals3,
        marker_color=API_COLORS["rest"], opacity=0.88,
        hovertemplate="<b>%{x}</b><br>" + metric3 + ": %{y:.0f} ms<extra>REST</extra>",
    ))
    fig3.update_layout(
        barmode="group",
        yaxis_title=f"{metric3} latency (ms)",
        legend_title="API",
        plot_bgcolor="white",
        height=450,
        xaxis_tickangle=-20,
    )
    st.plotly_chart(fig3, use_container_width=True)

    # Speedup row
    speedups = []
    for sc, g_v, r_v in zip(selected_scenarios, gql_vals3, rest_vals3):
        if g_v and r_v:
            speedups.append((SCENARIO_LABELS.get(sc, sc), r_v / g_v))

    if speedups:
        st.markdown(f"**Speedup at {level} user{'s' if level > 1 else ''} (REST ÷ GQL):**")
        cols = st.columns(len(speedups))
        for col, (sc_label, ratio) in zip(cols, speedups):
            col.metric(sc_label, f"{ratio:.2f}×")


# ── Tab 4: Raw table ──────────────────────────────────────────────────────────

with tab4:
    st.subheader("Raw Numbers")
    table_rows = []
    for sc in selected_scenarios:
        for users in present_levels:
            for api in ("gql", "rest"):
                row = _get(api, users, sc)
                if not row:
                    continue
                table_rows.append({
                    "Scenario":    SCENARIO_LABELS.get(sc, sc),
                    "API":         api.upper(),
                    "Users":       users,
                    "Median (ms)": row["median_ms"],
                    "p95 (ms)":    row["p95_ms"],
                    "Req/s":       round(row["rps"], 2) if row["rps"] else None,
                    "Requests":    int(row["req_count"]) if row["req_count"] else None,
                    "Failures":    int(row["fail_count"]) if row["fail_count"] else None,
                })
    st.dataframe(
        sorted(table_rows, key=lambda r: (r["Scenario"], r["Users"], r["API"])),
        use_container_width=True,
    )
