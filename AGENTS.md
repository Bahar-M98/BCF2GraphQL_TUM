# AGENTS.md

This file provides context for AI coding assistants working in this repository.

## What this project is

BCF2GraphQL is a research server built for a Master's thesis at TU Munich. It exposes the same BIM (Building Information Modelling) data through two APIs running side by side in the same process:

- **GraphQL API** — schema-first, built with Ariadne + FastAPI
- **BCF REST API** — follows the buildingSMART BCF REST API 3.0 specification

The goal is to benchmark the two approaches empirically for querying linked BCF/IFC data.

**BCF** (BIM Collaboration Format) — issue-tracking format for BIM projects, stored in MongoDB.  
**IFC** (Industry Foundation Classes) — 3D building model format, read directly from `.ifc` files on disk.

---

## Required environment variable

```
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?appName=BCF2GraphQL
```

Set it in a `.env` file at the project root or export it in your shell. The app will refuse to start without it. Never hardcode credentials in source files.

---

## Project structure

```
BCF2GraphQL/
├── main.py                  # FastAPI entry point — mounts GraphQL + REST + static
├── bcf_parser.py            # Parses .bcf ZIP files into Python dicts
├── ifc_reader.py            # Reads .ifc files via ifcopenshell (never imports to DB)
├── ifc_diff.py              # Computes element-level diffs between two IFC versions
├── import_bcf.py            # CLI script: python import_bcf.py <file.bcf>
│
├── schema/                  # GraphQL SDL files (load order matters — see main.py)
│   ├── bcf.graphql          # Base Query type + all BCF types
│   ├── ifc.graphql          # extend type Query — IFC queries and types
│   └── diff.graphql         # extend type Query — diff queries and types
│
├── resolvers/               # Ariadne resolver functions
│   ├── __init__.py          # Wires all resolvers to QueryType / ObjectType
│   ├── query.py             # BCF resolvers (topics, comments, events)
│   ├── ifc_resolvers.py     # IFC resolvers (elements, geometry, versions)
│   ├── history_resolvers.py # Timeline, element history, topics-for-element
│   └── diff_resolvers.py    # IFC file and element diff resolvers
│
├── db/
│   └── database.py          # MongoDB connection + all async DB helpers
│
├── rest/
│   ├── __init__.py          # Exports the FastAPI router
│   ├── bcf.py               # BCF REST API 3.0 endpoints
│   └── odata_filter.py      # Parses OData $filter expressions for REST queries
│
├── static/                  # HTML viewers served at /viewer and /ifc-viewer
│   ├── viewer.html          # BCF topic viewer (Three.js 3D + GraphQL)
│   ├── viewer.css
│   ├── viewer.js
│   ├── ifc-viewer.html      # IFC file viewer (web-ifc + click → BCF topics)
│   ├── ifc-viewer.css
│   └── ifc-viewer.js
│
├── benchmarks/              # All benchmark and analysis scripts
│   ├── benchmark.py         # Main benchmark (GraphQL vs REST, writes to results/)
│   ├── generate_benchmark_data.py  # Seeds synthetic BCF data into MongoDB
│   ├── dashboard.py         # Streamlit dashboard for benchmark results
│   ├── comparison_dashboard.py     # Streamlit: compare local vs Render results
│   ├── locust_scaling.py           # Locust load test (1/5/10 users)
│   ├── locust_scaling_dashboard.py # Streamlit dashboard for locust results
│   ├── flat_scaling_dashboard.py   # Streamlit dashboard for flat scaling runs
│   └── make_env_analyses.py        # Generates text analysis reports
│
├── ifcs/                    # IFC model files (dropped here to take effect immediately)
├── exports/                 # Sample .bcf files for import
├── results/                 # Benchmark CSV outputs (benchmark_results_*.csv)
└── locust_results/          # Locust scaling CSVs (one subfolder per experiment)
```

---

## Architecture decisions to know

### IFC data is never imported into MongoDB
IFC files in `ifcs/` are opened at query time by `ifc_reader.py` using `ifcopenshell`. This means dropping a new `.ifc` file into `ifcs/` takes effect immediately with no import step. The trade-off is higher per-query latency. Do not change this to a database-backed approach without a clear reason.

### Schema load order matters
In `main.py`, `schema/bcf.graphql` must be first because it defines `type Query`. The other two files use `extend type Query`. Ariadne merges SDL in list order.

### GraphQL schema extension is split across three files
- `schema/bcf.graphql` — base `Query` type and all BCF types
- `schema/ifc.graphql` — IFC types, `extend type Query` with IFC fields
- `schema/diff.graphql` — diff types, `extend type Query` with diff fields

### N+1 warning on `Component.ifcElement`
The resolver in `resolvers/ifc_resolvers.py` is called once per component and scans all IFC files each time. This is a known limitation. Do not add queries that request `ifcElement` on large result sets without implementing a DataLoader first.

### 3-tier IFC version matching
When matching a BCF event to an IFC file version, the system uses a 4-tier fallback:
1. Exact match: IFC project GUID + filename
2. Project GUID only
3. Filename only (case-insensitive basename)
4. Global fallback: latest version before the event timestamp (flagged `inferred: true`)

This logic lives in `ifc_reader.py` and is mirrored in the client-side JavaScript in `static/viewer.js`.

---

## How to run

**Install dependencies:**
```bash
uv sync
```

**Start the server:**
```bash
export MONGO_URI="mongodb+srv://..."
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

**Import a BCF file:**
```bash
uv run python import_bcf.py exports/TestTopicsV1.bcf
```

**Endpoints:**
- `GET  /viewer` — BCF element viewer (Three.js)
- `GET  /ifc-viewer` — IFC file viewer
- `POST /graphql` — GraphQL API
- `GET  /graphql` — Ariadne playground
- `GET  /docs` — Swagger UI for the REST API

---

## Benchmarks

Seed data, then run:
```bash
uv run python benchmarks/generate_benchmark_data.py
uv run python benchmarks/benchmark.py                        # writes to results/
uv run streamlit run benchmarks/dashboard.py                 # view results
uv run streamlit run benchmarks/comparison_dashboard.py      # local vs Render
uv run python benchmarks/locust_scaling.py --host <url>      # load test
uv run streamlit run benchmarks/locust_scaling_dashboard.py  # view load results
```

---

## Coding conventions

- **Python filenames**: snake_case (`bcf_parser.py`, `ifc_reader.py`)
- **GraphQL schema files**: lowercase `.graphql` extension, inside `schema/`
- **HTML viewers**: split into `.html` / `.css` / `.js` inside `static/`
- **Benchmark outputs**: CSV files go in `results/`, locust results in `locust_results/`
- **No hardcoded credentials**: always use `MONGO_URI` from environment
- **No comments explaining what code does**: only add comments for non-obvious *why*
