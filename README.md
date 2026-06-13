<div align="center">

# 🏗️ BCF2GraphQL

### One BIM dataset. Two APIs. Side by side.

**A research server that serves the same Building Information Modelling data through a [GraphQL API](#-the-graphql-api) and the official [buildingSMART BCF REST API 3.0](#-the-bcf-rest-api) — built to benchmark which one queries linked BCF/IFC data more efficiently.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![GraphQL](https://img.shields.io/badge/GraphQL-Ariadne-E10098?logo=graphql&logoColor=white)](https://ariadnegraphql.org/)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-47A248?logo=mongodb&logoColor=white)](https://www.mongodb.com/atlas)
[![BCF 3.0](https://img.shields.io/badge/buildingSMART-BCF%203.0-005A9C)](https://github.com/buildingSMART/BCF-API/tree/release_3_0)

[**Live demo**](https://bcf2graphql.onrender.com/graphql) · [**Quick start**](#-quick-start) · [**The two APIs**](#-the-two-apis) · [**Benchmarks**](#-benchmarks) · [**Architecture**](#-architecture)

</div>

---

## ✨ What is this?

Construction projects track issues — clashes, defects, open questions — in **BCF** (BIM Collaboration Format) files, while the building itself lives in **IFC** (Industry Foundation Classes) 3D models. The two are deeply linked: a BCF issue points at specific IFC elements, and as the model evolves through versions, those links have to be resolved against *whichever version of the model existed when the issue was raised*.

Answering a question like *"show me the full timeline of this issue, with the model version active at each step"* is awkward over a classic REST API — it takes many round trips. **BCF2GraphQL exposes the exact same data two ways so the cost of each can be measured empirically**, which is the core contribution of the [Master's thesis](#-citation) it was built for at the Technical University of Munich.

| | GraphQL API | BCF REST API 3.0 |
|---|---|---|
| **Style** | Schema-first, client picks fields | Spec-compliant, fixed resources |
| **Built with** | Ariadne + FastAPI | FastAPI |
| **Linked BCF↔IFC queries** | One request | `2 + N` requests |
| **Endpoint** | `POST /graphql` | `GET /bcf/3.0/...` |
| **Standard** | Custom (BCF-aligned) | [buildingSMART BCF 3.0](https://github.com/buildingSMART/BCF-API/tree/release_3_0) |

Both run **in the same process, on the same port, over the same MongoDB + IFC data** — so any performance difference is the API model, not the storage.

---

## 🚀 Quick start

> **Prerequisites:** [Python 3.12+](https://www.python.org/) and [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/Bahar-M98/BCF2GraphQL.git
cd BCF2GraphQL
uv sync
```

This repo ships with a **ready-to-use public demo database**, so you can run the full stack with zero setup. Create a `.env` file in the project root:

```bash
MONGO_URI=mongodb+srv://Bahar:TUMCCBEProject@bcf2graphql.iudftom.mongodb.net/?appName=BCF2GraphQL
```

> 💡 Want your own data? Point `MONGO_URI` at any MongoDB connection string (e.g. a free [MongoDB Atlas](https://www.mongodb.com/atlas) cluster) and [import a BCF file](#-importing-your-own-bcf-files). The server refuses to start without `MONGO_URI`.

Then start the server:

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open any of these:

| URL | What you get |
|---|---|
| http://localhost:8000/graphql | 🎮 **GraphQL playground** — explore the schema, run queries live |
| http://localhost:8000/docs | 📘 **Swagger UI** — interactive BCF REST API 3.0 docs |
| http://localhost:8000/viewer | 🧊 **BCF viewer** — browse issues with 3D snapshots (Three.js) |
| http://localhost:8000/ifc-viewer | 🏢 **IFC viewer** — load a model, click an element → see its issues (web-ifc) |

<sub>Windows PowerShell: use `copy .env.example .env` and `$env:MONGO_URI="..."` if you prefer exporting in-shell.</sub>

---

## 🔌 The two APIs

### 🟣 The GraphQL API

Schema-first with [Ariadne](https://ariadnegraphql.org/), split across three SDL files (`schema/bcf.graphql`, `ifc.graphql`, `diff.graphql`). Ask for exactly the fields you need:

```graphql
# Every open issue with its comments and the IFC elements it points at —
# one request, exactly the fields you want.
query OpenIssues {
  topics(topicStatus: "Open") {
    title
    priority
    assignedTo
    comments { author comment }
    viewpoints {
      components {
        selection {
          ifcGuid
          ifcElement { name type storey }   # resolved across IFC versions
        }
      }
    }
  }
}
```

**Query fields at a glance:**

| Domain | Fields |
|---|---|
| **BCF** | `project`, `topics(...)`, `topic(guid)`, `topicHistory(guid)`, `topicEvents(...)`, `commentEvents(...)` |
| **IFC** | `ifcElement(...)`, `ifcElements(...)`, `ifcVersions(...)`, `ifcVersionForEvent(...)`, `elementVersionHistory(...)`, `topicsForElement(...)` |
| **Cross-cutting** | `topicTimeline(topicGuid)` ⭐, `diff(ifcNameA, ifcNameB)` |

#### ⭐ The headline query: `topicTimeline`

This is the query that motivates the whole comparison. It returns a **single, unified, chronologically-sorted stream** that merges topic events *and* comment events, and **stamps every event with the IFC model version that was active on disk at that moment**:

```graphql
query Timeline {
  topicTimeline(topicGuid: "3a8b...") {
    eventType        # CREATION | COMMENT | MODIFICATION | STATUS_CHANGE
    timestamp { ISO8601 }
    author
    detail
    ifcVersion { version fileName inferred }  # model active at this moment
  }
}
```

Reproducing this over the REST API takes **`2 + N` requests** (topic events, comment events, then one IFC-version lookup per event). GraphQL does it in **one**. That gap, measured across realistic data sizes, is the heart of the thesis.

### 🟢 The BCF REST API

A faithful implementation of the **[buildingSMART BCF REST API 3.0](https://github.com/buildingSMART/BCF-API/tree/release_3_0)** spec, mounted under `/bcf/3.0`, with full Swagger docs at `/docs` and OData `$filter` support on topic queries.

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

## 🧊 Interactive viewers

Two browser-based viewers ship with the server and consume the GraphQL API directly:

- **`/viewer`** — browse BCF topics with their viewpoint snapshots and metadata, rendered with [Three.js](https://threejs.org/).
- **`/ifc-viewer`** — load an IFC model in the browser with [web-ifc](https://github.com/ThatOpen/engine_web-ifc), **click any element**, and instantly see every BCF issue attached to it (via `topicsForElement`).

---

## 🧠 Architecture

```mermaid
flowchart TD
    subgraph Clients
        PG[GraphQL Playground]
        SW[Swagger UI]
        V[Three.js / web-ifc viewers]
    end

    subgraph "FastAPI app · main.py · one process, one port"
        GQL["/graphql<br/>Ariadne schema-first"]
        REST["/bcf/3.0/*<br/>buildingSMART BCF REST 3.0"]
        STATIC["/viewer · /ifc-viewer"]
    end

    subgraph Data
        MONGO[(MongoDB<br/>BCF topics, comments, events)]
        IFC["ifcs/*.ifc<br/>read at query time<br/>via ifcopenshell"]
    end

    PG --> GQL
    V --> GQL
    SW --> REST
    GQL --> MONGO & IFC
    REST --> MONGO
    STATIC --> V
```

**Two design decisions worth knowing:**

1. **IFC data is never imported into MongoDB.** `.ifc` files in `ifcs/` are opened on demand by `ifc_reader.py` via [`ifcopenshell`](https://ifcopenshell.org/). Drop a new file in and it's live immediately — no import step. The trade-off is higher per-query latency, which is itself part of what the benchmarks measure.

2. **4-tier IFC version matching.** Linking a BCF event to the right model version falls back through: ① project GUID + filename → ② project GUID → ③ filename → ④ latest version before the event timestamp (flagged `inferred: true`). This logic lives in `ifc_reader.py` and is mirrored client-side in `static/viewer.js`.

<details>
<summary><b>Full project layout</b> (click to expand)</summary>

```
BCF2GraphQL/
├── main.py              # FastAPI entry point — mounts GraphQL + REST + static
├── bcf_parser.py        # Parses .bcf ZIP files into Python dicts
├── ifc_reader.py        # Reads .ifc files via ifcopenshell (never imports to DB)
├── ifc_diff.py          # Element-level diffs between two IFC versions
├── import_bcf.py        # CLI: uv run python import_bcf.py <file.bcf>
│
├── schema/              # GraphQL SDL (load order matters — see main.py)
│   ├── bcf.graphql      #   base Query type + all BCF types
│   ├── ifc.graphql      #   extend Query — IFC queries/types
│   └── diff.graphql     #   extend Query — diff queries/types
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

## 📥 Importing your own BCF files

```bash
uv run python import_bcf.py exports/TestTopicsV1.bcf
```

Re-importing the same project creates a new **version snapshot**, which is what powers `topicHistory` and the version-stamped `topicTimeline`. Sample `.bcf` files live in `exports/`.

---

## 📊 Benchmarks

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

## ☁️ Deployment

The repo includes a [`render.yaml`](render.yaml) and a [`Dockerfile`](Dockerfile) for one-click deployment to [Render](https://render.com/) (the live demo runs there). Set `MONGO_URI` as a secret in the Render dashboard — it is intentionally **not** committed to the blueprint.

```bash
docker build -t bcf2graphql .
docker run -p 8000:8000 -e MONGO_URI="<your-uri>" bcf2graphql
```

---

## 🛠️ Tech stack

**Backend:** Python 3.12 · FastAPI · Ariadne (GraphQL) · Motor (async MongoDB) · ifcopenshell · uv
**Frontend viewers:** Three.js · web-ifc
**Data:** MongoDB Atlas (BCF) · `.ifc` files on disk (IFC)
**Benchmarking:** httpx · Locust · Streamlit · Plotly · Matplotlib

---

## 📚 Citation

This software was built for a Master's thesis at the Technical University of Munich. If you use it in academic work, please cite it — see [`CITATION.cff`](CITATION.cff).

```bibtex
@software{moradi_bcf2graphql,
  author  = {Moradi, Bahar},
  title   = {BCF2GraphQL: A GraphQL and BCF REST API for benchmarking BIM data access},
  year    = {2026},
  url      = {https://github.com/Bahar-M98/BCF2GraphQL},
  note    = {Master's thesis, Technical University of Munich}
}
```

---

## 📄 License

Released under the [MIT License](LICENSE). © 2026 Bahar Moradi.

> Contributor and architecture notes for AI assistants live in [`AGENTS.md`](AGENTS.md).
