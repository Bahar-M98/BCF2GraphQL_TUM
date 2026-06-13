"""
benchmark.py — Compare GraphQL vs REST API performance for BCF data access.

WHY THIS FILE EXISTS
────────────────────
The thesis claims GraphQL is a better fit than REST for querying linked BCF/IFC data.
"Better fit" must be demonstrated empirically, not just argued.  This script is the
experimental apparatus: it sends the same logical queries to both APIs, measures three
independent variables (request count, payload size, wall-clock latency), and writes the
raw numbers to CSV so they can be analysed and charted independently of the code.

WHAT IS BEING COMPARED
───────────────────────
Both APIs run inside the same FastAPI process, share the same MongoDB database and the
same IFC files on disk.  The only variable is the query interface.  This eliminates
network topology, hardware differences, and data differences as confounds.

SCENARIOS
─────────
Six scenarios are chosen to cover the two distinct query shapes that exist in the BCF
domain:

  Shape A — flat resource fetch: one HTTP call returns all the data needed.
    Both APIs make exactly 1 request.  This is the control condition that shows what
    happens when GraphQL has no structural advantage.

  Shape B — relational (N+1) fetch: the client must first get a list of N resources,
    then make one or more follow-up calls per resource to retrieve related sub-resources.
    REST is structurally forced into this pattern by its one-resource-per-endpoint
    design.  GraphQL resolves the entire graph in 1 call regardless of N.

  Scenarios by shape:
    topics_flat       (A) — same fields as REST /topics, 1 call each
    topic_events      (A) — BCF 3.0 §3.9 audit log, 1 call each
    comment_events    (A) — BCF 3.0 §3.10 audit log, 1 call each
    topics_nested     (B) — topics + viewpoints + IFC GUIDs: REST needs 1 + 2N calls
    project_comments  (B) — topics + all comments:          REST needs 1 + N calls
    topic_full        (B) — topics + comments + files + viewpoint selections:
                            REST needs 1 + 3N + N×V calls

DATASET TIERS
─────────────
Three project IDs are used as dataset sizes.  The project ID doubles as the tier name
because the server is configured to seed each project with a different amount of data:
  benchmark_small   —  25 topics × 3 comments × 1 viewpoint
  benchmark_medium  — 100 topics × 5 comments × 1 viewpoint
  benchmark_large   — 500 topics × 8 comments × 1 viewpoint

Running all three tiers lets us observe whether the advantage scales with N (it does
for N+1 scenarios — the speedup ratio grows as N grows).

TIMING METHODOLOGY
──────────────────
time.perf_counter() brackets the entire interaction for a scenario — including all
chained REST calls in N+1 scenarios.  This is the wall-clock cost a real client would
pay, which is the metric that matters in practice.

WARMUP
──────
WARMUP_RUNS requests are made before timing begins.  This:
  1. Establishes the TCP connection (httpx.Client reuses it across calls).
  2. Ensures MongoDB query plans are cached by the server.
  3. Eliminates JIT and import-time overhead from the first measured run.
Without warmup the first run is often an outlier that inflates the mean.

PAYLOAD MEASUREMENT
───────────────────
len(response.content) is the byte count of the response body as received.  The httpx
client is created with Accept-Encoding: identity, which explicitly disables gzip/brotli
compression on both APIs.  This ensures byte counts represent raw JSON sizes regardless
of any GZipMiddleware that might be added to the server in future.

NOTE — GraphQL envelope overhead:
Every GraphQL response is wrapped in {"data": {"<field>": ...}} which adds a constant
~20 bytes that REST responses (plain JSON arrays) do not carry.  This overhead is
format-mandated and independent of dataset size.  It is not subtracted from GraphQL
byte counts — it is reported as-is so the reader can see the true wire cost — but it
should be borne in mind when interpreting flat/control scenario results where the
content on both sides is otherwise identical.

HOW TO RUN
──────────
  # Start the server first:
  uv run uvicorn main:app --port 8000

  # Then in another terminal:
  uv run python benchmark.py
  uv run python benchmark.py --url http://localhost:8000 --runs 20

OUTPUT
──────
  benchmark_results_raw.csv     — one row per individual run
                                  columns: tier, scenario, api, run, requests, bytes, elapsed_ms
  benchmark_results_summary.csv — median + 95% bootstrap CI per scenario × tier × api
                                  used for thesis charts and tables
"""

import argparse
import csv
import statistics
import time
from pathlib import Path

import httpx
import numpy as np
from scipy.stats import bootstrap as scipy_bootstrap

# ── Configuration ──────────────────────────────────────────────────────────────

DEFAULT_URL  = "http://localhost:8000"
WARMUP_RUNS  = 3    # discarded before measurement — eliminates cold-start noise
DEFAULT_RUNS = 10   # effect sizes are large and variance is low — 10 runs give clean CIs

# Project IDs that the server recognises as seeded benchmark datasets.
# The string is both the MongoDB projectId and a human-readable size label.
TIERS = [
    "benchmark_small",
    "benchmark_medium",
    "benchmark_large",
]

# IFC-element-scaling tiers — topic count is fixed at 50, only element count varies.
# Used exclusively by the ifc_element_scaling scenario to isolate element count
# as an independent variable separate from the topic-count scaling above.
IFC_TIERS = [
    "benchmark_ifc_s1",
    "benchmark_ifc_s3",
    "benchmark_ifc_s5",
]

# Viewpoint-scaling tiers — topic count fixed at 50, viewpoint count varies 1/3/5.
# Used exclusively by the viewpoint_scaling scenario.
VP_TIERS = ["benchmark_vp_v1", "benchmark_vp_v3", "benchmark_vp_v5"]
VP_VIEWPOINT_COUNTS = {"benchmark_vp_v1": 1, "benchmark_vp_v3": 3, "benchmark_vp_v5": 5}

GQL_PATH = "/graphql/"


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _gql(client: httpx.Client, query: str, variables: dict = None) -> tuple[int, int, float]:
    """
    POST one GraphQL request and return (request_count=1, response_bytes, elapsed_seconds).

    GraphQL always uses a single POST to /graphql regardless of how many types or
    fields the query spans.  request_count is returned as 1 so the caller accumulates
    it the same way as REST chains.

    Variables are sent as a separate JSON key rather than string-interpolated into the
    query to avoid injection issues and to allow the server to cache the parsed query
    document independently of the variable values.
    """
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    t0 = time.perf_counter()
    r = client.post(GQL_PATH, json=payload)
    elapsed = time.perf_counter() - t0
    r.raise_for_status()
    return 1, len(r.content), elapsed


def _rest(client: httpx.Client, path: str, params: dict = None) -> tuple[dict, int]:
    """
    GET one REST endpoint and return (parsed_json_body, response_bytes).

    Returns parsed JSON because REST callers need to iterate over the response to
    build follow-up request URLs (e.g. iterating topics to fetch /topics/{guid}/comments).
    The byte count is tracked separately so callers can accumulate total bytes across
    all chained requests in an N+1 sequence.

    Timing is NOT measured here — the outer REST scenario function wraps the entire
    chain in a single perf_counter bracket so the latency includes all chained calls.
    """
    r = client.get(path, params=params)
    r.raise_for_status()
    return r.json(), len(r.content)


# ── Scenario implementations ──────────────────────────────────────────────────
# Each function has the signature: (client, project_id) → (request_count, bytes, elapsed_s)
# This uniform signature lets the runner call any scenario the same way.


# ── 1. topics_flat ─────────────────────────────────────────────────────────────

def topics_flat_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    Fetch all topics for a project — requesting exactly the same fields that REST
    returns from GET /topics, making this a true apples-to-apples comparison.

    DESIGN NOTE: The BCF REST spec returns every scalar topic field from /topics but
    deliberately excludes sub-resources (comments, viewpoints).  Those require separate
    calls.  To make the comparison fair, this GraphQL query also excludes comments and
    viewpoints so both APIs return identical logical content via a single request.

    This is the CONTROL scenario.  If the two APIs perform differently here, the
    difference is due to serialisation overhead or framework routing cost, not query
    shape.  Any difference should be small and consistent across tiers.
    """
    q = """
    query($pid: ID!) {
        topics(projectId: $pid) {
            guid
            serverAssignedId
            topicType
            topicStatus
            referenceLinks
            title
            priority
            index
            labels
            creationDate  { ISO8601 }
            creationAuthor
            modifiedDate  { ISO8601 }
            modifiedAuthor
            dueDate       { ISO8601 }
            assignedTo
            stage
            description
            bimSnippet    { snippetType isExternal reference referenceSchema }
            documentReferences { guid url documentGuid description }
            relatedTopics { guid }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def topics_flat_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    BCF REST GET /topics — single call, returns topic summaries without sub-resources.

    The BCF 3.0 spec intentionally omits comments and viewpoints from this response
    to keep the payload manageable.  They must be fetched separately per topic, which
    is exactly what the N+1 scenarios measure.
    """
    t0 = time.perf_counter()
    _, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics")
    return 1, b, time.perf_counter() - t0


# ── 2. topics_nested ──────────────────────────────────────────────────────────

def topics_nested_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    Topics → viewpoints → components → selected IFC element GUIDs — single request.

    This query traverses three levels of nesting in one POST.  GraphQL resolves each
    level by calling the appropriate resolver in sequence server-side, but the client
    sees only one round-trip and one response.

    This is the PRIMARY N+1 stress test scenario.  The equivalent REST chain requires
    1 + 2N requests (see topics_nested_rest below).
    """
    q = """
    query($pid: ID!) {
        topics(projectId: $pid) {
            guid title topicStatus
            viewpoints {
                guid
                components {
                    selection {
                        ifcGuid
                        originatingSystem
                    }
                }
            }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def topics_nested_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    REST equivalent of topics_nested — demonstrates the N+1 problem.

    The BCF REST spec separates each sub-resource onto its own endpoint:
      GET /topics               → topic stubs (no viewpoints, no components)
      GET /topics/{g}/viewpoints → viewpoint stubs (guid only, no components embedded)
      GET /topics/{g}/viewpoints/{v}/selection → IFC component GUIDs

    There is no way in the BCF REST spec to collapse these into fewer calls.  The spec
    is faithful to REST's one-resource-per-endpoint constraint, which is exactly the
    structural property that GraphQL was designed to overcome.

    Request count: 1 (topic list) + N (viewpoint stubs) + N×V (selections)
                 = 1 + 2N for datasets with 1 viewpoint per topic
                 grows with both N (topics) and V (viewpoints per topic)

    The timer wraps the entire chain so elapsed_s reflects the real client cost.
    """
    t0 = time.perf_counter()
    topics, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics")
    req_count   = 1
    total_bytes = b

    for topic in topics:
        guid = topic["guid"]

        # Viewpoint stubs only contain guid, filename, snapshot, index.
        # Components are not embedded — a separate /selection call is required.
        vp_stubs, vb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints")
        total_bytes += vb
        req_count   += 1

        # One /selection call per viewpoint to retrieve the IFC component GUIDs.
        for vp in vp_stubs:
            _, sb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vp['guid']}/selection")
            total_bytes += sb
            req_count   += 1

    return req_count, total_bytes, time.perf_counter() - t0


# ── 3. topic_events ───────────────────────────────────────────────────────────

def topic_events_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    BCF 3.0 Section 3.9 topic audit log — single GraphQL request.

    topicEvents is a flat list query: the server aggregates all topic version diffs
    into a single event stream before returning.  No relationship traversal is needed,
    so both APIs make exactly 1 request.  This is a Shape A (flat) scenario.

    Expected result: both APIs perform similarly, confirming GraphQL does not add
    overhead for queries that don't benefit from graph traversal.
    """
    q = """
    query($pid: ID!) {
        topicEvents(projectId: $pid) {
            topicGuid
            date { ISO8601 }
            author
            actions { type value }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def topic_events_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """BCF 3.0 Section 3.9 — GET /topics/events, single flat list response."""
    t0 = time.perf_counter()
    _, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/events")
    return 1, b, time.perf_counter() - t0


# ── 4. comment_events ─────────────────────────────────────────────────────────

def comment_events_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    BCF 3.0 Section 3.10 comment audit log — single GraphQL request.

    Like topic_events, this is a flat list pre-computed server-side.  Both APIs use
    1 request.  Another Shape A control scenario to confirm parity on flat queries.
    """
    q = """
    query($pid: ID!) {
        commentEvents(projectId: $pid) {
            commentGuid topicGuid
            date { ISO8601 }
            author
            actions { type value }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def comment_events_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """BCF 3.0 Section 3.10 — GET /topics/comments/events, single flat list response."""
    t0 = time.perf_counter()
    _, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/comments/events")
    return 1, b, time.perf_counter() - t0


# ── 5. project_comments ───────────────────────────────────────────────────────

def project_comments_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    All topic fields + all comments for every topic in a project — single request.

    Requests the same 20 BCF scalar topic fields that GET /topics returns, plus the
    comments sub-selection.  This makes it a true counterpart to project_comments_rest:
    both sides return identical logical content (topic summaries + all their comments)
    and the only variable is the number of HTTP round trips.

    REST equivalent (see below) needs 1 + N calls.  This is a Shape B (N+1) scenario
    but with only one level of nesting rather than the two levels in topics_nested.
    """
    q = """
    query($pid: ID!) {
        topics(projectId: $pid) {
            guid
            serverAssignedId
            topicType
            topicStatus
            referenceLinks
            title
            priority
            index
            labels
            creationDate  { ISO8601 }
            creationAuthor
            modifiedDate  { ISO8601 }
            modifiedAuthor
            dueDate       { ISO8601 }
            assignedTo
            stage
            description
            bimSnippet    { snippetType isExternal reference referenceSchema }
            documentReferences { guid url documentGuid description }
            relatedTopics { guid }
            comments {
                guid
                date { ISO8601 }
                author
                comment
                modifiedDate { ISO8601 }
                modifiedAuthor
                viewpointGuid
            }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def project_comments_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    REST: fetch topic list then fetch comments separately for each topic.

    The BCF spec does not provide a "all comments for a project" endpoint — comments
    are nested under topics, so the only path is topic-by-topic.

    Request count: 1 (topic list) + N (one /comments call per topic).

    The GraphQL counterpart is project_comments_gql, which resolves the same data
    in a single request by nesting comments inside the topics query.
    """
    t0 = time.perf_counter()
    topics, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics")
    req_count   = 1
    total_bytes = b

    for topic in topics:
        _, cb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{topic['guid']}/comments")
        total_bytes += cb
        req_count   += 1

    return req_count, total_bytes, time.perf_counter() - t0


# ── 6. topic_full ─────────────────────────────────────────────────────────────

def topic_full_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    Per topic: comments + IFC files + viewpoints with IFC selections — 1 request.

    This is the deepest nesting scenario: three independent sub-resource types are
    requested simultaneously for every topic.  GraphQL resolves all three field
    resolvers (comments, files, viewpoints.components.selection) in the same
    execution pass and assembles the result before responding.

    The REST equivalent (see below) requires 1 + 3N + N×V calls, making this the
    scenario with the largest absolute and relative difference between the two APIs.
    """
    q = """
    query($pid: ID!) {
        topics(projectId: $pid) {
            guid title
            comments {
                guid author comment
                date { ISO8601 }
            }
            files {
                fileName ifcProjectGuid
                date { ISO8601 }
            }
            viewpoints {
                guid
                components {
                    selection {
                        ifcGuid
                        originatingSystem
                    }
                }
            }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def topic_full_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    REST: for every topic, separately fetch comments, header files, and viewpoint selections.

    This is the worst-case N+1 scenario.  Each topic requires four endpoint calls:
      GET /topics/{guid}/comments    → comment list
      GET /topics/{guid}/files       → IFC header files
      GET /topics/{guid}/viewpoints  → viewpoint stubs (guid only)
      GET /topics/{guid}/viewpoints/{vguid}/selection  → IFC GUIDs (one per viewpoint)

    Total request count: 1 + 3N + N×V
      where N = number of topics and V = average viewpoints per topic.

    At benchmark_large (500 topics × 1 viewpoint): 1 + 500 + 500 = 1001 requests.
    The timer includes the entire chain, so elapsed_s is the full client experience.
    """
    t0 = time.perf_counter()
    topics, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics")
    req_count   = 1
    total_bytes = b

    for topic in topics:
        guid = topic["guid"]

        # Comments — separate endpoint, not embedded in topic summary
        _, cb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/comments")
        total_bytes += cb
        req_count   += 1

        # IFC header files — separate endpoint per BCF spec
        _, fb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/files")
        total_bytes += fb
        req_count   += 1

        # Viewpoint stubs — components are not included here, only guid and metadata
        vp_stubs, vb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints")
        total_bytes += vb
        req_count   += 1

        # IFC component selection — one more call per viewpoint
        for vp in vp_stubs:
            _, sb = _rest(
                client,
                f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vp['guid']}/selection",
            )
            total_bytes += sb
            req_count   += 1

    return req_count, total_bytes, time.perf_counter() - t0


# ── 7. ifc_element_scaling ────────────────────────────────────────────────────

def ifc_element_scaling_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    Topics → viewpoints → IFC component GUIDs — identical query to topics_nested.

    Run against IFC_TIERS (fixed 50 topics, element count 1/3/5) so that the
    only variable between tiers is how many IFC elements each viewpoint references.
    This isolates element count as an independent scaling dimension and lets the
    thesis show whether GraphQL's single-request advantage grows, shrinks, or stays
    flat as element density increases (it stays flat — 1 request regardless).
    """
    q = """
    query($pid: ID!) {
        topics(projectId: $pid) {
            guid title topicStatus
            viewpoints {
                guid
                components {
                    selection {
                        ifcGuid
                        originatingSystem
                    }
                }
            }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def ifc_element_scaling_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    REST equivalent of ifc_element_scaling — same chain as topics_nested_rest.

    Request count: 1 (topics) + N (viewpoint stubs) + N (selections) = 1 + 2N.
    The total bytes increase with element count because each /selection response
    grows in proportion to the number of IFC component objects it contains.
    REST request count is unaffected by element count — the extra bytes come only
    from larger response bodies, not additional round-trips.
    """
    t0 = time.perf_counter()
    topics, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics")
    req_count   = 1
    total_bytes = b

    for topic in topics:
        guid = topic["guid"]
        vp_stubs, vb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints")
        total_bytes += vb
        req_count   += 1

        for vp in vp_stubs:
            _, sb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vp['guid']}/selection")
            total_bytes += sb
            req_count   += 1

    return req_count, total_bytes, time.perf_counter() - t0


# ── 8. overfetch_partial ──────────────────────────────────────────────────────

def overfetch_partial_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    Request only two fields from topics — the minimal query a client might make
    when it only needs to display an assignment list.

    GraphQL returns exactly those two fields per topic.  The response payload
    scales only with the number of topics, not with the total number of fields
    defined on the Topic type.  This is field selection: the client pays only
    for what it asked for.
    """
    q = """
    query($pid: ID!) {
        topics(projectId: $pid) {
            assignedTo
            creationAuthor
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def overfetch_partial_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    GET /topics — returns all 20 BCF spec fields regardless of what the client needs.

    The BCF REST spec does not support field selection.  A client that only needs
    assignedTo and creationAuthor still receives every field: title, description,
    priority, labels, bimSnippet, documentReferences, and so on.  The payload is
    identical to the flat/control scenario even though the client uses 2 of 20 fields.

    The byte ratio REST/GraphQL here directly measures the over-fetching cost for
    this field selection.  The ratio grows with topic count (more topics → more
    unused fields transmitted) but stays constant as a fraction of the full payload.
    """
    t0 = time.perf_counter()
    _, b = _rest(client, f"/bcf/3.0/projects/{project_id}/topics")
    return 1, b, time.perf_counter() - t0


# ── 9. viewpoint_scaling ─────────────────────────────────────────────────────

def viewpoint_scaling_gql(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    Topics → viewpoints → IFC component GUIDs in a single GraphQL request.

    Request count is always 1 regardless of how many viewpoints each topic has.
    The response payload grows with viewpoint count (more viewpoint objects
    returned) but the round-trip cost stays flat.
    """
    q = """
    query($pid: ID!) {
        topics(projectId: $pid) {
            guid title topicStatus
            viewpoints {
                guid
                components {
                    selection { ifcGuid originatingSystem }
                }
            }
        }
    }
    """
    return _gql(client, q, {"pid": project_id})


def viewpoint_scaling_rest(client: httpx.Client, project_id: str) -> tuple[int, int, float]:
    """
    REST chain: GET /topics → per topic GET /viewpoints → per viewpoint GET /selection.

    Request count: 1 + N_topics + N_topics × N_viewpoints_per_topic
      v1 → 101   v3 → 201   v5 → 301

    Each additional viewpoint per topic adds one more /selection round-trip
    per topic, so latency grows linearly with viewpoint count.
    """
    t0 = time.perf_counter()
    topics, b   = _rest(client, f"/bcf/3.0/projects/{project_id}/topics")
    req_count   = 1
    total_bytes = b

    for topic in topics:
        guid = topic["guid"]
        vp_stubs, vb = _rest(client, f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints")
        total_bytes += vb
        req_count   += 1

        for vp in vp_stubs:
            _, sb = _rest(
                client,
                f"/bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vp['guid']}/selection",
            )
            total_bytes += sb
            req_count   += 1

    return req_count, total_bytes, time.perf_counter() - t0


# ── Scenario registry ─────────────────────────────────────────────────────────
# Storing scenarios as dicts lets the runner iterate them uniformly without a
# large if/elif chain.  Adding a new scenario means adding one entry here.

SCENARIOS: list[dict] = [
    {
        "name":        "topics_flat",
        "description": "All topics, identical field set to REST (apples-to-apples)",
        "gql":         topics_flat_gql,
        "rest":        topics_flat_rest,
    },
    {
        "name":        "topics_nested",
        "description": "Topics → viewpoints → components → IFC GUIDs (N+1 in REST)",
        "gql":         topics_nested_gql,
        "rest":        topics_nested_rest,
    },
    {
        "name":        "topic_events",
        "description": "BCF 3.0 spec topic audit log (Section 3.9)",
        "gql":         topic_events_gql,
        "rest":        topic_events_rest,
    },
    {
        "name":        "comment_events",
        "description": "BCF 3.0 spec comment audit log (Section 3.10)",
        "gql":         comment_events_gql,
        "rest":        comment_events_rest,
    },
    {
        "name":        "project_comments",
        "description": "All comments for every topic in a project (1+N in REST)",
        "gql":         project_comments_gql,
        "rest":        project_comments_rest,
    },
    {
        "name":        "topic_full",
        "description": "Per topic: comments + files + viewpoints → IFC selection (1+3N+N×V in REST)",
        "gql":         topic_full_gql,
        "rest":        topic_full_rest,
    },
    {
        "name":        "overfetch_partial",
        "description": "2-field query (GraphQL) vs full topic payload (REST) — over-fetching cost",
        "gql":         overfetch_partial_gql,
        "rest":        overfetch_partial_rest,
    },
]

# IFC-element-scaling scenarios — run against IFC_TIERS only, not TIERS.
# Topic count is fixed so the only variable is the number of IFC elements per
# viewpoint, making it possible to isolate element count from topic count scaling.
IFC_SCENARIOS: list[dict] = [
    {
        "name":        "ifc_element_scaling",
        "description": "Topics → viewpoints → IFC GUIDs, fixed 50 topics, element count 1/3/5",
        "gql":         ifc_element_scaling_gql,
        "rest":        ifc_element_scaling_rest,
    },
]

# Viewpoint-scaling scenarios — run against VP_TIERS only.
# Topic count is fixed at 50 and element count at 2; only viewpoint count varies.
VP_SCENARIOS: list[dict] = [
    {
        "name":        "viewpoint_scaling",
        "description": "50 topics × N viewpoints × 2 IFC elements — viewpoint count 1/3/5",
        "gql":         viewpoint_scaling_gql,
        "rest":        viewpoint_scaling_rest,
    },
]


# ── Measurement helpers ───────────────────────────────────────────────────────

def _run_scenario(
    client:     httpx.Client,
    fn,
    project_id: str,
    runs:       int,
) -> tuple[list[tuple[int, int, float]], int, int]:
    """
    Warm up the server then collect measurement runs.

    Warmup: WARMUP_RUNS calls are made and discarded.  This ensures that:
      - The HTTP keep-alive connection is established (no TCP handshake cost in measured runs)
      - MongoDB query plans are warmed in the server process
      - Any lazy initialisation in resolvers has already happened

    Errors during individual runs are caught and printed rather than aborting the
    whole benchmark — a single timeout should not discard all other results.

    Returns (results, timeout_count, error_count).
    error_count covers non-timeout failures (HTTP 4xx/5xx, connection reset, OOM
    crash on the server side, etc.).  These are distinct from timeouts: a timeout
    means the server is alive but slow; an error means the server returned a failure
    or the connection was dropped entirely.
    """
    for _ in range(WARMUP_RUNS):
        try:
            fn(client, project_id)
        except Exception:
            pass

    results = []
    timeout_count = 0
    error_count   = 0
    for _ in range(runs):
        try:
            results.append(fn(client, project_id))
        except httpx.TimeoutException:
            timeout_count += 1
            print(f"\n    [timeout]", end="", flush=True)
        except Exception as exc:
            error_count += 1
            print(f"\n    [error] {type(exc).__name__}: {exc}", end="", flush=True)
    return results, timeout_count, error_count


def _bootstrap_ci(
    values: list[float],
    n_resamples: int = 2000,
    confidence: float = 0.95,
) -> tuple[float | None, float | None]:
    """95 % bootstrap CI for the median via scipy (percentile method, fixed seed)."""
    if len(values) < 2:
        v = values[0] if values else None
        return v, v
    result = scipy_bootstrap(
        (np.array(values),),
        statistic=np.median,
        n_resamples=n_resamples,
        confidence_level=confidence,
        method="percentile",
        random_state=42,
    )
    return round(float(result.confidence_interval.low), 2), round(float(result.confidence_interval.high), 2)


def _summarize(results: list[tuple[int, int, float]]) -> dict:
    """
    Compute median and p95 latency, median request count, and median byte count.

    DESIGN NOTE — why median and not mean:
    Latency distributions are right-skewed (occasional slow outliers due to OS
    scheduling, GC pauses, or MongoDB query plan recompilation).  The median is
    resistant to these outliers and better represents the typical client experience.

    DESIGN NOTE — bootstrap CI instead of p95:
    p95 is only meaningful when n >= 100 (at n=30, the p95 index is 28 — the
    2nd-highest value, which is just a dressed-up maximum).  The 95% bootstrap CI
    on the median is honest at n=30: it quantifies how stable the median estimate
    is across resamples, giving reviewers a direct measure of result reliability.

    Byte counts and request counts are deterministic (same data, same serialiser)
    so their median equals any individual run value; median is used for consistency.
    """
    if not results:
        return {
            "median_ms":    None,
            "ci_low_ms":    None,
            "ci_high_ms":   None,
            "median_reqs":  None,
            "median_bytes": None,
        }
    latencies   = sorted(r[2] * 1000 for r in results)   # convert seconds → milliseconds
    req_counts  = [r[0] for r in results]
    byte_counts = [r[1] for r in results]
    ci_low, ci_high = _bootstrap_ci(latencies)
    return {
        "median_ms":    round(statistics.median(latencies), 2),
        "ci_low_ms":    ci_low,
        "ci_high_ms":   ci_high,
        "median_reqs":  statistics.median(req_counts),
        "median_bytes": int(statistics.median(byte_counts)),
    }


# ── Pre-flight helpers ────────────────────────────────────────────────────────

def _check_tiers(client: httpx.Client, tiers: list[str]) -> list[str]:
    """
    Return the subset of tiers whose data is actually seeded on the server.

    A tier is considered seeded if GET /bcf/3.0/projects/{tier}/topics returns
    HTTP 200.  Any other status (404, 422, 500) means the data is missing or the
    server is broken for that tier — running benchmark scenarios against it would
    produce silent error rows with no useful numbers.

    Prints a warning for every unseeded tier so the user knows to run
    generate_benchmark_data.py (or generate_extra_data.py) first.
    """
    ok = []
    for tier in tiers:
        try:
            r = client.get(f"/bcf/3.0/projects/{tier}/topics", timeout=30.0)
            if r.status_code == 200:
                ok.append(tier)
            else:
                print(f"  ⚠ SKIP {tier!r} — server returned HTTP {r.status_code} "
                      f"(data not seeded? run generate_benchmark_data.py)")
        except Exception as exc:
            print(f"  ✗ SKIP {tier!r} — pre-flight request failed: {type(exc).__name__}: {exc}")
    return ok


def _wait_for_server(client: httpx.Client, url: str) -> None:
    """
    Block until the server responds to GET /docs (the health-check path used by
    Render).  Render free-tier instances spin down after 15 minutes of inactivity
    and take up to ~30 seconds to cold-start.  Without this wait, the first
    warmup requests hit a starting instance and inflate latency measurements.
    """
    print(f"Waiting for server at {url} …", end="", flush=True)
    for attempt in range(24):  # up to 120 s
        try:
            r = client.get("/docs", timeout=10.0)
            if r.status_code < 500:
                print(f" ready ({attempt * 5}s)")
                return
        except Exception:
            pass
        import time as _time
        _time.sleep(5)
        print(".", end="", flush=True)
    print(" timed out — continuing anyway")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """
    Outer loop: for every (tier, scenario, api) combination, warm up and measure.

    DESIGN NOTE — single persistent httpx.Client:
    Using one client for the entire benchmark run means TCP connections are reused
    across calls (HTTP keep-alive).  This reflects real client behaviour and avoids
    measuring TCP handshake costs on every REST sub-request in N+1 chains.
    Timeout is set to 180 s to accommodate large-tier REST chains that can take
    60+ seconds.

    DESIGN NOTE — two output files:
    Raw CSV preserves every individual run so the reader can verify the median
    calculation, check for outliers, or recompute statistics with a different method.
    Summary CSV is what gets pasted into the thesis tables and fed to dashboard.py.
    Separating them avoids any ambiguity about where the aggregated numbers came from.
    """
    parser = argparse.ArgumentParser(description="BCF GraphQL vs REST benchmark")
    parser.add_argument("--url",  default=DEFAULT_URL,  help="Base server URL (default: %(default)s)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="Measurement runs per scenario (default: %(default)s)")
    parser.add_argument(
        "--tiers", nargs="+", metavar="TIER",
        help=(
            "Run only these tiers (space-separated).  Use to resume after a crash "
            "or to re-run a single tier.  Example: "
            "--tiers benchmark_large benchmark_ifc_s3"
        ),
    )
    parser.add_argument(
        "--label", default="local",
        help="Label for output filenames, e.g. local or render (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.tiers:
        unknown = [t for t in args.tiers if t not in TIERS + IFC_TIERS + VP_TIERS]
        if unknown:
            parser.error(f"Unknown tier(s): {', '.join(unknown)}. "
                         f"Valid tiers: {', '.join(TIERS + IFC_TIERS + VP_TIERS)}")

    raw_rows:     list[dict] = []
    summary_rows: list[dict] = []

    raw_path     = Path(__file__).parent.parent / "results" / f"benchmark_results_raw_{args.label}.csv"
    summary_path = Path(__file__).parent.parent / "results" / f"benchmark_results_summary_{args.label}.csv"
    raw_path.parent.mkdir(exist_ok=True)

    print("BCF GraphQL vs REST Benchmark")
    print(f"Server : {args.url}")
    print(f"Runs   : {args.runs} (+ {WARMUP_RUNS} warmup)")
    print("=" * 76)

    with httpx.Client(
        base_url=args.url,
        timeout=600.0,
        headers={"Accept-Encoding": "identity"},
    ) as client:

        def _measure(tiers, scenarios):
            """Run every (tier, scenario, api) combination and append to row lists."""
            for tier in tiers:
                print(f"\n── Tier: {tier}")
                for sc in scenarios:
                    for api_label, fn in [("graphql", sc["gql"]), ("rest", sc["rest"])]:
                        label = f"  {sc['name']}/{api_label}"
                        print(f"{label:<48}", end="", flush=True)

                        results, n_timeouts, n_errors = _run_scenario(client, fn, tier, args.runs)
                        s = _summarize(results)

                        if s["median_ms"] is None:
                            failure_note = []
                            if n_timeouts:
                                failure_note.append(f"{n_timeouts} timeout(s)")
                            if n_errors:
                                failure_note.append(f"{n_errors} error(s)")
                            print(f"  [no results — {', '.join(failure_note) or 'unknown'}]")
                        else:
                            notes = []
                            if n_timeouts:
                                notes.append(f"⚠ {n_timeouts} timeout(s)")
                            if n_errors:
                                notes.append(f"✗ {n_errors} error(s)")
                            print(
                                f"median={s['median_ms']:>8}ms  "
                                f"95%CI=[{s['ci_low_ms']}, {s['ci_high_ms']}]ms  "
                                f"reqs={s['median_reqs']}  "
                                f"bytes={s['median_bytes']}"
                                + (f"  {'  '.join(notes)}" if notes else "")
                            )

                        run_offset = 0
                        for i, (reqs, b, elapsed) in enumerate(results):
                            raw_rows.append({
                                "tier":        tier,
                                "scenario":    sc["name"],
                                "api":         api_label,
                                "run":         i + 1,
                                "requests":    reqs,
                                "bytes":       b,
                                "elapsed_ms":  round(elapsed * 1000, 3),
                                "timed_out":   False,
                                "errored":     False,
                            })
                            run_offset = i + 1
                        for i in range(n_timeouts):
                            raw_rows.append({
                                "tier":        tier,
                                "scenario":    sc["name"],
                                "api":         api_label,
                                "run":         run_offset + i + 1,
                                "requests":    "",
                                "bytes":       "",
                                "elapsed_ms":  "",
                                "timed_out":   True,
                                "errored":     False,
                            })
                        for i in range(n_errors):
                            raw_rows.append({
                                "tier":        tier,
                                "scenario":    sc["name"],
                                "api":         api_label,
                                "run":         run_offset + n_timeouts + i + 1,
                                "requests":    "",
                                "bytes":       "",
                                "elapsed_ms":  "",
                                "timed_out":   False,
                                "errored":     True,
                            })

                        summary_rows.append({
                            "tier":          tier,
                            "scenario":      sc["name"],
                            "description":   sc["description"],
                            "api":           api_label,
                            "median_ms":     s["median_ms"],
                            "ci_low_ms":     s["ci_low_ms"],
                            "ci_high_ms":    s["ci_high_ms"],
                            "median_reqs":   s["median_reqs"],
                            "median_bytes":  s["median_bytes"],
                            "timeout_count": n_timeouts,
                            "error_count":   n_errors,
                        })

                # ── Checkpoint: flush CSVs after every tier so a crash doesn't
                # lose completed data.  The files are overwritten each time.
                if raw_rows:
                    with raw_path.open("w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=raw_rows[0].keys())
                        writer.writeheader()
                        writer.writerows(raw_rows)
                if summary_rows:
                    with summary_path.open("w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=summary_rows[0].keys())
                        writer.writeheader()
                        writer.writerows(summary_rows)
                print(f"  ✓ checkpoint saved → {raw_path.name}")

        _wait_for_server(client, args.url)

        # Apply --tiers filter before pre-flight so we only probe what will run.
        wanted_topic_tiers = [t for t in TIERS     if not args.tiers or t in args.tiers]
        wanted_ifc_tiers   = [t for t in IFC_TIERS if not args.tiers or t in args.tiers]

        print("\n── Pre-flight: checking seeded tiers …")
        active_tiers     = _check_tiers(client, wanted_topic_tiers)
        active_ifc_tiers = _check_tiers(client, wanted_ifc_tiers)
        if not active_tiers and not active_ifc_tiers:
            print("No seeded tiers found. Run generate_benchmark_data.py first.")
            return

        print("\n── Topic-scaling scenarios (element count random, topic count varies)")
        if active_tiers:
            _measure(active_tiers, SCENARIOS)
        else:
            print("  (all topic-scaling tiers skipped — no seeded data)")

        print("\n\n── IFC-element-scaling scenarios (topic count fixed at 50, element count 1/3/5)")
        if active_ifc_tiers:
            _measure(active_ifc_tiers, IFC_SCENARIOS)
        else:
            print("  (all IFC-scaling tiers skipped — no seeded data)")

        print("\n\n── Viewpoint-scaling scenarios (topic count fixed at 50, viewpoint count 1/3/5)")
        active_vp_tiers = _check_tiers(client, [t for t in VP_TIERS if not args.tiers or t in args.tiers])
        if active_vp_tiers:
            _measure(active_vp_tiers, VP_SCENARIOS)
        else:
            print("  (all VP-scaling tiers skipped — no seeded data)")

    print(f"\n{'=' * 76}")
    print(f"Raw results  → {raw_path}")
    print(f"Summary      → {summary_path}")


if __name__ == "__main__":
    main()
