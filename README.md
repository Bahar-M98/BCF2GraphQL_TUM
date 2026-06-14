<div align="center">

# BCF2GraphQL

### Is GraphQL a better fit than REST for connected BIM data?

**A research server that serves the same Building Information Modelling data through a [GraphQL API](#the-graphql-api) and the official [buildingSMART BCF REST API 3.0](#the-bcf-rest-api), built to test whether GraphQL is a more efficient and more expressive interface for linked BCF and IFC data.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![GraphQL](https://img.shields.io/badge/GraphQL-Ariadne-E10098?logo=graphql&logoColor=white)](https://ariadnegraphql.org/)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-47A248?logo=mongodb&logoColor=white)](https://www.mongodb.com/atlas)
[![BCF 3.0](https://img.shields.io/badge/buildingSMART-BCF%203.0-005A9C)](https://github.com/buildingSMART/BCF-API/tree/release_3_0)

[**Live demo**](https://bcf2graphql.onrender.com/graphql) · [**Quick start**](#quick-start) · [**What's inside**](#whats-inside) · [**The two APIs**](#the-two-apis) · [**Architecture**](#architecture)

</div>

---

## What this is

BCF2GraphQL serves the same Building Information Modelling data through two APIs running side by side: a **GraphQL API** and a faithful implementation of the official **[buildingSMART BCF REST API 3.0](https://github.com/buildingSMART/BCF-API/tree/release_3_0)** spec. Around them it bundles the cross-format integration queries, two web viewers, sample data, and the benchmark suite used to compare the two interfaces.

It was built for a Master's thesis at the Technical University of Munich. The thesis covers the full method, design rationale, and results; this repository is the running system behind it.

## Why it exists

BCF issue data is relational: a project contains topics, each topic has comments and viewpoints, each viewpoint references IFC components, and each component points to an element in an IFC model. BCF REST 3.0 models each level as its own endpoint, so three problems show up in practice:

- A single "show me these topics with their elements" view becomes a long chain of sequential requests (the classic **N+1 problem**).
- REST returns fixed representations with **no field selection**, so clients over-fetch.
- The spec defines **no link** between a BCF issue and the IFC element it concerns, so clients have to reconstruct that relationship themselves.

This repo implements a GraphQL alternative that collapses those chains into a single request, lets the client pick exactly the fields it needs, and unifies BCF and IFC into one queryable graph, then measures the difference against the REST baseline. Both APIs run in the same process over the same data, so the query interface is the only variable.

<p align="center">
  <img src="assets/architecture-overview.png" alt="How BCF and IFC link: a BCF Component's ifcGuid maps to an IfcElement globalId, with forward, reverse, and version links between the two formats" width="560">
  <br>
  <sub>BCF and IFC share a GlobalId, which lets the schema link issues to elements forward, in reverse, and across model versions.</sub>
</p>

> In the thesis benchmark, GraphQL ran relational queries roughly 20x to 130x faster locally and up to ~290x in the cloud, while REST kept a small edge only on flat queries. Full numbers, scenarios, and method are in the thesis.

---

## What's inside

| Component | What it is | Why it's here |
|---|---|---|
| **GraphQL API** (`schema/`, `resolvers/`) | Schema-first BCF + IFC + diff schema served with Ariadne | The main artefact under study |
| **BCF REST 3.0 API** (`rest/`) | Faithful implementation of the spec's core read endpoints | The comparison baseline |
| **Cross-format queries** | `topicTimeline`, `topicsForElement`, `elementVersionHistory` linking BCF and IFC | Capabilities no single REST request can provide |
| **Late-binding IFC reader** (`ifc_reader.py`) | Reads `.ifc` files at query time via ifcopenshell, no import step | Drop a model in and it is queryable immediately |
| **IFC diff** (`ifc_diff.py`) | Added, deleted, and modified elements between two model versions | Exposed as a GraphQL query |
| **Web viewers** (`static/`) | A BCF topic viewer and an IFC model viewer (click an element to see its issues) | Show the integration working in a browser |
| **Benchmark suite** (`benchmarks/`) | GraphQL-vs-REST scenarios plus Streamlit dashboards | Reproduce the thesis measurements |
| **Sample data** (`exports/`, `ifcs/`) | Example `.bcf` files and `.ifc` models | Run against real data out of the box |
| **Deployment** (`Dockerfile`, `render.yaml`) | Container image and a Render blueprint | Deploy the live demo |

---

## Quick start

> **Prerequisites:** [Python 3.12+](https://www.python.org/) and [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/Bahar-M98/BCF2GraphQL.git
cd BCF2GraphQL
uv sync
```

This repo ships with a ready-to-use public demo database, so you can run the full stack with zero setup. Copy the example env file, which already contains the demo `MONGO_URI`:

```bash
cp .env.example .env          # Windows PowerShell: copy .env.example .env
```

The `.env` then contains:

```bash
MONGO_URI=mongodb+srv://Bahar:TUMCCBEProject@bcf2graphql.iudftom.mongodb.net/?appName=BCF2GraphQL
```

> Want your own data? Point `MONGO_URI` at any MongoDB connection string (for example a free [MongoDB Atlas](https://www.mongodb.com/atlas) cluster) and [import a BCF file](#importing-your-own-bcf-files). The server refuses to start without `MONGO_URI`.

Then start the server:

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open any of these:

| URL | What you get |
|---|---|
| http://localhost:8000/graphql | **GraphQL playground**: explore the schema, run queries live |
| http://localhost:8000/docs | **Swagger UI**: interactive BCF REST API 3.0 docs |
| http://localhost:8000/viewer | **BCF viewer**: browse issues with 3D snapshots (Three.js) |
| http://localhost:8000/ifc-viewer | **IFC viewer**: load a model, click an element, see its issues (web-ifc) |

<sub>Prefer not to use a file? Set the variable in-shell instead: `export MONGO_URI="..."` (bash) or `$env:MONGO_URI="..."` (PowerShell), then run the server.</sub>

---

## The two APIs

### The GraphQL API

Schema-first with [Ariadne](https://ariadnegraphql.org/), split across three SDL files (`schema/bcf.graphql`, `ifc.graphql`, `diff.graphql`). Ask for exactly the fields you need:

<p align="center">
  <img src="assets/graphql-schema.png" alt="The GraphQL type graph: BCF, IFC, and diff types unified into one connected schema" width="720">
  <br>
  <sub>The unified type graph. Explore it interactively in the <a href="https://bcf2graphql.onrender.com/graphql">live playground</a>.</sub>
</p>

> **Live demo vs local.** The hosted demo at [bcf2graphql.onrender.com](https://bcf2graphql.onrender.com/graphql) serves the **BCF dataset only** — the IFC model files are not deployed there, so IFC, cross-format, and diff fields come back `null` or empty. Run the server **locally** (with the files in `ifcs/`) to use the full schema: resolving components to IFC elements, IFC element and version queries, the bidirectional element-to-topics link, version history, and IFC diff.

#### On the live demo (BCF only)

Fetch a topic with its comments and the raw IFC references its viewpoint highlights:

```graphql
query TopicDetail {
  topic(guid: "2405fa74-8c39-42d0-87a5-78cdcbf6c9be") {
    title              # "Change the wall type"
    topicStatus
    priority
    assignedTo
    comments { author comment }
    viewpoints {
      components {
        selection { ifcGuid }   # BCF stores only the GUID; resolve it to an element locally
      }
    }
  }
}
```

Or list every open issue:

```graphql
query OpenIssues {
  topics(topicStatus: "Open") {
    title
    priority
    assignedTo
    comments { author comment }
  }
}
```

#### Locally (adds IFC and diff)

**BCF to IFC** — resolve a topic's components into real IFC elements (`ifcElement` is a custom resolver, not part of the BCF spec), then read each element's material and a full per-element diff across model versions in the same request:

```graphql
query IssueElementDiff {
  topic(guid: "2405fa74-8c39-42d0-87a5-78cdcbf6c9be") {
    viewpoints {
      components {
        selection {
          ifcElement {
            material { name }
            diff {
              globalId
              status
              unchanged

              versionA { fileName exportedAt }
              versionB { fileName exportedAt }

              attributeChanges { attribute oldValue newValue }
              propertyChanges  { pset property oldValue newValue }

              geometryChanged
              placementOld { x y z }
              placementNew { x y z }

              typeChanged      oldType      newType
              containerChanged oldContainer newContainer

              aggregateChanged
              classificationChanged
            }
          }
        }
      }
    }
  }
}
```

**IFC to BCF (bidirectional)** — from an IFC element, find every BCF topic that references it:

```graphql
query ElementIssues {
  topicsForElement(globalId: "38NblWsDL1I8DljLvn67bV") {
    guid
    title
    topicStatus
  }
}
```

**Element history across model versions** — the same wall tracked through every IFC export:

```graphql
query ElementHistory {
  elementVersionHistory(
    globalId: "38NblWsDL1I8DljLvn67bV"
    ifcProjectGuid: "3ZGD7y6S5209$mGLi_sPlj"
  ) {
    version { fileName exportedAt }
    element { name type }
  }
}
```

**IFC diff between two model versions** — what was added, deleted, or modified:

```graphql
query FileDiff {
  ifcFileDiff(
    ifcProjectGuid: "3ZGD7y6S5209$mGLi_sPlj"
    ifcNameA: "BasicModelV1.ifc"
    ifcNameB: "BasicModelV2.ifc"
  ) {
    added    { globalId type name }
    deleted  { globalId type name }
    modified { globalId type name }
  }
}
```

**Query fields at a glance:**

| Domain | Fields | Available on |
|---|---|---|
| **BCF** | `project`, `topics(...)`, `topic(guid)`, `topicHistory(guid)`, `topicEvents(...)`, `commentEvents(...)` | Render + local |
| **IFC** | `ifcElement(...)`, `ifcElements(...)`, `ifcVersions(...)`, `ifcVersionForEvent(...)`, `elementVersionHistory(...)`, `topicsForElement(...)` | local only |
| **Diff** | `ifcFileDiff(...)`, `ifcElementDiff(...)` | local only |
| **Cross-format** | `topicTimeline(topicGuid)` | Render + local (`ifcVersion` stamped locally) |

#### The headline query: `topicTimeline`

This is the query that motivates the whole comparison. It returns a single, unified, chronologically sorted stream that merges topic events and comment events, and stamps every event with the IFC model version that was active on disk at that moment:

```graphql
query Timeline {
  topicTimeline(topicGuid: "2405fa74-8c39-42d0-87a5-78cdcbf6c9be") {
    eventType        # CREATION | COMMENT | MODIFICATION | STATUS_CHANGE
    timestamp { ISO8601 }
    author
    detail
    ifcVersion { version fileName inferred }  # model active at this moment
  }
}
```

Reproducing this over the REST API takes `2 + N` requests (topic events, comment events, then one IFC-version lookup per event). GraphQL does it in one. That gap, measured across realistic data sizes, is the heart of the thesis.

The BCF event stream returns on the live demo too; the `ifcVersion` stamp on each event is only populated when the IFC files are present, so run it locally to see the version linkage.

<p align="center">
  <img src="assets/topic-timeline.png" alt="topicTimeline merges the BCF issue timeline and the IFC model timeline through the GraphQL resolver layer into one version-stamped event stream" width="860">
  <br>
  <sub>The resolver layer merges the BCF issue timeline and the IFC model timeline into one version-stamped stream, resolved in a single request.</sub>
</p>

### The BCF REST API

A faithful implementation of the [buildingSMART BCF REST API 3.0](https://github.com/buildingSMART/BCF-API/tree/release_3_0) spec, mounted under `/bcf/3.0`, with full Swagger docs at `/docs` and OData `$filter` support on topic queries.

```bash
curl https://bcf2graphql.onrender.com/bcf/3.0/projects
curl "https://bcf2graphql.onrender.com/bcf/3.0/projects/{id}/topics?\$filter=topic_status eq 'Open'"
```

<details>
<summary><b>All BCF 3.0 endpoints</b> (click to expand)</summary>

```
GET /bcf/3.0/projects
GET /bcf/3.0/projects/{project_id}
GET /bcf/3.0/projects/{project_id}/topics
GET /bcf/3.0/projects/{project_id}/topics/events
GET /bcf/3.0/projects/{project_id}/topics/comments/events
GET /bcf/3.0/projects/{project_id}/topics/{guid}
GET /bcf/3.0/projects/{project_id}/topics/{guid}/files
GET /bcf/3.0/projects/{project_id}/topics/{guid}/comments
GET /bcf/3.0/projects/{project_id}/topics/{guid}/comments/{cguid}
GET /bcf/3.0/projects/{project_id}/topics/{guid}/comments/{cguid}/events
GET /bcf/3.0/projects/{project_id}/topics/{guid}/events
GET /bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints
GET /bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vguid}
GET /bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/selection
GET /bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/coloring
GET /bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/visibility
GET /bcf/3.0/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/bitmaps
```

</details>

---

## Interactive viewers

Two browser-based viewers ship with the server and consume the GraphQL API directly:

- **`/viewer`**: browse BCF topics with their viewpoint snapshots and metadata, rendered with [Three.js](https://threejs.org/).
- **`/ifc-viewer`**: load an IFC model in the browser with [web-ifc](https://github.com/ThatOpen/engine_web-ifc), click any element, and instantly see every BCF issue attached to it (via `topicsForElement`).

---

## Architecture

<p align="center">
  <img src="assets/BCF2Graphql.png" alt="System architecture: BCF and IFC inputs flow through the GraphQL API, IFC extension, and integration layer into the web application and benchmark suite" width="640">
  <br>
  <sub>System overview, from BCF/IFC inputs through the API layers to the web application and benchmark suite.</sub>
</p>


Inside the GraphQL layer, each root field resolves through its own chain before Ariadne assembles the response:

<p align="center">
  <img src="assets/resolver-chain.png" alt="Flowchart of how each GraphQL root field is resolved before Ariadne assembles the response" width="760">
  <br>
  <sub>How a GraphQL query is resolved, by root field.</sub>
</p>

**Two design decisions worth knowing:**

1. **IFC data is never imported into MongoDB.** The `.ifc` files in `ifcs/` are opened on demand by `ifc_reader.py` via [`ifcopenshell`](https://ifcopenshell.org/). Drop a new file in and it is live immediately, with no import step. The trade-off is higher per-query latency, which is itself part of what the benchmarks measure.

2. **4-tier IFC version matching.** Linking a BCF event to the right model version falls back through: (1) project GUID plus filename, (2) project GUID, (3) filename, (4) latest version before the event timestamp (flagged `inferred: true`). This logic lives in `ifc_reader.py` and is mirrored client-side in `static/viewer.js`.

<details>
<summary><b>Full project layout</b> (click to expand)</summary>

```
BCF2GraphQL/
├── main.py              # FastAPI entry point, mounts GraphQL + REST + static
├── bcf_parser.py        # Parses .bcf ZIP files into Python dicts
├── ifc_reader.py        # Reads .ifc files via ifcopenshell (never imports to DB)
├── ifc_diff.py          # Element-level diffs between two IFC versions
├── import_bcf.py        # CLI: uv run python import_bcf.py <file.bcf>
│
├── schema/              # GraphQL SDL (load order matters, see main.py)
│   ├── bcf.graphql      #   base Query type + all BCF types
│   ├── ifc.graphql      #   extend Query with IFC queries/types
│   └── diff.graphql     #   extend Query with diff queries/types
│
├── resolvers/           # Ariadne resolvers (bcf, ifc, history, diff)
├── rest/                # BCF REST 3.0 router + OData $filter parser
├── db/database.py       # MongoDB connection + async helpers
├── static/              # Three.js / web-ifc browser viewers
├── benchmarks/          # GraphQL-vs-REST benchmarks + Streamlit dashboards
│
├── ifcs/                # IFC model files (read at query time)
├── exports/             # Sample .bcf files for import
├── results/             # Benchmark CSV outputs
└── locust_results/      # Load-test CSVs
```

</details>

---

## Importing your own BCF files

```bash
uv run python import_bcf.py exports/TestTopicsV1.bcf
```

Re-importing the same project creates a new version snapshot, which is what powers `topicHistory` and the version-stamped `topicTimeline`. Sample `.bcf` files live in `exports/`.

---

## Benchmarks

The benchmark suite drives both APIs through equivalent workloads and writes CSVs to `results/`, with Streamlit dashboards to visualise the GraphQL-vs-REST gap.

```bash
# 1. Seed synthetic BCF data into MongoDB
uv run python benchmarks/generate_benchmark_data.py

# 2. Run the GraphQL vs REST benchmark (writes results/*.csv)
uv run python benchmarks/benchmark.py
uv run python benchmarks/benchmark.py --label render --url https://bcf2graphql.onrender.com

# 3. Explore the results
uv run streamlit run benchmarks/dashboard.py            --server.port 8501
uv run streamlit run benchmarks/comparison_dashboard.py --server.port 8502   # local vs Render
```

**Scaling / load test** with [Locust](https://locust.io/):

```bash
python benchmarks/locust_scaling.py --host https://bcf2graphql.onrender.com
uv run streamlit run benchmarks/locust_scaling_dashboard.py --server.port 8503
```

---

## Deployment

The repo includes a [`render.yaml`](render.yaml) and a [`Dockerfile`](Dockerfile) for one-click deployment to [Render](https://render.com/) (the live demo runs there). Set `MONGO_URI` as a secret in the Render dashboard; it is intentionally not committed to the blueprint.

```bash
docker build -t bcf2graphql .
docker run -p 8000:8000 -e MONGO_URI="<your-uri>" bcf2graphql
```

---

## Tech stack

**Backend:** Python 3.12, FastAPI, Ariadne (GraphQL), Motor (async MongoDB), ifcopenshell, uv
**Frontend viewers:** Three.js, web-ifc
**Data:** MongoDB Atlas (BCF), `.ifc` files on disk (IFC)
**Benchmarking:** httpx, Locust, Streamlit, Plotly, Matplotlib

---

## Citation

This software was built for a Master's thesis at the Technical University of Munich.

---

## License

Released under the [MIT License](LICENSE). Copyright 2026 Bahar Moradi.

> Contributor and architecture notes for AI assistants live in [`AGENTS.md`](AGENTS.md).
