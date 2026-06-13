"""
BCF REST API 3.0 endpoints (buildingSMART spec). Prefix /bcf/3.0 is applied in rest/__init__.py.

  GET /projects
  GET /projects/{project_id}
  GET /projects/{project_id}/topics
  GET /projects/{project_id}/topics/events                         (spec §3.9)
  GET /projects/{project_id}/topics/comments/events                (spec §3.10)
  GET /projects/{project_id}/topics/{guid}
  GET /projects/{project_id}/topics/{guid}/files                   (spec §3.3)
  GET /projects/{project_id}/topics/{guid}/comments
  GET /projects/{project_id}/topics/{guid}/comments/{cguid}
  GET /projects/{project_id}/topics/{guid}/events                  (spec §3.9)
  GET /projects/{project_id}/topics/{guid}/viewpoints
  GET /projects/{project_id}/topics/{guid}/viewpoints/{vguid}
  GET /projects/{project_id}/topics/{guid}/viewpoints/{vguid}/selection
  GET /projects/{project_id}/topics/{guid}/viewpoints/{vguid}/coloring
  GET /projects/{project_id}/topics/{guid}/viewpoints/{vguid}/visibility
  GET /projects/{project_id}/topics/{guid}/viewpoints/{vguid}/bitmaps
  GET /projects/{project_id}/topics/{guid}/comments/{cguid}/events (spec §3.10)

Route ordering: literal segments (/topics/events) must be declared before
parameterised ones (/topics/{guid}) or FastAPI treats the literal as a value.
"""

from fastapi import APIRouter, HTTPException, Query
from db.database import (
    get_projects,
    get_project,
    get_topics_for_project,
    get_topic_for_project,
    get_topic_events_for_project,
    get_comment_events_for_project,
)
from rest.odata_filter import apply_filter

router = APIRouter(tags=["BCF"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _filter_events(events: list[dict], filter: str | None) -> list[dict]:
    """Apply an OData $filter, raising 400 if it cannot be parsed."""
    try:
        return apply_filter(events, filter)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid $filter: {exc}")


async def _require_project(project_id: str) -> dict:
    project = await get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _require_topic(project_id: str, guid: str) -> dict:
    await _require_project(project_id)
    topic = await get_topic_for_project(project_id, guid)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    return topic


# BCF 3.0 spec-defined topic fields — the exact set the standard mandates for
# GET /topics and GET /topics/{guid}.  Using an allowlist rather than a blacklist
# ensures MongoDB-internal fields (projectId, version, bcfSourceFiles) are never
# leaked into the response, which would inflate payload size and make the
# GraphQL vs REST payload comparison unfair.
_BCF_TOPIC_FIELDS = {
    "guid", "serverAssignedId", "topicType", "topicStatus",
    "referenceLinks", "title", "priority", "index", "labels",
    "creationDate", "creationAuthor", "modifiedDate", "modifiedAuthor",
    "dueDate", "assignedTo", "stage", "description",
    "bimSnippet", "documentReferences", "relatedTopics",
}


def _normalize_dates(obj):
    """
    Recursively replace MongoDB date dicts with their ISO 8601 string.

    MongoDB dates are stored as {"timestamp": <float>, "ISO8601": <str>} so
    that Python code can do numeric comparisons.  The REST API should return
    only the ISO 8601 string — that is what the BCF 3.0 spec mandates and
    what the GraphQL queries request.  Sending the raw MongoDB object would
    inflate REST payloads with timestamp numbers the client never asked for,
    making the GraphQL vs REST payload comparison unfair.
    """
    if isinstance(obj, dict):
        if obj.keys() == {"timestamp", "ISO8601"}:
            return {"ISO8601": obj["ISO8601"]}
        return {k: _normalize_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_dates(item) for item in obj]
    return obj


def _topic_summary(topic: dict) -> dict:
    """
    Return only the BCF 3.0 spec-defined fields for a topic.

    Comments and viewpoints are accessed through their own sub-resource
    endpoints.  MongoDB-internal fields (projectId, version, bcfSourceFiles)
    are excluded so the payload matches exactly what the GraphQL topics_flat
    query returns, making the flat/control benchmark scenario apples-to-apples.
    """
    return {k: v for k, v in topic.items() if k in _BCF_TOPIC_FIELDS}


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects():
    """List all BCF projects."""
    return await get_projects()


@router.get("/projects/{project_id}")
async def get_project_by_id(project_id: str):
    """Get a single BCF project."""
    return await _require_project(project_id)


# ── Topics ────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/topics")
async def list_topics(project_id: str):
    """
    List all topics for a project (topic fields only — no comments/viewpoints).

    To get the IFC elements referenced in each topic, the client must follow up
    with GET .../viewpoints/{vguid}/selection for each viewpoint, then
    GET /api/ifc/files/{file}/elements/{ifcGuid} for each component.
    """
    await _require_project(project_id)
    topics = await get_topics_for_project(project_id)
    return [_normalize_dates(_topic_summary(t)) for t in topics]


# ── Topic Events (Section 3.9) — MUST be declared before /topics/{guid} ──────

@router.get("/projects/{project_id}/topics/events")
async def list_topic_events(
    project_id: str,
    filter: str | None = Query(default=None, alias="$filter"),
):
    """
    BCF 3.0 Section 3.9.1 — get all topic events for a project.

    Events are derived from stored version history: version 1 generates
    topic_created plus field events for every initially-set field; subsequent
    versions generate events for each field that changed.

    Supports OData $filter on `author`, `type` (matches if any action has
    that type), and `date` (eq/ne/gt/ge/lt/le).
    """
    await _require_project(project_id)
    events = await get_topic_events_for_project(project_id)
    events = _filter_events(events, filter)
    return _normalize_dates(events)


# ── Comment Events (Section 3.10) — MUST be declared before /topics/{guid} ───

@router.get("/projects/{project_id}/topics/comments/events")
async def list_comment_events(
    project_id: str,
    filter: str | None = Query(default=None, alias="$filter"),
):
    """
    BCF 3.0 Section 3.10.1 — get all comment events for a project.

    Events are derived from stored version history by comparing the comments
    array across consecutive topic versions.

    Supports OData $filter on `author`, `type` (matches if any action has
    that type), and `date` (eq/ne/gt/ge/lt/le).
    """
    await _require_project(project_id)
    events = await get_comment_events_for_project(project_id)
    events = _filter_events(events, filter)
    return _normalize_dates(events)


# ── Single topic — declared AFTER literal-segment routes above ────────────────

@router.get("/projects/{project_id}/topics/{guid}")
async def get_topic_by_guid(project_id: str, guid: str):
    """Get a single topic (topic fields only — no comments/viewpoints)."""
    topic = await _require_topic(project_id, guid)
    return _normalize_dates(_topic_summary(topic))


# ── Files (Section 3.3) ───────────────────────────────────────────────────────

@router.get("/projects/{project_id}/topics/{guid}/files")
async def get_topic_files(project_id: str, guid: str):
    """
    BCF 3.0 Section 3.3.2 — get the header files for a topic.

    Returns the array of File objects that link this topic to IFC model
    version(s). Each file carries ifcProjectGuid and fileName so clients
    can locate the corresponding IFC file.
    """
    topic = await _require_topic(project_id, guid)
    return _normalize_dates(topic.get("files", []))


# BCF 3.0 spec-defined comment fields (Section 3.3.2).
# topicGuid is stored in MongoDB for cross-document lookups but is NOT part of
# the spec-mandated GET /topics/{guid}/comments response — the client already
# knows the topic it requested.  Excluding it keeps the REST payload symmetric
# with what GraphQL returns (GraphQL resolvers never expose topicGuid on Comment).
_BCF_COMMENT_FIELDS = {
    "guid", "date", "author", "comment",
    "modifiedDate", "modifiedAuthor", "viewpointGuid",
}


# ── Comments ──────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/topics/{guid}/comments")
async def list_comments(project_id: str, guid: str):
    """List all comments for a topic."""
    topic = await _require_topic(project_id, guid)
    comments = [{k: v for k, v in c.items() if k in _BCF_COMMENT_FIELDS}
                for c in topic.get("comments", [])]
    return _normalize_dates(comments)


@router.get("/projects/{project_id}/topics/{guid}/comments/{cguid}")
async def get_comment(project_id: str, guid: str, cguid: str):
    """BCF 3.0 Section 3.4.2 — get a single comment by GUID."""
    topic = await _require_topic(project_id, guid)
    comment = next((c for c in topic.get("comments", []) if c.get("guid") == cguid), None)
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return _normalize_dates({k: v for k, v in comment.items() if k in _BCF_COMMENT_FIELDS})


# ── Topic Events for a single topic (Section 3.9.2) ──────────────────────────

@router.get("/projects/{project_id}/topics/{guid}/events")
async def list_topic_events_for_topic(
    project_id: str,
    guid: str,
    filter: str | None = Query(default=None, alias="$filter"),
):
    """
    BCF 3.0 Section 3.9.2 — get events for a single topic.

    Returns the full audit history of this topic: what changed, when, and
    by whom.  Events are derived from the stored import version history.

    Supports OData $filter on `author`, `type` (matches if any action has
    that type), and `date` (eq/ne/gt/ge/lt/le).
    """
    await _require_topic(project_id, guid)
    events = await get_topic_events_for_project(project_id, topic_guid=guid)
    events = _filter_events(events, filter)
    return _normalize_dates(events)


# ── Viewpoints ────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/topics/{guid}/viewpoints")
async def list_viewpoints(project_id: str, guid: str):
    """
    List viewpoint stubs for a topic (guid, filename, snapshot, index only).

    To retrieve the full viewpoint including camera and components the client
    must issue a separate GET for each viewpoint GUID.
    """
    topic = await _require_topic(project_id, guid)
    return [
        {
            "guid":      vp.get("guid"),
            "viewpoint": vp.get("viewpoint"),
            "snapshot":  vp.get("snapshot"),
            "index":     vp.get("index"),
        }
        for vp in topic.get("viewpoints", [])
    ]


@router.get("/projects/{project_id}/topics/{guid}/viewpoints/{vguid}")
async def get_viewpoint(project_id: str, guid: str, vguid: str):
    """Get a full viewpoint including camera, components, lines and clipping planes."""
    topic = await _require_topic(project_id, guid)
    vp = next((v for v in topic.get("viewpoints", []) if v.get("guid") == vguid), None)
    if not vp:
        raise HTTPException(status_code=404, detail="Viewpoint not found")
    return vp


@router.get("/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/selection")
async def get_viewpoint_selection(project_id: str, guid: str, vguid: str):
    """
    Get the list of selected IFC components for a viewpoint.

    Each entry contains an ifcGuid.  To get the element's properties the client
    must issue GET /api/ifc/files/{filename}/elements/{ifcGuid} separately —
    one request per component.  This is the N+1 problem that GraphQL solves
    server-side via the Component.ifcElement resolver.
    """
    topic = await _require_topic(project_id, guid)
    vp = next((v for v in topic.get("viewpoints", []) if v.get("guid") == vguid), None)
    if not vp:
        raise HTTPException(status_code=404, detail="Viewpoint not found")
    return vp.get("components", {}).get("selection", [])


@router.get("/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/coloring")
async def get_viewpoint_coloring(project_id: str, guid: str, vguid: str):
    """BCF 3.0 Section 3.5.7 — get the coloring list for a viewpoint."""
    topic = await _require_topic(project_id, guid)
    vp = next((v for v in topic.get("viewpoints", []) if v.get("guid") == vguid), None)
    if not vp:
        raise HTTPException(status_code=404, detail="Viewpoint not found")
    return vp.get("components", {}).get("coloring", [])


@router.get("/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/visibility")
async def get_viewpoint_visibility(project_id: str, guid: str, vguid: str):
    """BCF 3.0 Section 3.5.8 — get the visibility settings for a viewpoint."""
    topic = await _require_topic(project_id, guid)
    vp = next((v for v in topic.get("viewpoints", []) if v.get("guid") == vguid), None)
    if not vp:
        raise HTTPException(status_code=404, detail="Viewpoint not found")
    return vp.get("components", {}).get("visibility")


@router.get("/projects/{project_id}/topics/{guid}/viewpoints/{vguid}/bitmaps")
async def list_viewpoint_bitmaps(project_id: str, guid: str, vguid: str):
    """
    BCF 3.0 Section 3.5.4 — list bitmaps for a viewpoint.

    The BCF 3.0 spec also defines GET .../bitmaps/{bguid} (§3.5.5) for
    individual bitmap access, but that endpoint is not implemented because
    the BCF parser stores bitmaps without individual GUIDs — the Solibri
    export format does not assign GUIDs to bitmap entries.
    """
    topic = await _require_topic(project_id, guid)
    vp = next((v for v in topic.get("viewpoints", []) if v.get("guid") == vguid), None)
    if not vp:
        raise HTTPException(status_code=404, detail="Viewpoint not found")
    return vp.get("bitmaps", [])


# ── Comment Events for a single comment (Section 3.10.2) ─────────────────────

@router.get("/projects/{project_id}/topics/{guid}/comments/{cguid}/events")
async def list_comment_events_for_comment(
    project_id: str,
    guid: str,
    cguid: str,
    filter: str | None = Query(default=None, alias="$filter"),
):
    """
    BCF 3.0 Section 3.10.2 — get events for a single comment.

    Returns the audit history of this specific comment (created, text updated,
    viewpoint linked/unlinked).

    Supports OData $filter on `author`, `type` (matches if any action has
    that type), and `date` (eq/ne/gt/ge/lt/le).
    """
    await _require_topic(project_id, guid)
    events = await get_comment_events_for_project(project_id, topic_guid=guid, comment_guid=cguid)
    events = _filter_events(events, filter)
    return _normalize_dates(events)
