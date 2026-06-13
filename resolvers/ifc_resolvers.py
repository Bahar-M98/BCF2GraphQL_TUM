"""
resolvers/ifc_resolvers.py

IFC resolvers — read directly from .ifc files on disk, never from MongoDB.

Design decision: IFC data is NOT imported into a database.  Instead every
resolver calls ifcReader functions that scan the ifcs/ directory at query time.
This avoids a costly import step and lets new IFC versions take effect the
moment the file is dropped into ifcs/ — critical for a thesis workflow where
model files change frequently.  The trade-off is higher per-query latency for
large IFC files; acceptable for the dataset sizes this project targets.

Resolver hooks:
  Query.ifcElement         — look up one element by globalId (top-level query)
                             accepts optional ifcProjectGuid / fileName selectors
                             to implement BCF→IFC precision levels 2 and 3
  Query.ifcElements        — list elements with optional type/storey filter
  Query.ifcElementHistory  — all IFC-file versions of one element
  Component.ifcElement     — BCF → IFC forward link
  IfcElement.topics        — IFC → BCF reverse link (reads MongoDB for BCF data)
"""

import asyncio
import logging

from ifc_reader import (
    list_ifc_files,
    list_versions,
    find_ifc_file,
    extract_elements_by_guids,
    extract_all_elements,
    get_element_geometry,
    mesh_to_glb,
)
from db.database import get_topics_for_ifc_element

logger = logging.getLogger(__name__)


# ─── Top-level query: ifcElement(globalId, version, ifcProjectGuid, fileName) ─
#
# Three selector modes, checked in this priority order:
#
#   1. ifcProjectGuid and/or fileName
#      Locates the IFC file by project GUID and/or filename using list_versions().
#      This is the "precision level 2/3" path from the BCF↔IFC concept:
#        - project GUID only  → first file for that project containing the element
#        - project GUID + fileName → that exact file, then extract element
#      Not using list_ifc_files() here because that only gives integer version
#      numbers; list_versions() gives the full metadata (ifcProjectGuid, fileName)
#      needed to match by those selectors.
#
#   2. version (Int)
#      Select by integer version index (1 = oldest).  Kept for compatibility
#      with clients that already know the version number.
#
#   3. none — latest version containing the element
#      Scans all files oldest→newest, overwrites on each match so the last
#      assignment wins (latest file containing the element).  An element may
#      not exist in all versions (e.g. added in v2), so we cannot just read
#      only the newest file.

async def resolve_ifc_element(
    obj, info,
    globalId: str,
    version: int = None,
    ifcProjectGuid: str = None,
    fileName: str = None,
):
    # Mode 1: project GUID and/or filename selectors
    if ifcProjectGuid is not None or fileName is not None:
        # list_versions() opens every IFC file to read IfcProject.GlobalId;
        # filter by ifcProjectGuid up front to avoid opening unrelated files.
        all_versions = list_versions(ifc_project_guid=ifcProjectGuid)
        for ifc_ver in all_versions:
            if fileName and ifc_ver.get("fileName") != fileName:
                continue
            file_path = ifc_ver.get("filePath")
            if not file_path:
                continue
            results = extract_elements_by_guids(file_path, [globalId])
            el = results.get(globalId)
            if el:
                # Stamp with the integer version number for schema consistency.
                # Cross-reference with list_ifc_files() which owns the int index.
                for v, fp in list_ifc_files():
                    if fp == file_path:
                        el["version"] = v
                        break
                return el
        return None

    # Mode 2: integer version index
    versions_list = list_ifc_files()
    if not versions_list:
        return None

    if version is not None:
        # Use the specific versioned IFC file
        for v, filepath in versions_list:
            if v == version:
                results = extract_elements_by_guids(filepath, [globalId])
                el = results.get(globalId)
                if el:
                    el["version"] = v
                return el
        return None

    # Mode 3: no selector — scan all files, return element from the latest
    # file that contains it (so history changes are visible on re-import).
    # list_ifc_files() returns files sorted oldest-first, so the last
    # assignment wins, giving us the element from the newest version.
    found = None
    for v, filepath in versions_list:
        results = extract_elements_by_guids(filepath, [globalId])
        el = results.get(globalId)
        if el:
            el["version"] = v
            found = el  # overwrite → last wins = latest version
    return found


# ─── Top-level query: ifcElements(type, version, storey) ─────────────────────
# Lists elements with optional filters.
# - version given  → only that IFC file
# - version omitted → all IFC files combined (each element stamped with its version)

async def resolve_ifc_elements(obj, info, type: str = None, version: int = None, storey: str = None):
    versions_list = list_ifc_files()
    if not versions_list:
        return []

    if version is not None:
        for v, filepath in versions_list:
            if v == version:
                return extract_all_elements(filepath, type_filter=type, storey_filter=storey, version=v)
        return []

    # No version filter — scan every IFC file and combine results
    all_elements = []
    for v, filepath in versions_list:
        all_elements.extend(
            extract_all_elements(filepath, type_filter=type, storey_filter=storey, version=v)
        )
    return all_elements


# ─── Field resolver: Component.ifcElement ────────────────────────────────────
# BCF → IFC forward link.
# Scans every IFC file in ifcs/ for the component's ifcGuid and returns
# the first match found.
#
# Why not use Component.originatingSystem to pick the right file?
# originatingSystem is a free-text field set by the authoring tool; it often
# contains a product name ("Revit 2024") rather than a filename, and even when
# it does contain a filename it may not match what is actually on disk (e.g.
# the file was renamed after export).  Scanning all files by GlobalId is
# slower but robust to both of these cases.
#
# SERVER-SIDE N+1 WARNING:
# This resolver is invoked once per Component object returned by the parent
# query.  A query like:
#   topics { viewpoints { components { selection { ifcElement { ... } } } } }
# fires this function N_topics × N_viewpoints × N_components times — each
# invocation scanning all IFC files on disk.  With 500 topics × 1 viewpoint
# × 3 elements avg = ~1500 sequential file scans.
# The benchmark deliberately does NOT request ifcElement so this cost is not
# measured.  Any future scenario requesting ifcElement must account for this
# overhead, which would show GraphQL with a server-side N+1 disadvantage.
# The correct fix is a DataLoader that batches all GUIDs into a single
# multi-GUID scan per IFC file per resolver batch.

async def resolve_component_ifc_element(component, info):
    guid = component.get("ifcGuid")
    if not guid:
        return None

    for v, filepath in list_ifc_files():
        results = extract_elements_by_guids(filepath, [guid])
        el = results.get(guid)
        if el:
            el["version"] = v
            return el

    logger.debug("GUID %s not found in any IFC file", guid)
    return None


# ─── Field resolver: IfcElement.geometry ─────────────────────────────────────
# Returns tessellated mesh for the element in world coordinates.
# Lazy: only computed when the client explicitly requests the geometry field.
# Uses asyncio.to_thread because ifcopenshell.geom.create_shape is CPU-bound.

async def resolve_ifc_element_geometry(element, info):
    global_id = element.get("globalId")
    file_name  = element.get("fileName")
    if not global_id or not file_name:
        return None

    file_path = find_ifc_file(file_name)
    if not file_path:
        return None

    return await asyncio.to_thread(get_element_geometry, file_path, global_id)


# ─── Field resolver: IfcMesh.glb ─────────────────────────────────────────────
# Converts the already-tessellated mesh dict into a base64 GLB binary.
# Only runs when the client explicitly requests the glb field.

async def resolve_ifc_mesh_glb(ifc_mesh, info):
    vertices = ifc_mesh.get("vertices") or []
    faces    = ifc_mesh.get("faces") or []
    normals  = ifc_mesh.get("normals") or []
    if not vertices or not faces:
        return None
    return await asyncio.to_thread(mesh_to_glb, vertices, faces, normals)


# ─── Field resolver: IfcElement.topics ───────────────────────────────────────
# IFC → BCF reverse link.
# When you have an IFC element and want to know which BCF topics reference it.
# BCF topics are stored in MongoDB, so we query Atlas here.

async def resolve_ifc_element_topics(element, info):
    # element dict uses "globalId" (matches GraphQL schema field name)
    global_id = element.get("globalId")
    if not global_id:
        return []
    return await get_topics_for_ifc_element(global_id)
