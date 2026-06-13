import logging
from datetime import datetime

import ifcopenshell
import ifcopenshell.util.element
from bcf.bcfxml import load as bcf_load
from xsdata.models.datatype import XmlDateTime

logger = logging.getLogger(__name__)


# ── Date ──────────────────────────────────────────────────────────────────────
def parse_date(dt) -> dict | None:
    """
    Return a Date dict {timestamp: float, ISO8601: str} for any date input.
    timestamp is a Unix epoch float (seconds), useful for sorting and math.
    Returns None if input is None.
    """
    if dt is None:
        return None
    if isinstance(dt, XmlDateTime):
        dt = dt.to_datetime()
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if isinstance(dt, datetime):
        return {
            "timestamp": dt.timestamp(),
            "ISO8601":   dt.isoformat(),
        }
    return None


# ── Vector ────────────────────────────────────────────────────────────────────
def parse_vec(point) -> dict:
    return {"x": point.x, "y": point.y, "z": point.z}


# ── Camera ────────────────────────────────────────────────────────────────────
def parse_camera(visinfo_obj) -> dict | None:
    """
    Extract camera data using BCF 3.0 field names:
    cameraViewPoint, cameraDirection, cameraUpVector.
    """
    cam = visinfo_obj.perspective_camera
    if cam:
        return {
            "__typename":      "PerspectiveCamera",
            "cameraViewPoint": parse_vec(cam.camera_view_point),
            "cameraDirection": parse_vec(cam.camera_direction),
            "cameraUpVector":  parse_vec(cam.camera_up_vector),
            "fieldOfView":     cam.field_of_view,
            "aspectRatio":     getattr(cam, "aspect_ratio", None),
        }

    cam = visinfo_obj.orthogonal_camera
    if cam:
        return {
            "__typename":       "OrthogonalCamera",
            "cameraViewPoint":  parse_vec(cam.camera_view_point),
            "cameraDirection":  parse_vec(cam.camera_direction),
            "cameraUpVector":   parse_vec(cam.camera_up_vector),
            "viewToWorldScale": cam.view_to_world_scale,
            "aspectRatio":      getattr(cam, "aspect_ratio", None),
        }

    return None


# ── Components ────────────────────────────────────────────────────────────────
def parse_components(visinfo_obj) -> dict:
    """
    Parse components (selection, visibility, coloring) from a VisualizationInfo.
    IFC element enrichment is left to resolvers — they receive the IfcGuids
    and can load the correct IFC file version via the topic's Header files.
    """
    components = visinfo_obj.components
    if not components:
        return {"selection": [], "visibility": None, "coloring": []}

    def parse_component_list(wrapper) -> list:
        comp_list = getattr(wrapper, "component", []) or []
        return [
            {
                "ifcGuid":           c.ifc_guid,
                "originatingSystem": getattr(c, "originating_system", None),
                "authoringToolId":   getattr(c, "authoring_tool_id", None),
            }
            for c in comp_list if c.ifc_guid
        ]

    # Selection
    selection = []
    if components.selection:
        selection = parse_component_list(components.selection)

    # Visibility
    visibility = None
    if components.visibility:
        vis = components.visibility
        exceptions = parse_component_list(vis.exceptions) if vis.exceptions else []
        hints = None
        if vis.view_setup_hints:
            h = vis.view_setup_hints
            hints = {
                "spacesVisible":            getattr(h, "spaces_visible", None),
                "spaceBoundariesVisible":   getattr(h, "space_boundaries_visible", None),
                "openingsVisible":          getattr(h, "openings_visible", None),
            }
        visibility = {
            "defaultVisibility": getattr(vis, "default_visibility", None),
            "exceptions":        exceptions,
            "viewSetupHints":    hints,
        }

    # Coloring
    coloring = []
    if components.coloring:
        color_list = getattr(components.coloring, "color", []) or []
        for entry in color_list:
            coloring.append({
                "color":      getattr(entry, "color", None),
                "components": parse_component_list(entry),
            })

    return {"selection": selection, "visibility": visibility, "coloring": coloring}


# ── Header / Files ────────────────────────────────────────────────────────────
def parse_header_files(handler) -> list:
    """
    Extract File entries from the BCF XML Header element.

    In the BCF spec the Header lives at Markup level (not inside Topic):
        Markup → Header → Files → File[]

    The bcf-client exposes it as handler.header.files.file[].
    Returns an empty list when this topic has no header files.
    """
    header = getattr(handler, "header", None)
    if not header:
        return []
    header_files = getattr(header, "files", None)
    raw_files = getattr(header_files, "file", []) or [] if header_files else []
    return [
        {
            "ifcProjectGuid":             getattr(f, "ifc_project", None),
            "ifcSpatialStructureElement": getattr(f, "ifc_spatial_structure_element", None),
            "isExternal":                 getattr(f, "is_external", None),
            "fileName":                   getattr(f, "filename", None),
            "date":                       parse_date(getattr(f, "date", None)),
            "reference":                  getattr(f, "reference", None),
        }
        for f in raw_files
    ]


# ── Viewpoint GUID map ────────────────────────────────────────────────────────
def build_viewpoint_guid_map(handler) -> dict:
    """
    Build a filename-to-GUID map for a topic's viewpoints.

    When iterating handler.viewpoints.items(), ifcopenshell uses the
    viewpoint filename (e.g. "viewpoint.bcfv") as the dictionary key
    rather than the actual UUID. The real GUID lives in the markup's
    ViewPoint element as the Guid attribute.

    This function reads the topic's ViewPoint references from the markup
    and returns a mapping so the real GUIDs can be looked up by filename
    during viewpoint parsing.

    """
    guid_map = {}
    viewpoints_wrapper = getattr(handler.topic, "viewpoints", None)
    if viewpoints_wrapper is None:
        return guid_map

    # TopicViewpoints wraps the list in .view_point (not directly iterable)
    vp_refs = getattr(viewpoints_wrapper, "view_point", []) or []
    for vp_ref in vp_refs:
        if vp_ref.viewpoint and vp_ref.guid:
            guid_map[vp_ref.viewpoint] = vp_ref.guid

    return guid_map


# ── Main parser ───────────────────────────────────────────────────────────────
def parse_bcf(filepath: str, ifc_filepath: str = None) -> dict:
    """
    Parse a BCF file into a GraphQL-compatible Python dict matching the
    BCF 3.0 spec schema.
    """
    ifc_model = None
    if ifc_filepath:
        ifc_model = ifcopenshell.open(ifc_filepath)
        logger.debug("Loaded IFC file: %s", ifc_filepath)

    result = {"project": None, "topics": []}

    with bcf_load(filepath) as bcfxml:

        # ── Project ───────────────────────────────────────────────────────
        p = bcfxml.project
        result["project"] = {
            "projectId": getattr(p, "project_id", None) if p else None,
            "name":      p.name if p else None,
            "topics":    []
        }

        # ── Topics ────────────────────────────────────────────────────────
        for guid, handler in bcfxml.topics.items():
            topic = handler.topic

            # ── Comments ──────────────────────────────────────────────────
            comments = []
            for c in handler.comments:
                vp_ref = getattr(c, "viewpoint", None)
                vp_guid = getattr(vp_ref, "guid", None) if vp_ref else None
                comments.append({
                    "guid":           c.guid,
                    "date":           parse_date(c.date),
                    "author":         c.author,
                    "comment":        c.comment,
                    "topicGuid":      guid,   # spec: topic_guid (required in comment_GET)
                    "modifiedDate":   parse_date(getattr(c, "modified_date", None)),
                    "modifiedAuthor": getattr(c, "modified_author", None),
                    "viewpointGuid":  vp_guid,
                })

            # ── Viewpoints ────────────────────────────────────────────────
            vp_guid_map = build_viewpoint_guid_map(handler)

            viewpoints = []
            for vp_filename, vp_handler in handler.viewpoints.items():
                real_guid = vp_guid_map.get(vp_filename, vp_filename)

                try:
                    visinfo_obj = vp_handler.visualization_info
                    components  = parse_components(visinfo_obj)
                    camera      = parse_camera(visinfo_obj)

                    # Lines
                    lines = []
                    if visinfo_obj.lines:
                        for line in (getattr(visinfo_obj.lines, "line", []) or []):
                            lines.append({
                                "startPoint": parse_vec(line.start_point),
                                "endPoint":   parse_vec(line.end_point),
                            })

                    # Clipping planes
                    clipping_planes = []
                    if visinfo_obj.clipping_planes:
                        for cp in (getattr(visinfo_obj.clipping_planes, "clipping_plane", []) or []):
                            clipping_planes.append({
                                "location":  parse_vec(cp.location),
                                "direction": parse_vec(cp.direction),
                            })

                    # Bitmaps
                    bitmaps = []
                    if visinfo_obj.bitmaps:
                        for bm in (getattr(visinfo_obj.bitmaps, "bitmap", []) or []):
                            # bitmap_type: bcf-client may return an enum or string
                            fmt = getattr(bm, "format", None)
                            if fmt is not None:
                                bm_type = getattr(fmt, "value", None) or getattr(fmt, "name", None) or str(fmt)
                                bm_type = bm_type.upper() if bm_type else None
                            else:
                                bm_type = None
                            bitmaps.append({
                                "bitmapType": bm_type,          # spec: bitmap_type
                                "location":   parse_vec(bm.location),
                                "normal":     parse_vec(bm.normal),
                                "up":         parse_vec(bm.up),
                                "height":     getattr(bm, "height", None),
                            })

                except Exception as e:
                    logger.warning("Could not parse viewpoint %s: %s", vp_filename, e)
                    components      = {"selection": [], "visibility": None, "coloring": []}
                    camera          = None
                    lines           = []
                    clipping_planes = []
                    bitmaps         = []

                viewpoints.append({
                    "guid":      real_guid,
                    "viewpoint": vp_filename,
                    "snapshot":  None,   # Snapshot object {snapshotType, snapshotData}; not extracted from ZIP yet
                    "index":     None,
                    "components":     components,
                    "camera":         camera,
                    "lines":          lines,
                    "clippingPlanes": clipping_planes,
                    "bitmaps":        bitmaps,
                })

            # ── Reference links (BCF 3.0: plural) ────────────────────────
            reference_links = []
            rl = getattr(topic, "reference_links", None)
            if rl:
                if isinstance(rl, list):
                    reference_links = [str(r) for r in rl]
                elif hasattr(rl, "reference_link"):
                    reference_links = [str(r) for r in (getattr(rl, "reference_link", []) or [])]

            # ── Related topics ────────────────────────────────────────────
            # Store as minimal dicts — resolver fetches full Topic by guid
            related_topics = []
            rt_wrapper = getattr(topic, "related_topics", None)
            if rt_wrapper:
                rt_list = getattr(rt_wrapper, "related_topic", []) or []
                related_topics = [{"guid": getattr(rt, "related_topic", str(rt))} for rt in rt_list]

            # ── Topic dict ────────────────────────────────────────────────
            result["topics"].append({
                "guid":               topic.guid,
                "serverAssignedId":   getattr(topic, "server_assigned_id", None),
                "topicType":          topic.topic_type,
                "topicStatus":        topic.topic_status,
                "referenceLinks":     reference_links,
                "title":              topic.title,
                "priority":           topic.priority,
                "index":              getattr(topic, "index", None),
                "labels":             list(getattr(topic.labels, "label", topic.labels) or []) if topic.labels else [],
                "creationDate":       parse_date(topic.creation_date),
                "creationAuthor":     topic.creation_author,
                "modifiedDate":       parse_date(getattr(topic, "modified_date", None)),
                "modifiedAuthor":     getattr(topic, "modified_author", None),
                "dueDate":            parse_date(getattr(topic, "due_date", None)),
                "assignedTo":         topic.assigned_to,
                "description":        topic.description,
                "stage":              getattr(topic, "stage", None),
                "bimSnippet":         None,
                "documentReferences": [],
                "relatedTopics":      related_topics,
                "comments":           comments,
                "viewpoints":         viewpoints,
                "files":              parse_header_files(handler),
            })

        result["project"]["topics"] = result["topics"]

    return result


# ── Standalone ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    data = parse_bcf("Test.bcf")
    print(json.dumps(data, indent=2))
