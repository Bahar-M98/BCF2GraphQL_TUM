"""
resolvers/query.py — BCF query resolvers.

All BCF data lives in MongoDB Atlas (imported via import_bcf.py).
get_topics() already deduplicates to the latest version of each topic
using a MongoDB aggregation, so every resolver here works on clean data.
"""

from db.database import (
    get_topics,
    get_topics_for_project,
    get_topic,
    get_topic_history,
    get_topic_events_for_project,
    get_comment_events_for_project,
)


async def resolve_project(obj, info):
    topics = await get_topics()
    return {
        # BCF 3.0 files do not carry a stable project identifier that maps
        # cleanly to a single string — the spec allows it but BIMplus exports
        # omit it.  None is the honest value; the schema field is nullable.
        "projectId": None,
        "name":      "BCF Project",
        "topics":    topics
    }


async def resolve_topics(obj, info, projectId=None, topicStatus=None, topicType=None, assignedTo=None, title=None):
    topics = await get_topics_for_project(projectId) if projectId else await get_topics()

    # Filters are applied in Python rather than as MongoDB query predicates.
    # For a thesis dataset (tens to low hundreds of topics) this is fast enough
    # and keeps the DB layer simple.  If the dataset grows, push these into
    # get_topics() as match arguments.
    if topicStatus:
        topics = [t for t in topics if t.get("topicStatus") == topicStatus]
    if topicType:
        topics = [t for t in topics if t.get("topicType") == topicType]
    if assignedTo:
        topics = [t for t in topics if t.get("assignedTo") == assignedTo]
    if title:
        topics = [t for t in topics if t.get("title") == title]

    return topics


async def resolve_topic(obj, info, guid: str):
    return await get_topic(guid)


async def resolve_topic_history(obj, info, guid: str):
    # Returns every stored version of the topic in insertion order so callers
    # can diff consecutive versions (used by resolve_topic_timeline).
    return await get_topic_history(guid)


async def resolve_topic_events(obj, info, projectId: str, topicGuid: str = None):
    """BCF 3.0 Section 3.9 — topic events derived from stored version history."""
    return await get_topic_events_for_project(projectId, topic_guid=topicGuid)


async def resolve_comment_events(obj, info, projectId: str, topicGuid: str = None, commentGuid: str = None):
    """BCF 3.0 Section 3.10 — comment events derived from stored version history."""
    return await get_comment_events_for_project(projectId, topic_guid=topicGuid, comment_guid=commentGuid)
