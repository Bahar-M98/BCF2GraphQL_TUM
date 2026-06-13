"""
Structural diff between two IFC model versions.

Wraps the ifcdiff library and translates its output into the dict shapes
expected by schema/diff.graphql. Both entry points reopen the IFC files on
every call — no caching — which is stateless and correct for this dataset size.
"""

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.placement
from ifcdiff import IfcDiff

# Relationship types to check during comparison.
# All six are enabled (is_shallow=False) so the diff is complete.
# Omitting "geometry" would make the diff much faster but miss shape changes.
# Omitting "property" would miss Pset_* value changes, which are common in BIM workflows.
_RELATIONSHIPS = [
    "attributes",
    "geometry",
    "property",
    "type",
    "container",
    "aggregate",
    "classification",
]


def _by_guid(model: ifcopenshell.file, guid: str):
    """
    Look up an IFC entity by GlobalId, returning None if not found.

    ifcopenshell raises RuntimeError (not a Python KeyError) when a GUID is absent.
    Wrapping it here lets callers use a simple None-check rather than a try/except
    at every call site.  This matters in compute_element_diff where both el_a and el_b
    may legitimately be None (element added or deleted between versions).
    """
    try:
        return model.by_guid(guid)
    except RuntimeError:
        return None


def _element_summary(ifc_file: ifcopenshell.file, guid: str) -> dict:
    """
    Return a minimal {globalId, type, name} dict for one element.

    Used to build the added/deleted/modified lists in compute_file_diff.
    The full element detail is intentionally omitted — callers who want it can run
    compute_element_diff on any GUID from these lists.

    If the element is somehow not found in the file (shouldn't happen since the GUID
    came from the diff output, but defensive), type and name are returned as None.
    """
    el = _by_guid(ifc_file, guid)
    if el is None:
        return {"globalId": guid, "type": None, "name": None}
    return {"globalId": guid, "type": el.is_a(), "name": getattr(el, "Name", None)}


def _s(v) -> str | None:
    """
    Convert any IFC attribute value to a plain Python string.

    IFC attribute values are not always plain strings — they can be:
      - ifcopenshell entity references (e.g. IfcLabel wrapping a string)
      - numeric types (IfcReal, IfcInteger)
      - None / null

    str() normalises all of these to a consistent comparable type.  Keeping None as
    None (rather than converting to "None") lets the diff logic distinguish "field
    was absent" from "field had the value 'None'".
    """
    return str(v) if v is not None else None


def _placement_point(el) -> dict | None:
    """
    Extract the world-space (x, y, z) origin of an element's placement.

    IFC stores element placement as a 4×4 homogeneous transformation matrix obtained
    by composing the local placement chain up to the world coordinate system.
    ifcopenshell.util.placement.get_local_placement() performs this composition and
    returns the matrix as a 4×4 numpy array.

    The translation component lives in column index 3 (the fourth column):
      m[0][3] = X,  m[1][3] = Y,  m[2][3] = Z

    This is compared between versions to detect whether an element has moved in space,
    even if its other attributes are unchanged (e.g. a wall shifted during coordination).

    Returns None if the element has no ObjectPlacement (e.g. type objects, assemblies)
    or if the placement computation fails for any reason.
    """
    if el is None:
        return None
    placement = getattr(el, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        m = ifcopenshell.util.placement.get_local_placement(placement)
        return {
            "x": round(float(m[0][3]), 6),
            "y": round(float(m[1][3]), 6),
            "z": round(float(m[2][3]), 6),
        }
    except Exception:
        return None


def _attribute_changes(el_a, el_b) -> list[dict]:
    """
    Compare the five core IFC product attributes and return only those that changed.

    The five attributes chosen are the ones most commonly edited in BIM authoring tools
    and most relevant to BCF issue tracking:
      Name           — element display name in the BIM tool
      Description    — free-text description
      ObjectType     — overrides the type name for this instance
      PredefinedType — standardised subtype (e.g. WALL, DOOR, WINDOW variants)
      Tag            — identifier from the authoring tool (e.g. Revit mark number)

    Only changed attributes are returned (old != new).  Unchanged attributes are omitted
    to keep the response payload small — a typical diff involves only 1-2 changed fields.

    Either element being None is valid: el_a = None means the element was added (no
    old values), el_b = None means it was deleted (no new values).
    """
    attrs = ["Name", "Description", "ObjectType", "PredefinedType", "Tag"]
    changes = []
    for attr in attrs:
        old = _s(getattr(el_a, attr, None)) if el_a is not None else None
        new = _s(getattr(el_b, attr, None)) if el_b is not None else None
        if old != new:
            changes.append({"attribute": attr, "oldValue": old, "newValue": new})
    return changes


def _property_changes(el_a, el_b) -> list[dict]:
    """
    Compare all Pset_* and Qto_* property set values and return changed entries.

    ifcopenshell.util.element.get_psets() returns a nested dict:
      { pset_name: { property_name: value, ... }, ... }

    The union of both versions' pset names is iterated so that added and removed
    property sets are captured, not just modified ones.  Within each pset, the union
    of property names is iterated for the same reason.

    DESIGN NOTE — skip "id":
    get_psets() injects an internal "id" key (the IFC entity's numeric instance ID)
    into each pset dict.  This ID changes between file saves even when the actual
    property values are identical, which would produce false positives.  Skipping "id"
    ensures only real value changes are reported.

    DESIGN NOTE — sorted() on pset and property names:
    Sorted iteration produces deterministic output order across runs, which makes the
    diff results easier to read and compare in tests.
    """
    psets_a = ifcopenshell.util.element.get_psets(el_a) if el_a is not None else {}
    psets_b = ifcopenshell.util.element.get_psets(el_b) if el_b is not None else {}
    changes = []
    for pset_name in sorted(set(psets_a) | set(psets_b)):
        props_a = psets_a.get(pset_name, {})
        props_b = psets_b.get(pset_name, {})
        for prop_name in sorted(set(props_a) | set(props_b)):
            if prop_name == "id":
                # Internal ifcopenshell entity ID — changes every save, not a real diff
                continue
            old = _s(props_a[prop_name]) if prop_name in props_a else None
            new = _s(props_b[prop_name]) if prop_name in props_b else None
            if old != new:
                changes.append({
                    "pset":     pset_name,
                    "property": prop_name,
                    "oldValue": old,
                    "newValue": new,
                })
    return changes


def _type_name(el) -> str | None:
    """
    Return the name of the element's IFC type object (e.g. a Revit family name).

    In IFC, a type object (IfcWallType, IfcDoorType, etc.) holds shared properties for
    all instances of that type.  get_type() walks the IfcRelDefinesByType relationship.
    The Name attribute of the type is what BIM tools display as the "family" or "type"
    name.  If the type assignment changes between versions, it means the element was
    retyped (e.g. a door was changed from a single-leaf to a double-leaf door type).
    """
    if el is None:
        return None
    t = ifcopenshell.util.element.get_type(el)
    return _s(getattr(t, "Name", None)) if t else None


def _container_name(el) -> str | None:
    """
    Return the name of the spatial container (storey or space) this element belongs to.

    IFC spatial structure: IfcProject → IfcSite → IfcBuilding → IfcBuildingStorey → IfcSpace.
    get_container() walks the IfcRelContainedInSpatialStructure relationship upward to
    find the direct container.  If this changes between versions, the element moved to
    a different storey or space (e.g. a wall was reassigned from Level 1 to Level 2).
    """
    if el is None:
        return None
    c = ifcopenshell.util.element.get_container(el)
    return _s(getattr(c, "Name", None)) if c else None


def compute_file_diff(path_a: str, path_b: str) -> dict:
    """
    Run a full model diff and return summary lists of added, deleted, and modified elements.

    This is used by the ifcFileDiff GraphQL query to give a project manager a quick
    overview of what changed between two IFC versions without having to drill into
    individual elements.

    The IfcDiff object collects results into three sets:
      diff.added_elements   — GlobalIds present in model_b but not model_a
      diff.deleted_elements — GlobalIds present in model_a but not model_b
      diff.change_register  — dict of {GlobalId: {change_type: bool}} for modified elements

    DESIGN NOTE — sort(diff.*):
    Sorting the GUID sets before building the response lists gives deterministic
    output order, which makes the GraphQL response stable across identical calls
    and easier to compare in tests or thesis examples.

    DESIGN NOTE — summary only (no full element data):
    This function returns {globalId, type, name} per element, not full IfcElement dicts.
    Full data would make the response very large for models with hundreds of changes.
    Callers who want full detail on a specific changed element can call compute_element_diff.
    """
    model_a = ifcopenshell.open(path_a)
    model_b = ifcopenshell.open(path_b)

    # is_shallow=False: check all relationship types, not just attributes.
    # This is slower but complete — geometry, properties, type, container all checked.
    diff = IfcDiff(model_a, model_b, relationships=_RELATIONSHIPS, is_shallow=False)
    diff.diff()

    return {
        "added":    [_element_summary(model_b, g) for g in sorted(diff.added_elements)],
        "deleted":  [_element_summary(model_a, g) for g in sorted(diff.deleted_elements)],
        "modified": [_element_summary(model_b, g) for g in sorted(diff.change_register)],
    }


def compute_element_diff(path_a: str, path_b: str, global_id: str) -> dict | None:
    """
    Return a detailed field-level diff for one specific element between two IFC files.

    Returns None if the element does not exist in either file — this prevents the
    GraphQL resolver from returning a result for a GUID that has nothing to compare.

    HOW STATUS IS DETERMINED
    ─────────────────────────
    Status is derived from our own change detection rather than blindly trusting
    ifcdiff's change_register:

      "deleted"  — GlobalId in diff.deleted_elements (not in model_b)
      "added"    — GlobalId in diff.added_elements   (not in model_a)
      "modified" — any of: attribute changes, property changes, geometry changed,
                   type changed, container changed, aggregate changed,
                   classification changed
      "unchanged" — none of the above detected

    DESIGN NOTE — why not use ifcdiff's properties_changed flag directly:
    ifcdiff can set properties_changed=True as a false positive when property set
    entity IDs differ between file saves even though no actual property values changed
    (the IFC STEP format assigns new entity IDs on every save).  Our _property_changes()
    function compares actual values via get_psets() and skips the "id" key, so it
    correctly reports unchanged when values are the same.  Using our own detection as
    the source of truth avoids surfacing these false positives to the API consumer.

    GEOMETRY AND PLACEMENT
    ──────────────────────
    geometry_changed comes from ifcdiff's flag (CGAL-based shape comparison).
    placementOld/New are extracted separately as world-space XYZ points so the client
    can display where the element moved without having to parse the raw IFC placement
    chain.  These are independent: an element can move (placement change) without its
    shape changing, or its shape can change without it moving.
    """
    model_a = ifcopenshell.open(path_a)
    model_b = ifcopenshell.open(path_b)

    diff = IfcDiff(model_a, model_b, relationships=_RELATIONSHIPS, is_shallow=False)
    diff.diff()

    el_a = _by_guid(model_a, global_id)
    el_b = _by_guid(model_b, global_id)

    # Element must exist in at least one version to produce a meaningful diff
    if el_a is None and el_b is None:
        return None

    # Extract per-relationship change flags from the diff register.
    # Absent key means ifcdiff found no change for that relationship type.
    flags = diff.change_register.get(global_id, {})

    attribute_changes      = _attribute_changes(el_a, el_b)
    property_changes       = _property_changes(el_a, el_b)
    geometry_changed       = bool(flags.get("geometry_changed"))
    type_changed           = bool(flags.get("type_changed"))
    container_changed      = bool(flags.get("container_changed"))
    aggregate_changed      = bool(flags.get("aggregate_changed"))
    classification_changed = bool(flags.get("classification_changed"))

    # Determine status from our own detectors, not from ifcdiff's raw flags.
    # See module docstring for why this matters (false positive avoidance).
    if global_id in diff.deleted_elements:
        status = "deleted"
    elif global_id in diff.added_elements:
        status = "added"
    elif (attribute_changes or property_changes or geometry_changed
          or type_changed or container_changed
          or aggregate_changed or classification_changed):
        status = "modified"
    else:
        status = "unchanged"

    return {
        "globalId":  global_id,
        "status":    status,
        "unchanged": status == "unchanged",

        # Field-level diffs — empty lists when nothing changed in that category
        "attributeChanges": attribute_changes,
        "propertyChanges":  property_changes,

        # Geometry: boolean flag from ifcdiff (CGAL shape diff) + world XYZ positions
        "geometryChanged": geometry_changed,
        "placementOld":    _placement_point(el_a),
        "placementNew":    _placement_point(el_b),

        # Type assignment (e.g. Revit family changed)
        "typeChanged": type_changed,
        "oldType":     _type_name(el_a),
        "newType":     _type_name(el_b),

        # Spatial container (e.g. moved to a different storey)
        "containerChanged": container_changed,
        "oldContainer":     _container_name(el_a),
        "newContainer":     _container_name(el_b),

        # Remaining structural relationship flags (boolean only, no detail)
        "aggregateChanged":      aggregate_changed,
        "classificationChanged": classification_changed,
    }
