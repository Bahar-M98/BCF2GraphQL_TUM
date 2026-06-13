"""
Registers all GraphQL resolvers with Ariadne's QueryType and ObjectType.
"""

import logging

from ariadne import QueryType, UnionType, ObjectType

logger = logging.getLogger(__name__)
from resolvers.query import (
    resolve_project,
    resolve_topics,
    resolve_topic,
    resolve_topic_history,
    resolve_topic_events,
    resolve_comment_events,
)
try:
    from resolvers.ifc_resolvers import (
        resolve_ifc_element,
        resolve_ifc_elements,
        resolve_ifc_element_topics,
        resolve_ifc_element_geometry,
        resolve_ifc_mesh_glb,
        resolve_component_ifc_element,
    )
    from resolvers.history_resolvers import (
        resolve_ifc_versions,
        resolve_ifc_version_for_event,
        resolve_topic_timeline,
        resolve_element_version_history,
        resolve_topics_for_element,
    )
    from resolvers.diff_resolvers import (
        resolve_ifc_file_diff,
        resolve_ifc_element_diff,
        resolve_ifc_element_diff_field,
    )
    _IFC_AVAILABLE = True
except ImportError:
    _IFC_AVAILABLE = False
    logger.warning("ifcopenshell not available — IFC resolvers disabled")
    async def _ifc_unavailable(*args, **kwargs):
        return None
    resolve_ifc_element = resolve_ifc_elements = resolve_ifc_element_topics = _ifc_unavailable
    resolve_ifc_element_geometry = resolve_ifc_mesh_glb = resolve_component_ifc_element = _ifc_unavailable
    resolve_ifc_versions = resolve_ifc_version_for_event = resolve_topic_timeline = _ifc_unavailable
    resolve_element_version_history = resolve_topics_for_element = _ifc_unavailable
    resolve_ifc_file_diff = resolve_ifc_element_diff = resolve_ifc_element_diff_field = _ifc_unavailable
from db.database import get_topic

query = QueryType()

# ── BCF queries ───────────────────────────────────────────────────────────────
query.set_field("project",       resolve_project)
query.set_field("topics",        resolve_topics)
query.set_field("topic",         resolve_topic)
query.set_field("topicHistory",  resolve_topic_history)

# BCF 3.0 spec Section 3.9 / 3.10 — event audit logs
query.set_field("topicEvents",   resolve_topic_events)
query.set_field("commentEvents", resolve_comment_events)

# ── IFC queries ───────────────────────────────────────────────────────────────
query.set_field("ifcElement",   resolve_ifc_element)
query.set_field("ifcElements",  resolve_ifc_elements)

# ── IFC version matching ───────────────────────────────────────────────────────
query.set_field("ifcVersions",         resolve_ifc_versions)
query.set_field("ifcVersionForEvent",  resolve_ifc_version_for_event)

# ── IFC version history + element change tracking ─────────────────────────────
query.set_field("topicTimeline",          resolve_topic_timeline)
query.set_field("elementVersionHistory",  resolve_element_version_history)

# ── BCF topics for an element ─────────────────────────────────────────────────
query.set_field("topicsForElement",  resolve_topics_for_element)

# ── IFC diff queries ───────────────────────────────────────────────────────────
query.set_field("ifcFileDiff",     resolve_ifc_file_diff)
query.set_field("ifcElementDiff",  resolve_ifc_element_diff)

# Camera is a union — resolve which concrete type to use via __typename
camera_type = UnionType("Camera")

@camera_type.type_resolver
def resolve_camera_type(obj, *_):
    return obj.get("__typename")

# IfcElement field resolvers
ifc_element_type = ObjectType("IfcElement")
ifc_element_type.set_field("topics",    resolve_ifc_element_topics)
ifc_element_type.set_field("geometry",  resolve_ifc_element_geometry)
ifc_element_type.set_field("diff",      resolve_ifc_element_diff_field)

# IfcMesh field resolvers
ifc_mesh_type = ObjectType("IfcMesh")
ifc_mesh_type.set_field("glb", resolve_ifc_mesh_glb)

# Component.ifcElement forward link: BCF component → IFC element
component_type = ObjectType("Component")
component_type.set_field("ifcElement", resolve_component_ifc_element)

# Comment.viewpoint: resolve viewpointGuid → full Viewpoint object
comment_type = ObjectType("Comment")

@comment_type.field("viewpoint")
async def resolve_comment_viewpoint(comment, info):
    vp_guid    = comment.get("viewpointGuid")
    topic_guid = comment.get("topicGuid")
    if not vp_guid or not topic_guid:
        return None
    topic = await get_topic(topic_guid)
    if not topic:
        return None
    return next(
        (vp for vp in topic.get("viewpoints", []) if vp.get("guid") == vp_guid),
        None,
    )
