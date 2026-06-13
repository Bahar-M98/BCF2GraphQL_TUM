"""
ASGI application entry point — serves GraphQL and BCF REST APIs on the same port.

  POST /graphql        GraphQL API (Ariadne schema-first)
  GET  /graphql        Ariadne interactive playground
  GET  /bcf/3.0/...    BCF REST API 3.0 (buildingSMART spec)
  GET  /viewer         BCF element viewer
  GET  /ifc-viewer     IFC file viewer
  GET  /docs           Swagger UI

Schema load order matters: schema/bcf.graphql defines the base Query type;
ifc.graphql and diff.graphql extend it. Ariadne merges SDL in list order.
"""

import logging

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")

from pathlib import Path

from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from resolvers import query, camera_type, ifc_element_type, ifc_mesh_type, component_type, comment_type
from rest import router as rest_router

base = Path(__file__).parent

type_defs = [
    (base / "schema/bcf.graphql").read_text(encoding="utf-8"),
    (base / "schema/ifc.graphql").read_text(encoding="utf-8"),
    (base / "schema/diff.graphql").read_text(encoding="utf-8"),
]

schema = make_executable_schema(type_defs, query, camera_type, ifc_element_type, ifc_mesh_type, component_type, comment_type)
graphql_app = GraphQL(schema, debug=True)

app = FastAPI(
    title="BCF REST & GraphQL API",
    description=(
        "This server was built to benchmark GraphQL vs the official buildingSMART BCF REST API 3.0 for BIM data exchange. "
    ),
    version="1.0.0",
)

@app.get("/viewer", include_in_schema=False)
async def serve_viewer():
    return FileResponse(base / "static/viewer.html")

@app.get("/ifc-viewer", include_in_schema=False)
async def serve_ifc_viewer():
    return FileResponse(base / "static/ifc-viewer.html")

app.include_router(rest_router)

# /static must be mounted before /graphql so static requests don't fall through to Ariadne.
app.mount("/static", StaticFiles(directory=base / "static"), name="static")
app.mount("/graphql", graphql_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
