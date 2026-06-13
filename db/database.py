"""
MongoDB connection and all async database helpers for BCF data.

All topic documents are versioned — multiple documents share the same GUID,
distinguished by an integer version field. Helpers here always return the
latest version unless history is explicitly requested.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import motor.motor_asyncio
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is required but not set")
DB_NAME = "bcf2graphql"

db = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)[DB_NAME]


async def save_project(project: dict, filename: str) -> str:
    """
    Upsert a project into the projects collection.

    The project_id comes from the BCF file's Project element.  If the BCF
    file has no Project element (or no project_id attribute), the BCF
    filename stem is used as a stable fallback identifier.

    Returns the effective project_id that was stored.
    """
    project_id = project.get("projectId") or filename
    name = project.get("name") or filename

    await db.projects.update_one(
        {"projectId": project_id},
        {
            "$set":         {"name": name},
            "$setOnInsert": {"importedAt": datetime.now(timezone.utc).isoformat()},
            "$addToSet":    {"bcfSourceFiles": filename},
        },
        upsert=True,
    )
    return project_id


async def get_projects() -> list:
    """Return all stored projects."""
    cursor = db.projects.find({}, {"_id": 0})
    return await cursor.to_list(length=None)


async def get_project(project_id: str) -> dict | None:
    """Return a single project by its project_id."""
    return await db.projects.find_one({"projectId": project_id}, {"_id": 0})


async def get_topics_for_project(project_id: str) -> list:
    """Get the latest version of every topic that belongs to a project."""
    pipeline = [
        {"$match": {"projectId": project_id}},
        {"$sort": {"version": -1}},
        {"$group": {"_id": "$guid", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {"_id": 0}},
    ]
    return await db.topics.aggregate(pipeline).to_list(length=None)


async def get_topic_for_project(project_id: str, guid: str) -> dict | None:
    """Get the latest version of a single topic within a project."""
    return await db.topics.find_one(
        {"projectId": project_id, "guid": guid},
        {"_id": 0},
        sort=[("version", -1)],
    )


async def save_bcf(filename: str, data: dict, project_id: str = None):
    """
    Import a parsed BCF file into MongoDB.

    Each topic is tagged with project_id so it can be retrieved through
    the /bcf/3.0/projects/{project_id}/topics/... hierarchy.

    Matching logic:
    - Each topic is identified by its BCF GUID.
    - If a topic with the same GUID already exists, compare modifiedDate:

    Scenario 1 — modifiedDate is the SAME:
        The topic content has not changed. Only append any new File entries
        from the incoming <Header><Files> that are not already stored.
        No new document is created.

    Scenario 2 — modifiedDate is DIFFERENT (or topic is new):
        The topic has been edited. Insert a new document with an incremented
        version number, containing the full topic data from this import.
    """
    inserted = 0
    skipped = 0
    files_appended = 0

    for topic in data.get("topics", []):
        guid = topic.get("guid")
        modified_date = topic.get("modifiedDate")
        new_files = topic.get("files", [])

        # Find the latest stored version of this topic
        existing = await db.topics.find_one(
            {"guid": guid},
            sort=[("version", -1)]
        )

        if existing:
            existing_md = existing.get("modifiedDate")
            existing_md_str = existing_md.get("ISO8601") if isinstance(existing_md, dict) else existing_md
            md_str = modified_date.get("ISO8601") if isinstance(modified_date, dict) else modified_date

            if existing_md_str == md_str:
                # ── Scenario 1: same content ──
                existing_files = existing.get("files", [])
                existing_filenames = {f.get("fileName") for f in existing_files}
                files_to_add = [
                    f for f in new_files
                    if f.get("fileName") not in existing_filenames
                ]

                update_ops = {}

                # Append new File entries if any
                if files_to_add:
                    update_ops.setdefault("$push", {})["files"] = {"$each": files_to_add}
                    files_appended += len(files_to_add)

                # Track source BCF file
                if filename not in (existing.get("bcfSourceFiles") or []):
                    update_ops.setdefault("$addToSet", {})["bcfSourceFiles"] = filename

                if update_ops:
                    await db.topics.update_one(
                        {"_id": existing["_id"]},
                        update_ops
                    )
                else:
                    skipped += 1

                continue

            # ── Scenario 2: modifiedDate changed, new version ──
            version_base = str(existing.get("version", "0")).lstrip("v")
            try:
                new_version = int(version_base) + 1
            except (ValueError, TypeError):
                new_version = 1
        else:
            new_version = 1

        doc = {**topic, "bcfSourceFiles": [filename], "version": new_version}
        if project_id is not None:
            doc["projectId"] = project_id
        await db.topics.insert_one(doc)
        inserted += 1

    logger.info("Inserted %d new versions, %d File entries appended, %d unchanged", inserted, files_appended, skipped)


async def get_topics() -> list:
    """Get the latest version of each topic."""
    pipeline = [
        {"$sort": {"version": -1}},
        {"$group": {
            "_id": "$guid",
            "doc": {"$first": "$$ROOT"}
        }},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {"_id": 0}}
    ]
    return await db.topics.aggregate(pipeline).to_list(length=None)


async def get_topic(guid: str) -> dict | None:
    """Get the latest version of a single topic."""
    return await db.topics.find_one(
        {"guid": guid},
        {"_id": 0},
        sort=[("version", -1)]
    )


async def get_topic_history(guid: str) -> list:
    """Get all versions of a topic, oldest first."""
    cursor = db.topics.find(
        {"guid": guid},
        {"_id": 0}
    ).sort("version", 1)
    return await cursor.to_list(length=None)


# ── Event helpers ─────────────────────────────────────────────────────────────

def _iso(date_field) -> str | None:
    if isinstance(date_field, dict):
        return date_field.get("ISO8601")
    return date_field


def _make_date(iso_str: str | None) -> dict | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return {"timestamp": dt.timestamp(), "ISO8601": iso_str}
    except Exception:
        return {"timestamp": 0.0, "ISO8601": iso_str}


def _generate_topic_events(versions: list) -> list:
    """
    Derive spec-compliant topic events (Section 3.9) from stored version history.

    Version 1 → topic_created + events for every initially-set field (per spec).
    Version N → events for each field that changed vs the previous version.
    """
    events = []
    for i, version in enumerate(versions):
        prev = versions[i - 1] if i > 0 else None
        actions = []

        if prev is None:
            actions.append({"type": "topic_created", "value": None})
            if version.get("title"):
                actions.append({"type": "title_updated", "value": version["title"]})
            if version.get("description"):
                actions.append({"type": "description_updated", "value": version["description"]})
            if version.get("topicStatus"):
                actions.append({"type": "status_updated", "value": version["topicStatus"]})
            if version.get("topicType"):
                actions.append({"type": "type_updated", "value": version["topicType"]})
            if version.get("priority"):
                actions.append({"type": "priority_updated", "value": version["priority"]})
            if version.get("dueDate"):
                actions.append({"type": "due_date_updated", "value": _iso(version["dueDate"])})
            if version.get("assignedTo"):
                actions.append({"type": "assigned_to_updated", "value": version["assignedTo"]})
            if version.get("stage"):
                actions.append({"type": "stage_added", "value": version["stage"]})
            for label in (version.get("labels") or []):
                actions.append({"type": "label_added", "value": label})
            date_val = version.get("creationDate")
            author   = version.get("creationAuthor")
        else:
            if version.get("title") != prev.get("title"):
                actions.append({"type": "title_updated", "value": version.get("title")})

            prev_desc, curr_desc = prev.get("description"), version.get("description")
            if curr_desc != prev_desc:
                actions.append({"type": "description_updated" if curr_desc else "description_removed",
                                 "value": curr_desc})

            if version.get("topicStatus") != prev.get("topicStatus"):
                actions.append({"type": "status_updated", "value": version.get("topicStatus")})

            if version.get("topicType") != prev.get("topicType"):
                actions.append({"type": "type_updated", "value": version.get("topicType")})

            prev_prio, curr_prio = prev.get("priority"), version.get("priority")
            if curr_prio != prev_prio:
                actions.append({"type": "priority_updated" if curr_prio else "priority_removed",
                                 "value": curr_prio})

            prev_due, curr_due = _iso(prev.get("dueDate")), _iso(version.get("dueDate"))
            if curr_due != prev_due:
                actions.append({"type": "due_date_updated" if curr_due else "due_date_removed",
                                 "value": curr_due})

            prev_assigned, curr_assigned = prev.get("assignedTo"), version.get("assignedTo")
            if curr_assigned != prev_assigned:
                actions.append({"type": "assigned_to_updated" if curr_assigned else "assigned_to_removed",
                                 "value": curr_assigned})

            prev_stage, curr_stage = prev.get("stage"), version.get("stage")
            if curr_stage != prev_stage:
                if not prev_stage and curr_stage:
                    actions.append({"type": "stage_added",   "value": curr_stage})
                elif prev_stage and curr_stage:
                    actions.append({"type": "stage_updated", "value": curr_stage})
                else:
                    actions.append({"type": "stage_removed", "value": None})

            prev_labels = set(prev.get("labels") or [])
            curr_labels = set(version.get("labels") or [])
            for added   in sorted(curr_labels - prev_labels):
                actions.append({"type": "label_added",   "value": added})
            for removed in sorted(prev_labels - curr_labels):
                actions.append({"type": "label_removed", "value": removed})

            date_val = version.get("modifiedDate")
            author   = version.get("modifiedAuthor")

        if actions:
            events.append({
                "topicGuid": version.get("guid"),
                "date":      _make_date(_iso(date_val)) or date_val,
                "author":    author or "",
                "actions":   actions,
            })

    return events


def _generate_comment_events(versions: list) -> list:
    """
    Derive spec-compliant comment events (Section 3.10) from stored version history.

    A comment appearing for the first time → comment_created.
    A changed comment text  → comment_text_updated.
    A changed viewpointGuid → viewpoint_updated / viewpoint_removed.
    """
    all_events = []
    for i, version in enumerate(versions):
        prev = versions[i - 1] if i > 0 else None
        prev_comments = (
            {c["guid"]: c for c in (prev.get("comments") or []) if c.get("guid")}
            if prev else {}
        )
        curr_comments = {c["guid"]: c for c in (version.get("comments") or []) if c.get("guid")}

        for c_guid, comment in curr_comments.items():
            actions = []
            if c_guid not in prev_comments:
                actions.append({"type": "comment_created", "value": None})
            else:
                prev_c = prev_comments[c_guid]
                if comment.get("comment") != prev_c.get("comment"):
                    actions.append({"type": "comment_text_updated", "value": comment.get("comment")})
                prev_vp = prev_c.get("viewpointGuid")
                curr_vp = comment.get("viewpointGuid")
                if curr_vp != prev_vp:
                    actions.append({"type": "viewpoint_updated" if curr_vp else "viewpoint_removed",
                                     "value": curr_vp})

            if actions:
                date_val = comment.get("date")
                all_events.append({
                    "commentGuid": c_guid,
                    "topicGuid":   version.get("guid"),
                    "date":        _make_date(_iso(date_val)) or date_val,
                    "author":      comment.get("author") or "",
                    "actions":     actions,
                })

    return all_events


async def get_topic_events_for_project(project_id: str, topic_guid: str = None) -> list:
    """Return spec-compliant TopicEvents derived from stored version history."""
    query = {"projectId": project_id}
    if topic_guid:
        query["guid"] = topic_guid

    all_versions = await db.topics.find(query, {"_id": 0}).sort(
        [("guid", 1), ("version", 1)]
    ).to_list(length=None)

    all_events = []
    current_guid = None
    group: list = []
    for v in all_versions:
        if v["guid"] != current_guid:
            if group:
                all_events.extend(_generate_topic_events(group))
            current_guid = v["guid"]
            group = [v]
        else:
            group.append(v)
    if group:
        all_events.extend(_generate_topic_events(group))
    return all_events


async def get_comment_events_for_project(
    project_id: str,
    topic_guid: str = None,
    comment_guid: str = None,
) -> list:
    """Return spec-compliant CommentEvents derived from stored version history."""
    query = {"projectId": project_id}
    if topic_guid:
        query["guid"] = topic_guid

    all_versions = await db.topics.find(query, {"_id": 0}).sort(
        [("guid", 1), ("version", 1)]
    ).to_list(length=None)

    all_events = []
    current_guid = None
    group: list = []
    for v in all_versions:
        if v["guid"] != current_guid:
            if group:
                events = _generate_comment_events(group)
                if comment_guid:
                    events = [e for e in events if e.get("commentGuid") == comment_guid]
                all_events.extend(events)
            current_guid = v["guid"]
            group = [v]
        else:
            group.append(v)
    if group:
        events = _generate_comment_events(group)
        if comment_guid:
            events = [e for e in events if e.get("commentGuid") == comment_guid]
        all_events.extend(events)
    return all_events


async def get_topics_for_ifc_element(global_id: str) -> list:
    """
    Find all BCF topics that reference a given IFC element GlobalId
    in any of their viewpoint components (selection list).

    Returns the latest version of each matching topic.
    """
    pipeline = [
        # Match topics where at least one viewpoint component selection
        # contains a component with this ifcGuid.
        {"$match": {
            "viewpoints.components.selection.ifcGuid": global_id
        }},
        # Deduplicate: keep only the latest version of each topic guid.
        {"$sort": {"version": -1}},
        {"$group": {
            "_id":  "$guid",
            "doc":  {"$first": "$$ROOT"}
        }},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$project": {"_id": 0}},
    ]
    return await db.topics.aggregate(pipeline).to_list(length=None)