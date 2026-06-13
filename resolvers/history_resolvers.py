"""
Resolvers for IFC version matching, topic timeline, element version history,
and topics-for-element queries. IFC version data comes from ifcs/ on disk;
BCF data comes from MongoDB.
"""

import asyncio
import logging
from datetime import datetime

from ifc_reader import (
    list_versions,
    match_version,
    extract_header_hints,
    extract_elements_by_guids,
)
from db.database import get_topic, get_topic_history, db

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date_to_iso(date_field: dict | str | None) -> str | None:
    """Pull the ISO8601 string out of a Date dict or return None."""
    if isinstance(date_field, dict):
        return date_field.get("ISO8601")
    return date_field


def _match_for_topic(topic: dict, event_time_iso: str | None, versions: list[dict]) -> dict | None:
    """Run version matching for a topic + event timestamp against a pre-loaded version list."""
    guids, names = extract_header_hints(topic)
    file_refs = topic.get("files") or []
    ts = event_time_iso or _date_to_iso(topic.get("creationDate"))
    if not ts:
        return None
    return match_version(versions, ts, guids, names, file_refs=file_refs)


# ── Feature 1 ─────────────────────────────────────────────────────────────────

async def resolve_ifc_versions(obj, info, ifcProjectGuid: str = None):
    """List all IFC versions from disk, optionally filtered by project GUID.

    asyncio.to_thread is used because list_versions opens every IFC file in
    ifcs/ with ifcopenshell (synchronous/CPU-bound).  Running it directly on
    the event loop would stall every other in-flight GraphQL request.
    """
    return await asyncio.to_thread(list_versions, ifc_project_guid=ifcProjectGuid)


async def resolve_ifc_version_for_event(obj, info, topicGuid: str, eventTime: str = None):
    """
    Feature 1: find the best matching IFC version for a BCF topic event.

    eventTime — ISO 8601 string. Omit to use the topic's creation date.
    Returns IfcVersionMatch { version, inferred }.

    Versions are loaded without a project GUID filter here because the topic's
    BCF header may reference a project that has since been renamed or moved;
    filtering by GUID too early would eliminate valid Tier-2/3 fallbacks.
    """
    topic = await get_topic(topicGuid)
    if not topic:
        return None
    versions = await asyncio.to_thread(list_versions)
    return _match_for_topic(topic, eventTime, versions)


# ── Feature 2a — topic timeline ───────────────────────────────────────────────

async def resolve_topic_timeline(obj, info, topicGuid: str):
    """
    Feature 2: BCF event timeline for a topic.

    Returns a chronological list of BCFEvents (CREATION, COMMENT,
    STATUS_CHANGE, MODIFICATION) each stamped with the IFC version that
    was active on disk at that moment.

    DESIGN NOTE — BCFEvent differs from the spec's topic_event_GET and
    comment_event_GET (BCF 3.0 Sections 3.9 / 3.10) in three ways:

      1. UNIFIED — merges topic + comment events into one sorted stream.
         The spec exposes them at two separate endpoints.

      2. IFC-VERSION-STAMPED — each event carries the IfcVersion active on
         disk at that moment (3-tier matching).  The spec has no such concept.

      3. SEMANTIC TYPES — four coarse types replace the spec's 17+4 granular
         field-level types:
           CREATION      → topic.creationDate / creationAuthor
           COMMENT       → each entry in topic.comments[]
           STATUS_CHANGE → topicStatus differs between consecutive versions
           MODIFICATION  → new version exists but status did not change

    Building this list requires merging three data sources:
      1. the latest topic document       (MongoDB)
      2. all historical topic versions   (MongoDB)
      3. all IFC files on disk           (ifcs/ directory)

    A REST client reproducing this output needs 2 + N round trips per topic:
      GET /topics/{guid}/events              (spec Section 3.9)
      GET /topics/{guid}/comments/events     (spec Section 3.10)
      For each N events: IFC version lookup
    This resolver does it in one GraphQL query — the core N+1 thesis finding.

    Implementation notes:
    - Versions are loaded once per call (not per event) to avoid
      multiplying the ifcopenshell disk I/O cost.
    - History is fetched upfront so each event is matched against the BCF
      header (files) that was current when the event occurred, not the
      latest header.  Using the latest topic for CREATION/COMMENT events
      would produce wrong IFC matches if the BCF was reimported with
      updated file references.
    - Comments are deduplicated by guid across topic versions so a
      comment that appears in multiple stored versions is emitted once.
    - Events are sorted by unix timestamp (not ISO string) to handle
      mixed timezone offsets correctly.
    """
    topic = await get_topic(topicGuid)
    if not topic:
        return []

    # Load versions once for the entire call.  list_versions opens every IFC
    # file on disk (synchronous); loading inside the per-event loop would
    # multiply that I/O cost by the number of events.
    versions = await asyncio.to_thread(list_versions)

    # Fetch history upfront so CREATION and COMMENT events can use the BCF
    # header (files field) from the version that was current when each event
    # occurred, rather than the latest version's header.
    history = await get_topic_history(topicGuid)
    first_version = history[0] if history else topic

    # Map each comment GUID to the earliest history version that contains it.
    # This lets COMMENT events be matched to the IFC file referenced in the
    # BCF header at the time the comment was first added.
    comment_version_map: dict[str, dict] = {}
    for h in history:
        for c in h.get("comments", []):
            c_guid = c.get("guid")
            if c_guid and c_guid not in comment_version_map:
                comment_version_map[c_guid] = h

    events = []

    # ── CREATION ──────────────────────────────────────────────────────────────
    # Use the first stored version so the file refs match the original BCF header.
    creation_ts = _date_to_iso(first_version.get("creationDate"))
    events.append({
        "eventType": "CREATION",
        "timestamp": first_version.get("creationDate"),
        "author":    first_version.get("creationAuthor"),
        "detail":    first_version.get("title"),
        "ifcVersion": _match_for_topic(first_version, creation_ts, versions),
    })

    # ── COMMENTS — deduplicated by comment guid across versions ───────────────
    seen_comment_guids: set[str] = set()
    for comment in topic.get("comments", []):
        c_guid = comment.get("guid")
        if c_guid and c_guid in seen_comment_guids:
            continue
        if c_guid:
            seen_comment_guids.add(c_guid)
        ts = _date_to_iso(comment.get("date"))
        # Use the history version where this comment first appeared so the
        # IFC match reflects the BCF header that was current at that time.
        comment_topic = comment_version_map.get(c_guid, first_version) if c_guid else first_version
        events.append({
            "eventType": "COMMENT",
            "timestamp": comment.get("date"),
            "author":    comment.get("author"),
            "detail":    comment.get("comment"),
            "ifcVersion": _match_for_topic(comment_topic, ts, versions),
        })

    # ── STATUS_CHANGE + MODIFICATION — compare consecutive topic versions ──────
    for i in range(1, len(history)):
        prev = history[i - 1]
        curr = history[i]
        ts   = _date_to_iso(curr.get("modifiedDate"))

        if prev.get("topicStatus") != curr.get("topicStatus"):
            events.append({
                "eventType": "STATUS_CHANGE",
                "timestamp": curr.get("modifiedDate"),
                "author":    curr.get("modifiedAuthor"),
                "detail":    curr.get("topicStatus"),
                "ifcVersion": _match_for_topic(curr, ts, versions),
            })
        elif ts:
            # Only emit a MODIFICATION event when a timestamp exists.
            # Without ts we cannot sort the event or link it to an IFC version,
            # so emitting it would produce an unsortable, unanchored record.
            events.append({
                "eventType": "MODIFICATION",
                "timestamp": curr.get("modifiedDate"),
                "author":    curr.get("modifiedAuthor"),
                "detail":    curr.get("title"),
                "ifcVersion": _match_for_topic(curr, ts, versions),
            })

    # Sort chronologically.
    # MongoDB stores BCF dates as { "ISO8601": "...", "timestamp": <unix-s> }.
    # We sort on the numeric unix value rather than the ISO string because
    # string comparison of ISO 8601 dates with mixed timezone offsets can
    # produce wrong ordering; the integer epoch is always UTC and unambiguous.
    # Events without a timestamp sort to epoch 0 (front of list).
    def _sort_key(e):
        ts = e.get("timestamp")
        return ts.get("timestamp", 0) if isinstance(ts, dict) else 0

    events.sort(key=_sort_key)
    return events


# ── Feature 2b — element version history ─────────────────────────────────────

def _open_and_extract(file_path: str, global_id: str) -> dict | None:
    """Synchronous IFC read — called inside asyncio.to_thread to avoid blocking.

    Isolated into its own function so that a corrupt or unreadable IFC file
    returns None for that version rather than aborting the entire history
    query.  The calling resolver maps None → ElementVersion.element = null,
    which is the correct schema representation of "not present in this version".
    """
    try:
        results = extract_elements_by_guids(file_path, [global_id])
        return results.get(global_id)
    except Exception as e:
        logger.warning("Could not read %s: %s", file_path, e)
        return None


async def resolve_element_version_history(obj, info, globalId: str, ifcProjectGuid: str):
    """
    Feature 2: show how an IFC element changed across model versions.

    For each version of the given project, opens the IFC file on disk (in a
    thread) and extracts the element by globalId. Returns every version —
    element is None for versions where the GUID was not yet present.
    """
    ifc_versions = await asyncio.to_thread(list_versions, ifc_project_guid=ifcProjectGuid)
    result = []

    for ifc_ver in ifc_versions:
        file_path = ifc_ver.get("filePath")
        element = None
        if file_path:
            element = await asyncio.to_thread(_open_and_extract, file_path, globalId)
        result.append({"version": ifc_ver, "element": element})

    return result


# ── Feature 3 — topics for an element, flat list ─────────────────────────────

async def resolve_topics_for_element(
    obj, info,
    globalId: str,
    ifcProjectGuid: str = None,
    fileName: str = None,
    before: str = None,
    includeHistory: bool = False,
):
    """
    Feature 3: BCF topics referencing an IFC element, as a flat list.

    By default returns only the latest version of each matching topic.
    Pass includeHistory: true to get ALL stored versions of each topic
    (oldest first), so you can see how the topic changed over time.

    Optional precision filters (Direction 2 of the BCF↔IFC concept):
      ifcProjectGuid  — only topics whose BCF header referenced this IFC project.
      fileName        — further narrows to a specific IFC filename in the header.
      before          — ISO 8601 string; only topics created at or before this time.
      includeHistory  — when true, return all versions instead of just the latest.
    """
    # Base match — must happen before dedup so it applies to all versions.
    base_match = {"$match": {"viewpoints.components.selection.ifcGuid": globalId}}

    if includeHistory:
        # Return every stored version of each matching topic, oldest first.
        # Skip the $group deduplication so all versions pass through.
        pipeline = [
            base_match,
            {"$sort": {"guid": 1, "version": 1}},
        ]
    else:
        # Default: deduplicate — keep only the latest version of each topic.
        pipeline = [
            base_match,
            {"$sort": {"version": -1}},
            {"$group": {
                "_id": "$guid",
                "doc": {"$first": "$$ROOT"},
            }},
            {"$replaceRoot": {"newRoot": "$doc"}},
        ]

    # Precision filters — applied after dedup (or after base match for history).
    if ifcProjectGuid:
        # Include topics that match the project GUID, plus topics that don't
        # specify any project GUID (the BCF IfcProject header field is optional;
        # many tools omit it, so a null value means "unspecified", not "different project").
        pipeline.append({"$match": {"$or": [
            {"files.ifcProjectGuid": ifcProjectGuid},
            {"files.ifcProjectGuid": None},
        ]}})

    if fileName:
        pipeline.append({"$match": {"files.fileName": fileName}})

    if before:
        try:
            epoch = datetime.fromisoformat(before.replace("Z", "+00:00")).timestamp()
            pipeline.append({"$match": {"creationDate.timestamp": {"$lte": epoch}}})
        except ValueError:
            pass

    return await db.topics.aggregate(pipeline).to_list(length=None)
