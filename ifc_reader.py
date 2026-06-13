"""
IFC4 element extraction utilities.

Opens IFC files via ifcopenshell and extracts element data (type, name, storey,
property sets, materials, volume). Results go directly to resolvers; nothing is
stored in MongoDB. Dict keys from get_element_data() match GraphQL schema field names.
"""

import logging
import os
import re
from datetime import datetime, timezone

import base64

import numpy as np
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
from pygltflib import (
    GLTF2, Scene, Node, Mesh, Primitive, Accessor, BufferView, Buffer,
    ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER, FLOAT, UNSIGNED_INT,
)

logger = logging.getLogger(__name__)


# ── IFC STEP header ───────────────────────────────────────────────────────────

def _read_ifc_exported_at(filepath: str) -> str | None:
    """
    Read the exported timestamp from the FILE_NAME line in an IFC STEP header.

    The header looks like:
        FILE_NAME('BasicModel4V1.ifc','2026-03-23T11:08:11+01:00',...);

    Only the first ~10 lines are read (before DATA;) so this is fast.
    Returns None if the timestamp cannot be found.
    """
    try:
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("FILE_NAME("):
                    parts = re.findall(r"'([^']*)'", stripped)
                    return parts[1] if len(parts) > 1 else None
                if stripped == "DATA;":
                    break
    except Exception:
        pass
    return None


# ── Storey ────────────────────────────────────────────────────────────────────

def _get_storey(element) -> dict | None:
    """Walk up the spatial containment tree to find the IfcBuildingStorey."""
    container = ifcopenshell.util.element.get_container(element)
    while container:
        if container.is_a("IfcBuildingStorey"):
            elevation = None
            try:
                elevation = float(container.Elevation) if container.Elevation is not None else None
            except (TypeError, ValueError):
                pass
            return {"name": container.Name, "elevation": elevation}
        container = ifcopenshell.util.element.get_container(container)
    return None


# ── Owner history ─────────────────────────────────────────────────────────────

def _get_owner_history(element) -> dict | None:
    """Extract OwnerHistory as a GraphQL-compatible dict."""
    oh = getattr(element, "OwnerHistory", None)
    if oh is None:
        return None

    user = None
    app = None

    try:
        if oh.OwningUser:
            person = oh.OwningUser.ThePerson
            if person:
                parts = [getattr(person, "GivenName", None), getattr(person, "FamilyName", None)]
                user = " ".join(p for p in parts if p) or None
    except Exception:
        pass

    try:
        if oh.OwningApplication:
            app = getattr(oh.OwningApplication, "ApplicationFullName", None)
    except Exception:
        pass

    change_action = getattr(oh, "ChangeAction", None)

    return {
        "creationDate":      getattr(oh, "CreationDate", None),
        "lastModifiedDate":  getattr(oh, "LastModifiedDate", None),
        "changeAction":      str(change_action) if change_action is not None else None,
        "owningUser":        user,
        "owningApplication": app,
    }


# ── Volume ────────────────────────────────────────────────────────────────────

def _get_volume(element) -> float | None:
    """
    Extract net volume from quantity sets.
    Looks in any Qto_* set for a property whose name contains 'Volume'.
    """
    try:
        psets = ifcopenshell.util.element.get_psets(element)
        for pset_name, props in psets.items():
            if not (pset_name.startswith("Qto_") or "BaseQuantities" in pset_name):
                continue
            for key, val in props.items():
                if "volume" in key.lower() and val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return None


# ── Material ──────────────────────────────────────────────────────────────────

def _layers_from_layer_set(layer_set) -> list:
    """Build [IfcMaterialLayer] list from an IfcMaterialLayerSet entity."""
    layers = getattr(layer_set, "MaterialLayers", []) or []
    result = []
    for layer in layers:
        mat = layer.Material if layer.Material else None
        result.append({
            "name": getattr(layer, "Name", None) or (mat.Name if mat else None),
            "material": {
                "name":     mat.Name if mat else None,
                "category": getattr(layer, "Category", None),
            } if mat else None,
            "thickness": getattr(layer, "LayerThickness", None),
        })
    return result


def _get_material_layers(element) -> list | None:
    """
    Return material as [IfcMaterialLayer] matching the GraphQL schema.
    Handles all common IFC4 material association types.
    """
    mat_obj = ifcopenshell.util.element.get_material(element)
    if mat_obj is None:
        return None

    ifc_type = mat_obj.is_a()

    # ── single material ──────────────────────────────────────────────────────
    if ifc_type == "IfcMaterial":
        return [{
            "name": mat_obj.Name,
            "material": {
                "name":     mat_obj.Name,
                "category": getattr(mat_obj, "Category", None),
            },
            "thickness": None,
        }]

    # ── layer set (most common for walls/slabs) ──────────────────────────────
    if ifc_type == "IfcMaterialLayerSet":
        return _layers_from_layer_set(mat_obj)

    # ── layer set usage wrapper (very common in IFC4) ────────────────────────
    if ifc_type == "IfcMaterialLayerSetUsage":
        layer_set = getattr(mat_obj, "ForLayerSet", None)
        if layer_set:
            return _layers_from_layer_set(layer_set)
        return None

    # ── constituent set (composite elements) ────────────────────────────────
    if ifc_type == "IfcMaterialConstituentSet":
        constituents = getattr(mat_obj, "MaterialConstituents", []) or []
        return [
            {
                "name": getattr(c, "Name", None),
                "material": {
                    "name":     getattr(c.Material, "Name", None) if c.Material else None,
                    "category": getattr(c, "Category", None),
                } if c.Material else None,
                "thickness": None,
            }
            for c in constituents
        ]

    # ── profile set (beams, columns) ─────────────────────────────────────────
    if ifc_type == "IfcMaterialProfileSet":
        profiles = getattr(mat_obj, "MaterialProfiles", []) or []
        return [
            {
                "name": getattr(p, "Name", None),
                "material": {
                    "name":     getattr(p.Material, "Name", None) if p.Material else None,
                    "category": getattr(p, "Category", None),
                } if p.Material else None,
                "thickness": None,
            }
            for p in profiles
        ]

    # ── fallback ─────────────────────────────────────────────────────────────
    name = str(getattr(mat_obj, "Name", mat_obj.is_a()))
    return [{"name": name, "material": {"name": name, "category": None}, "thickness": None}]


# ── Property set helpers ──────────────────────────────────────────────────────

def _parse_psets(element) -> list:
    """Convert ifcopenshell's pset dict into a GraphQL-compatible list."""
    raw = ifcopenshell.util.element.get_psets(element)
    result = []
    for pset_name, props in raw.items():
        properties = []
        for prop_name, prop_value in props.items():
            if prop_name == "id":
                continue
            str_val = str(prop_value) if prop_value is not None else None
            properties.append({
                "name":         prop_name,
                "nominalValue": str_val,
            })
        result.append({"name": pset_name, "properties": properties})
    return result


# ── Element extraction ────────────────────────────────────────────────────────

def get_element_data(element, ifc_filename: str = None) -> dict:
    """
    Build a full IFC element dict for GraphQL resolution.

    Dict keys match the GraphQL schema field names exactly so Ariadne's default
    resolver can map them without extra field resolvers:
      globalId, type, predefinedType, ownerHistory, storey, material, volume …
    """
    return {
        # ── IfcRoot / IfcBuildingElement fields (schema keys) ─────────────────
        "globalId":       element.GlobalId,
        "type":           element.is_a(),
        "predefinedType": str(getattr(element, "PredefinedType", None)) if getattr(element, "PredefinedType", None) else None,
        "name":           getattr(element, "Name", None),
        "ownerHistory":   _get_owner_history(element),
        "storey":         _get_storey(element),
        "material":       _get_material_layers(element),
        "volume":         _get_volume(element),

        # ── extra context (not in schema, used internally by resolvers) ───────
        "description":    getattr(element, "Description", None),
        "objectType":     getattr(element, "ObjectType", None),
        "tag":            getattr(element, "Tag", None),
        "psets":          _parse_psets(element),
        "fileName":       ifc_filename,
    }


# ── File discovery ────────────────────────────────────────────────────────────

def list_ifc_files(project_dir: str = "ifcs") -> list[tuple[int, str]]:
    """
    Scan project_dir for .ifc files, sorted by the exported timestamp in their
    FILE_NAME STEP header (oldest first). Falls back to file mtime if the header
    cannot be read.

    Returns a list of (version, filepath) tuples where version starts at 1.
    """
    entries = []
    for root, _, fnames in os.walk(project_dir):
        for fname in fnames:
            if fname.lower().endswith(".ifc"):
                fpath = os.path.join(root, fname)
                sort_key = _read_ifc_exported_at(fpath) or datetime.fromtimestamp(
                    os.path.getmtime(fpath), tz=timezone.utc
                ).isoformat()
                entries.append((sort_key, fpath))

    entries.sort(key=lambda x: x[0])
    return [(i + 1, fpath) for i, (_, fpath) in enumerate(entries)]


def find_ifc_file(ifc_filename: str, project_dir: str = "ifcs") -> str | None:
    """
    Search for an IFC file by name under the ifcs/ directory.
    Returns the full path if found, None otherwise.
    """
    for root, _, files in os.walk(project_dir):
        if ifc_filename in files:
            return os.path.join(root, ifc_filename)
    return None


# ── Version registry (file-based, no MongoDB) ────────────────────────────────

def list_versions(project_dir: str = "ifcs", ifc_project_guid: str = None) -> list[dict]:
    """
    Scan project_dir and return IfcVersion-shaped dicts sorted oldest-first
    by the exportedAt timestamp from each file's FILE_NAME STEP header.

    Opens each IFC file with ifcopenshell to read IfcProject.GlobalId.
    The returned dicts include a non-schema 'filePath' key used by
    elementVersionHistory to open the file for element extraction.
    """
    entries = []
    for root, _, fnames in os.walk(project_dir):
        for fname in fnames:
            if not fname.lower().endswith(".ifc"):
                continue
            fpath = os.path.join(root, fname)
            exported_at = _read_ifc_exported_at(fpath) or datetime.fromtimestamp(
                os.path.getmtime(fpath), tz=timezone.utc
            ).isoformat()

            project_guid = ""
            try:
                model = ifcopenshell.open(fpath)
                projects = model.by_type("IfcProject")
                if projects:
                    project_guid = projects[0].GlobalId
            except Exception:
                pass

            if ifc_project_guid and project_guid != ifc_project_guid:
                continue

            entries.append((exported_at, {
                "id":         project_guid,
                "fileName":   fname,
                "filePath":   fpath,
                "exportedAt": exported_at,
            }))

    entries.sort(key=lambda x: x[0])
    return [ver for _, ver in entries]


def match_version(
    versions: list[dict],
    event_time: str,
    ifc_project_guids: list[str],
    file_names: list[str],
    file_refs: list[dict] | None = None,
) -> dict | None:
    """
    4-tier IFC version matching on a pre-loaded version list.

    Tier 1 — exact file reference: ifcProjectGuid AND fileName from the SAME
              BCF header <File> entry.  The BCF spec stores both fields on one
              element to identify a specific file, not a project + any file.
              Matching them together prevents picking a newer version of the
              same project (same GUID, different filename) when the BCF
              explicitly names an older file.
    Tier 2 — ifcProjectGuid only: latest version with that project GUID before
              the event time.  Used when the BCF header has a GUID but no
              filename, or when the exact-file match fails (e.g. file renamed).
    Tier 3 — fileName only: case-insensitive basename comparison.  BCF tools
              often store full Windows paths; the server stores only basenames.
    Tier 4 — global: latest version before the event time, flagged inferred.

    For tiers 1-3 the ideal result is the latest matching version exported
    BEFORE the event time.  When no such version exists (IFC loaded after BCF
    import), the matching falls back to the earliest overall version satisfying
    the predicate.  File identity is still known, so inferred=False is kept.

    file_refs — raw BCF header File dicts, each with {ifcProjectGuid, fileName}.
                Passing this preserves the per-entry GUID↔name association that
                is lost when the two fields are collected into separate flat lists.

    Returns {version, inferred} or None if no versions exist.
    """
    if not versions:
        return None

    def latest_before(pred):
        # versions are sorted oldest-first; iterate reversed for latest-first
        for v in reversed(versions):
            if v["exportedAt"] <= event_time and pred(v):
                return v
        return None

    def best_match(pred):
        # Prefer latest version exported before event_time; when the IFC file
        # post-dates the BCF event, fall back to the earliest matching version.
        v = latest_before(pred)
        if v:
            return v
        for v in versions:
            if pred(v):
                return v
        return None

    # Tier 1 — exact file reference (GUID + filename from the same BCF entry)
    for ref in (file_refs or []):
        g = ref.get("ifcProjectGuid") if isinstance(ref, dict) else None
        n = ref.get("fileName")       if isinstance(ref, dict) else None
        if not g or not n:
            continue
        nb = os.path.basename(n).lower()
        ver = best_match(
            lambda v, g=g, nb=nb:
                v["id"] == g and os.path.basename(v["fileName"]).lower() == nb
        )
        if ver:
            return {"version": ver, "inferred": False}

    # Tier 2 — GUID only
    for guid in ifc_project_guids:
        if not guid:
            continue
        ver = best_match(lambda v, g=guid: v["id"] == g)
        if ver:
            return {"version": ver, "inferred": False}

    # Tier 3 — filename only (case-insensitive basename)
    for name in file_names:
        if not name:
            continue
        name_base = os.path.basename(name).lower()
        ver = best_match(
            lambda v, nb=name_base: os.path.basename(v["fileName"]).lower() == nb
        )
        if ver:
            return {"version": ver, "inferred": False}

    # Tier 4 — global fallback
    ver = latest_before(lambda v: True)
    if ver:
        return {"version": ver, "inferred": True}

    # Event predates all versions — use earliest available
    return {"version": versions[0], "inferred": True}


def extract_header_hints(topic: dict) -> tuple[list[str], list[str]]:
    """Pull ifcProjectGuid and fileName lists from a topic's BCF file references."""
    files = topic.get("files") or []
    guids = [f.get("ifcProjectGuid") for f in files if f.get("ifcProjectGuid")]
    names = [f.get("fileName")       for f in files if f.get("fileName")]
    return guids, names


# ── Geometry extraction ───────────────────────────────────────────────────────

def get_element_geometry(ifc_filepath: str, global_id: str) -> dict | None:
    """
    Tessellate a single IFC element and return flat mesh arrays in world coords.

    Returns a dict with:
        vertices — flat [x,y,z, x,y,z, ...] world-space positions
        faces    — flat triangle indices into the vertex list [i,j,k, ...]
        normals  — flat [nx,ny,nz, ...] per-vertex normals

    Returns None when the element has no geometry representation (e.g. abstract
    types, spaces without body geometry) or when tessellation fails.
    """
    try:
        ifc_model = ifcopenshell.open(ifc_filepath)
        element = ifc_model.by_guid(global_id)
        if element is None:
            return None

        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)

        shape = ifcopenshell.geom.create_shape(settings, element)
        geo = shape.geometry

        return {
            "vertices": list(geo.verts),
            "faces":    list(geo.faces),
            "normals":  list(geo.normals),
        }
    except Exception as e:
        logger.warning("Geometry extraction failed for %s: %s", global_id, e)
        return None


def mesh_to_glb(vertices: list, faces: list, normals: list) -> str:
    """
    Pack flat mesh arrays (IFC Z-up) into a base64-encoded GLB binary (glTF Y-up).

    Applies the IFC → glTF coordinate swap (Y↔Z) so the client can load the
    result directly with THREE.js GLTFLoader without any axis correction.
    """
    v = np.array(vertices, dtype=np.float32).reshape(-1, 3)
    # IFC Z-up → glTF Y-up: new_x=x, new_y=z, new_z=-y
    v_gltf = np.stack([v[:, 0], v[:, 2], -v[:, 1]], axis=1).astype(np.float32)

    has_normals = bool(normals) and len(normals) == len(vertices)
    if has_normals:
        n = np.array(normals, dtype=np.float32).reshape(-1, 3)
        n_gltf = np.stack([n[:, 0], n[:, 2], -n[:, 1]], axis=1).astype(np.float32)

    idx = np.array(faces, dtype=np.uint32)

    v_bytes   = v_gltf.tobytes()
    idx_bytes = idx.tobytes()
    n_bytes   = n_gltf.tobytes() if has_normals else b""

    # Layout: positions | normals | indices
    n_pos   = len(v_bytes)
    n_nrm   = len(n_bytes)
    blob    = v_bytes + n_bytes + idx_bytes

    bv_position = BufferView(buffer=0, byteOffset=0,       byteLength=n_pos, target=ARRAY_BUFFER)
    bv_indices  = BufferView(buffer=0, byteOffset=n_pos + n_nrm, byteLength=len(idx_bytes), target=ELEMENT_ARRAY_BUFFER)
    buffer_views = [bv_position, bv_indices]

    ac_position = Accessor(
        bufferView=0, byteOffset=0, componentType=FLOAT,
        count=len(v_gltf), type="VEC3",
        max=v_gltf.max(axis=0).tolist(), min=v_gltf.min(axis=0).tolist(),
    )
    ac_indices = Accessor(
        bufferView=1, byteOffset=0, componentType=UNSIGNED_INT,
        count=len(idx), type="SCALAR",
    )
    accessors = [ac_position, ac_indices]
    attrs = {"POSITION": 0}

    if has_normals:
        buffer_views.append(BufferView(buffer=0, byteOffset=n_pos, byteLength=n_nrm, target=ARRAY_BUFFER))
        accessors.append(Accessor(
            bufferView=2, byteOffset=0, componentType=FLOAT,
            count=len(v_gltf), type="VEC3",
        ))
        attrs["NORMAL"] = 2

    gltf = GLTF2()
    gltf.scene = 0
    gltf.scenes      = [Scene(nodes=[0])]
    gltf.nodes       = [Node(mesh=0)]
    gltf.meshes      = [Mesh(primitives=[Primitive(attributes=attrs, indices=1)])]
    gltf.bufferViews = buffer_views
    gltf.accessors   = accessors
    gltf.buffers     = [Buffer(byteLength=len(blob))]
    gltf.set_binary_blob(blob)

    return base64.b64encode(b"".join(gltf.save_to_bytes())).decode("ascii")


# ── Bulk extraction ───────────────────────────────────────────────────────────

def extract_elements_by_guids(ifc_filepath: str, guids: list[str]) -> dict[str, dict]:
    """
    Open an IFC file and extract full element data for each GUID in the list.

    Returns a dict keyed by globalId for fast resolver lookups:
        { "3ZGD7y6S...": { globalId, type, name, storey, material, … } }

    GUIDs not found in the file are silently skipped.
    """
    ifc_filename = os.path.basename(ifc_filepath)
    ifc_model = ifcopenshell.open(ifc_filepath)

    results = {}
    for guid in guids:
        try:
            el = ifc_model.by_guid(guid)
            results[guid] = get_element_data(el, ifc_filename)
        except Exception:
            pass  # GUID absent in this file — skip

    logger.debug("Extracted %d/%d elements from %s", len(results), len(guids), ifc_filename)
    return results


def extract_all_elements(
    ifc_filepath: str,
    type_filter: str = None,
    storey_filter: str = None,
    version: int = None,
) -> list[dict]:
    """
    Extract all IfcElement instances from an IFC file with optional filters.

    Args:
        ifc_filepath:  Path to the .ifc file.
        type_filter:   Only return elements of this IFC type (e.g. "IfcWall").
        storey_filter: Only return elements on this storey name.
        version:       Version number to stamp on each returned element dict.

    Returns list of element dicts matching the GraphQL IfcElement schema.
    """
    ifc_filename = os.path.basename(ifc_filepath)
    ifc_model = ifcopenshell.open(ifc_filepath)

    # IfcElement in IFC4 covers all physical building elements
    elements = ifc_model.by_type("IfcElement")

    results = []
    for el in elements:
        if type_filter and el.is_a() != type_filter:
            continue

        data = get_element_data(el, ifc_filename)

        if storey_filter:
            storey = data.get("storey")
            if not storey or storey.get("name") != storey_filter:
                continue

        if version is not None:
            data["version"] = version

        results.append(data)

    logger.debug("Extracted %d elements from %s", len(results), ifc_filename)
    return results
