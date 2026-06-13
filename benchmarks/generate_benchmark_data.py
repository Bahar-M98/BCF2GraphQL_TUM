"""
generate_benchmark_data.py — Insert synthetic BCF benchmark datasets into MongoDB.

Creates three project tiers, each with a clean projectId so benchmark scripts
can target them independently:

  benchmark_small   —  25 topics, 3 comments/topic, 1 viewpoint/topic
  benchmark_medium  — 100 topics, 5 comments/topic, 1 viewpoint/topic
  benchmark_large   — 500 topics, 8 comments/topic, 1 viewpoint/topic

Run:
  uv run python generate_benchmark_data.py

Re-running drops and recreates all benchmark data — fully idempotent.
"""

import asyncio
import math
import random
import uuid
from datetime import datetime, timezone, timedelta

import motor.motor_asyncio

# ── Connection (reuse credentials from db/database.py) ────────────────────────
import os
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is required but not set")
DB_NAME   = "bcf2graphql"
db = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)[DB_NAME]

# ── BCF vocabulary ─────────────────────────────────────────────────────────────
TOPIC_TYPES    = ["Error", "Warning", "Request", "Info"]
TOPIC_STATUSES = ["Open", "In Progress", "Resolved", "Closed"]
PRIORITIES     = ["Critical", "Major", "Normal", "Minor"]
STAGES         = ["Schematic Design", "Design Development", "Construction Documents", "Construction", "Handover"]
LABELS         = ["clash", "coordination", "fire-safety", "structure", "MEP", "facade", "accessibility", "code-compliance"]

AUTHORS = [
    "Bahar.moradi@tum.de",
    "Alyssa@tum.de",
    "Nepomuk@tum.de",
    "Bekky@tum.de",
    "Jonas@tum.de",
    "Moriz@tum.de",
]
ASSIGNEES = ["ARC", "STR", "MEP", "COORD", "PM"]

# IFC discipline definitions used to build the per-tier file pool.
# Each discipline has one stable project GUID (assigned at tier generation time)
# and one or two export versions — matching real IFC workflows where the project
# GUID never changes but the file is re-exported as the design evolves.
IFC_DISCIPLINES = [
    ("ArchitecturalModel", ["v1", "v2"]),
    ("StructuralModel",    ["v1", "v2"]),
    ("MEP_Coordination",   ["v1", "v2"]),
    ("FacadeLayout",       ["v1", "v2"]),
    ("SiteModel",          ["v1"]),
]
# Produces 9 (ifcProjectGuid, fileName) pairs with 5 unique GUIDs per tier.

BCF_SOURCE_FILES = [
    "BIMcollab_export_2025-Q1.bcf",
    "Revit_coordination_round2.bcf",
    "Navisworks_clash_report_v3.bcf",
    "OpenBIM_review_final.bcf",
]

ORIGINATING_SYSTEMS = [
    "Autodesk Revit 2025 (ENU)",
    "Autodesk Revit 2024 (ENU)",
    "ARCHICAD 27",
    "Tekla Structures 2024",
    "Vectorworks 2024",
]

# Title generation — stems combined with a location tag to ensure every topic
# gets a unique title even at 500 topics.
# stem pool × level pool × grid pool = 30 × 8 × 48 = 11 520 combinations.
_TITLE_STEMS = [
    "Clash detected between beam and duct",
    "Wall thickness does not match specification",
    "Missing fire rating on partition",
    "Door swing conflicts with egress path",
    "Column alignment offset exceeds tolerance",
    "Ceiling height below minimum clearance",
    "Railing height non-compliant with DIN 18065",
    "Window sill too low — fall protection required",
    "Staircase headroom insufficient",
    "HVAC penetration not coordinated with structure",
    "Structural opening missing in shear wall",
    "Curtain wall mullion spacing incorrect",
    "Roof drainage slope insufficient",
    "Facade panel gap too wide",
    "Slab edge detail missing at expansion joint",
    "Mechanical room access door undersized",
    "Sprinkler head conflicts with beam flange",
    "Electrical conduit crosses fire compartment boundary",
    "Toilet room ventilation duct missing",
    "Handrail bracket clashes with wall finish",
    "Parking ramp gradient exceeds maximum",
    "Emergency exit sign obscured by ductwork",
    "Pile cap geometry does not match structural drawing",
    "Glazing U-value does not meet energy code",
    "Waterproofing layer missing at terrace edge",
    "Elevator shaft dimensions below manufacturer minimum",
    "Acoustic partition not full-height to slab",
    "Load-bearing wall removed without structural review",
    "Ventilation grille location conflicts with furniture layout",
    "Floor level mismatch between adjacent zones",
]
_LEVELS = [f"Level {n}" for n in range(1, 9)]
_GRIDS  = [f"Grid {c}-{n}" for c in "ABCDEF" for n in range(1, 9)]


def make_title() -> str:
    return f"{random.choice(_TITLE_STEMS)} — {random.choice(_LEVELS)}, {random.choice(_GRIDS)}"

DESCRIPTIONS = [
    "Coordination issue identified during model review. Requires immediate attention from the responsible discipline.",
    "Non-conformance detected against project specification section 4.2. Please review and update the model.",
    "Issue flagged during clash detection run on {date}. Spatial conflict requires resolution before next submission.",
    "Design deviation from approved drawings. Refer to drawing reference for correct dimensions.",
    "Regulatory compliance issue — must be resolved before permit application.",
    "Detected during BIM coordination meeting. Action required by assigned team.",
    "Reported by site team during construction phase review.",
    "Flagged by structural engineer during interdisciplinary review.",
]

COMMENTS_TEXT = [
    "Confirmed — needs immediate coordination with the responsible team.",
    "Reviewed on site, issue persists. Escalating to lead engineer.",
    "Assigned to structural team for resolution. Expected response by end of week.",
    "Updated model uploaded to shared drive, please verify and close if resolved.",
    "Waiting for client approval before proceeding with the proposed solution.",
    "This was discussed in last Tuesday's coordination meeting — minutes attached.",
    "Temporary workaround applied in construction. Permanent fix required for handover.",
    "Fixed in revision 3 of the architectural drawings. Please verify in model.",
    "Cannot reproduce in current model version — please provide more detail or screenshot.",
    "Escalated to lead engineer. Decision expected within 48 hours.",
    "RFI submitted to contractor. Awaiting response.",
    "Issue noted. Will be addressed in the next model update scheduled for Friday.",
    "Clash accepted by structural engineer — clearance is sufficient per updated calculation.",
    "Responsibility transferred to MEP team. Please update your model and recheck.",
    "Partially resolved — duct rerouted but beam connection still requires review.",
    "Client has approved the proposed deviation. Updating drawing register.",
    "Site instruction issued. Construction team notified.",
    "Model updated per coordination comment. Reopening for verification.",
    "Second review completed — issue confirmed as critical. Do not proceed until resolved.",
    "Documentation attached. Refer to email thread from 2025-03-12 for background.",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def new_guid() -> str:
    return str(uuid.uuid4())


def make_date(dt: datetime) -> dict:
    return {"timestamp": dt.timestamp(), "ISO8601": dt.isoformat()}


def random_date(start: datetime, end: datetime) -> datetime:
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def make_ifc_guid() -> str:
    """22-character compressed IFC GUID — correct charset and length per IFC spec."""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"
    return "".join(random.choices(chars, k=22))


def make_vector(scale: float = 10.0) -> dict:
    return {
        "x": round(random.uniform(0, scale), 6),
        "y": round(random.uniform(0, scale), 6),
        "z": round(random.uniform(0, 5), 6),
    }


def make_unit_vector() -> dict:
    x, y, z = random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(-1, 1)
    length   = math.sqrt(x**2 + y**2 + z**2) or 1
    return {"x": round(x / length, 6), "y": round(y / length, 6), "z": round(z / length, 6)}


# ── Builder functions ──────────────────────────────────────────────────────────

def make_comment(topic_guid: str, base_time: datetime, index: int, vp_guids: list[str]) -> dict:
    author = random.choice(AUTHORS)
    date   = base_time + timedelta(hours=index * 3 + random.randint(1, 12))

    # ~35% of comments were later edited; edits are usually by the original author
    was_modified  = random.random() < 0.35
    modified_date = make_date(date + timedelta(hours=random.randint(1, 48))) if was_modified else None
    # 70% chance the editor is the same person who wrote it; 30% a different team member
    modified_auth = (author if random.random() < 0.70 else random.choice(AUTHORS)) if was_modified else None

    # ~40% of comments reference a viewpoint
    vp_guid = random.choice(vp_guids) if vp_guids and random.random() < 0.40 else None

    return {
        "guid":           new_guid(),
        "date":           make_date(date),
        "author":         author,
        "comment":        random.choice(COMMENTS_TEXT),
        "topicGuid":      topic_guid,
        "modifiedDate":   modified_date,
        "modifiedAuthor": modified_auth,
        "viewpointGuid":  vp_guid,
    }


def make_viewpoint(ifc_guids: list[str], guid: str, n_elements: int = None) -> dict:
    orig_sys  = random.choice(ORIGINATING_SYSTEMS)

    # n_elements=None → realistic random count (used by the topic-scaling tiers).
    # n_elements=N   → exactly N elements (used by the IFC-scaling tiers so that
    #                   element count is the only variable between those datasets).
    if n_elements is not None:
        n_selected = min(n_elements, len(ifc_guids))
    else:
        # Real BCF viewpoints typically highlight 1–6 elements.
        # Clash detection issues most often involve exactly 2 (the two clashing elements).
        # Weights: 1→10%, 2→35%, 3→28%, 4→16%, 5→7%, 6→4%
        n_selected = random.choices([1, 2, 3, 4, 5, 6], weights=[10, 35, 28, 16, 7, 4])[0]
        n_selected = min(n_selected, len(ifc_guids))
    selected   = random.sample(ifc_guids, n_selected)
    selection  = [
        {
            "ifcGuid":           g,
            "originatingSystem": orig_sys,
            "authoringToolId":   str(random.randint(100000, 999999)),
        }
        for g in selected
    ]

    # Coloring: 50% chance of highlighting the selected elements in a meaningful colour
    argb_colors = ["FF0000FF", "FFFF0000", "FF00FF00", "FFFFFF00", "FFFF8800"]
    if random.random() < 0.50 and selected:
        color_components = [{"ifcGuid": g, "originatingSystem": orig_sys, "authoringToolId": ""} for g in selected[:2]]
        coloring = [{"color": random.choice(argb_colors), "components": color_components}]
    else:
        coloring = []

    # Visibility: 80% default-visible; when hidden, a subset of elements is the exception
    default_vis = random.random() < 0.80
    exceptions  = (
        [{"ifcGuid": g, "originatingSystem": orig_sys, "authoringToolId": ""} for g in selected[:1]]
        if not default_vis and selected else []
    )

    # Clipping plane: 25% chance — common for coordination sections
    clipping_planes = []
    if random.random() < 0.25:
        clipping_planes = [{
            "location":  make_vector(scale=20.0),
            "direction": make_unit_vector(),
        }]

    # Snapshot: 30% of viewpoints have a captured screenshot (type only; no binary data)
    snapshot = {"snapshotType": random.choice(["PNG", "JPG"]), "snapshotData": None} if random.random() < 0.30 else None

    return {
        "guid":      guid,
        "viewpoint": f"{guid}.bcfv",
        "snapshot":  snapshot,
        "index":     1,
        "components": {
            "selection":  selection,
            "visibility": {
                "defaultVisibility": default_vis,
                "exceptions":        exceptions,
                "viewSetupHints": {
                    "spacesVisible":          random.random() < 0.15,
                    "spaceBoundariesVisible": False,
                    "openingsVisible":        random.random() < 0.30,
                },
            },
            "coloring": coloring,
        },
        "camera": {
            "__typename":      "PerspectiveCamera",
            "cameraViewPoint": make_vector(scale=30.0),
            "cameraDirection": make_unit_vector(),
            "cameraUpVector":  {"x": 0.0, "y": 0.0, "z": 1.0},
            "fieldOfView":     round(random.uniform(45.0, 90.0), 1),
            "aspectRatio":     round(random.uniform(1.33, 1.78), 3),
        },
        "lines":          [],
        "clippingPlanes": clipping_planes,
        "bitmaps":        [],
    }


def make_topic(
    project_id: str,
    topic_index: int,
    ifc_file_pool: list[tuple[str, str]],
    ifc_guids: list[str],
    n_comments: int,
    base_time: datetime,
    n_ifc_elements: int = None,
    n_viewpoints: int = 1,
) -> list[dict]:
    """
    Return a list of version documents for one topic (1–3 versions).

    Each version is a separate MongoDB document with the same guid and an
    incrementing version number — matching how import_bcf.py stores edits.
    Fields that are stable (topicType, title) are fixed at v1 and carried
    forward; fields that routinely change (topicStatus, assignedTo) may
    differ between versions, giving _generate_topic_events real diffs to
    emit STATUS_CHANGE / MODIFICATION events from.
    """
    topic_guid    = new_guid()
    creation_date = random_date(base_time, base_time + timedelta(days=30))
    author        = random.choice(AUTHORS)
    ifc_guid, ifc_name = random.choice(ifc_file_pool)

    # Stable fields — set once at v1, never re-randomized
    topic_type    = random.choice(TOPIC_TYPES)
    title         = make_title()
    priority      = random.choice(PRIORITIES)
    description   = random.choice(DESCRIPTIONS)
    due_offset    = timedelta(days=random.randint(7, 60)) if random.random() < 0.75 else None
    stage         = random.choice(STAGES) if random.random() < 0.35 else None
    server_id     = f"BCF-{topic_index:04d}" if random.random() < 0.60 else None
    labels        = random.sample(LABELS, random.randint(1, 2)) if random.random() < 0.50 else []

    # ifcSpatialStructureElement: 30% chance — links topic to a specific storey GUID
    ifc_spatial   = make_ifc_guid() if random.random() < 0.30 else None

    # Pre-generate viewpoint GUIDs so comments can reference them.
    vp_guids   = [new_guid() for _ in range(n_viewpoints)]
    comments   = [make_comment(topic_guid, creation_date, i, vp_guids) for i in range(n_comments)]
    viewpoints = [make_viewpoint(ifc_guids, g, n_ifc_elements) for g in vp_guids]

    # The BCF source file this topic was imported from — stable across versions
    bcf_source = random.choice(BCF_SOURCE_FILES)

    # Version count: 60% → 1, 30% → 2, 10% → 3
    n_versions       = random.choices([1, 2, 3], weights=[60, 30, 10])[0]
    current_status   = random.choice(TOPIC_STATUSES)
    current_assigned = random.choice(ASSIGNEES)
    modified_date    = creation_date + timedelta(hours=random.randint(1, 48))
    modifier         = author

    versions = []
    for v in range(1, n_versions + 1):
        if v > 1:
            modified_date    = modified_date + timedelta(hours=random.randint(24, 120))
            modifier         = random.choice(AUTHORS)
            current_status   = random.choice(TOPIC_STATUSES)
            current_assigned = random.choice(ASSIGNEES)
            # Rarely the priority is escalated on a later version
            if random.random() < 0.15:
                priority = random.choice(PRIORITIES)

        versions.append({
            "guid":               topic_guid,
            "serverAssignedId":   server_id,
            "topicType":          topic_type,
            "topicStatus":        current_status,
            "referenceLinks":     [],
            "title":              title,
            "priority":           priority,
            "index":              topic_index,
            "labels":             labels,
            "creationDate":       make_date(creation_date),
            "creationAuthor":     author,
            "modifiedDate":       make_date(modified_date),
            "modifiedAuthor":     modifier,
            "dueDate":            make_date(modified_date + due_offset) if due_offset else None,
            "assignedTo":         current_assigned,
            "description":        description,
            "stage":              stage,
            "bimSnippet":         None,
            "documentReferences": [],
            "relatedTopics":      [],
            "comments":           comments,
            "viewpoints":         viewpoints,
            "files": [
                {
                    "ifcProjectGuid":             ifc_guid,
                    "ifcSpatialStructureElement": ifc_spatial,
                    "isExternal":                 True,
                    "fileName":                   ifc_name,
                    "date":                       make_date(creation_date),
                    "reference":                  None,
                }
            ],
            "bcfSourceFiles": [bcf_source],
            "version":        v,
            "projectId":      project_id,
        })

    return versions


# ── Tier definitions ───────────────────────────────────────────────────────────

TIERS = [
    # ── Topic-scaling tiers (IFC element count random, topic count varies) ────
    {
        "project_id": "benchmark_small",
        "n_topics":   25,
        "n_comments": 3,
    },
    {
        "project_id": "benchmark_medium",
        "n_topics":   100,
        "n_comments": 5,
    },
    {
        "project_id": "benchmark_large",
        "n_topics":   500,
        "n_comments": 8,
    },
    # ── IFC-element-scaling tiers (topic count fixed, IFC element count varies) ─
    # Topic count is held at 50 and comments at 3 so the only variable between
    # these three datasets is the number of IFC elements referenced per viewpoint.
    # This lets the benchmark isolate element count as an independent variable
    # and plot latency/payload as a function of it, separate from topic-count scaling.
    {
        "project_id":     "benchmark_ifc_s1",
        "n_topics":       50,
        "n_comments":     3,
        "n_ifc_elements": 1,
    },
    {
        "project_id":     "benchmark_ifc_s3",
        "n_topics":       50,
        "n_comments":     3,
        "n_ifc_elements": 3,
    },
    {
        "project_id":     "benchmark_ifc_s5",
        "n_topics":       50,
        "n_comments":     3,
        "n_ifc_elements": 5,
    },
    # ── Viewpoint-scaling tiers (topic count fixed, viewpoint count varies) ───
    # Topic count is held at 50, comments at 3, and IFC elements at 2.
    # Only the number of viewpoints per topic changes: 1 → 3 → 5.
    # This isolates viewpoint count as an independent variable distinct from
    # both topic-count and element-count scaling.
    {"project_id": "benchmark_vp_v1", "n_topics": 50, "n_comments": 3, "n_viewpoints": 1, "n_ifc_elements": 2},
    {"project_id": "benchmark_vp_v3", "n_topics": 50, "n_comments": 3, "n_viewpoints": 3, "n_ifc_elements": 2},
    {"project_id": "benchmark_vp_v5", "n_topics": 50, "n_comments": 3, "n_viewpoints": 5, "n_ifc_elements": 2},
]


# ── Main ───────────────────────────────────────────────────────────────────────

async def generate_tier(tier: dict):
    project_id     = tier["project_id"]
    n_topics       = tier["n_topics"]
    n_comments     = tier["n_comments"]
    n_ifc_elements = tier.get("n_ifc_elements")   # None → random count per viewpoint
    n_viewpoints   = tier.get("n_viewpoints", 1)
    base_time      = datetime(2025, 1, 1, tzinfo=timezone.utc)

    # Build the IFC file pool for this tier.
    # Each discipline gets a fresh GUID (stable per discipline, as in real IFC),
    # then each of its export versions becomes a separate (GUID, fileName) entry.
    # Result: 9 pairs with 5 unique GUIDs — topics spread across disciplines and
    # across old/new versions of the same discipline model.
    ifc_file_pool = [
        (guid, f"{discipline}_{version}.ifc")
        for discipline, versions in IFC_DISCIPLINES
        for guid in [new_guid()]        # one GUID per discipline, shared across its versions
        for version in versions
    ]

    # Pool of IFC element GUIDs shared across all topics (simulates a real model)
    ifc_guids = [make_ifc_guid() for _ in range(50)]

    element_desc = f"{n_ifc_elements} fixed" if n_ifc_elements is not None else "varies (1–6, random)"
    print(f"\n── {project_id} ({'─' * 30})")
    print(f"   {n_topics} topics × {n_comments} comments × {n_viewpoints} viewpoint(s) × {element_desc} IFC elements")

    # Remove existing benchmark data for this project
    deleted = await db.topics.delete_many({"projectId": project_id})
    await db.projects.delete_many({"projectId": project_id})
    if deleted.deleted_count:
        print(f"   Removed {deleted.deleted_count} existing documents")

    # Insert project document
    await db.projects.insert_one({
        "projectId":      project_id,
        "name":           f"Benchmark — {project_id}",
        "bcfSourceFiles": BCF_SOURCE_FILES,
        "importedAt":     datetime.now(timezone.utc).isoformat(),
    })

    # Generate all version-lists, then flatten into a single list for bulk insert.
    # topic_index is passed so each topic gets a unique serverAssignedId (BCF-0001 etc.)
    # and a meaningful `index` field rather than always 0.
    topic_version_groups = [
        make_topic(project_id, i + 1, ifc_file_pool, ifc_guids, n_comments, base_time, n_ifc_elements, n_viewpoints)
        for i in range(n_topics)
    ]
    all_docs = [doc for group in topic_version_groups for doc in group]
    await db.topics.insert_many(all_docs)

    total_docs     = len(all_docs)
    total_comments = sum(len(d["comments"]) for d in all_docs if d["version"] == 1)
    total_elements = sum(
        len(vp["components"]["selection"])
        for d in all_docs if d["version"] == 1
        for vp in d["viewpoints"]
    )
    multi_version  = sum(1 for g in topic_version_groups if len(g) > 1)
    avg_elements   = round(total_elements / n_topics, 1)
    print(f"   Inserted {total_docs} documents ({n_topics} topics, {multi_version} with >1 version)")
    print(f"   {total_comments} comments · {n_topics} viewpoints · avg {avg_elements} IFC elements/viewpoint")
    print(f"   projectId = \"{project_id}\"")


async def main():
    print("BCF Benchmark Dataset Generator")
    print("=" * 40)
    random.seed(42)  # reproducible data across runs

    for tier in TIERS:
        await generate_tier(tier)

    print("\n" + "=" * 40)
    print("Done. Use these projectIds in your benchmark script:")
    for tier in TIERS:
        n = tier["n_topics"]
        c = tier["n_comments"]
        v = tier.get("n_viewpoints", 1)
        e = tier.get("n_ifc_elements")
        e_str = f"{e} IFC elements/viewpoint (fixed)" if e is not None else "IFC elements vary"
        print(f"  {tier['project_id']:<25} ({n} topics × {c} comments × {v} viewpoint(s), {e_str})")


if __name__ == "__main__":
    asyncio.run(main())
