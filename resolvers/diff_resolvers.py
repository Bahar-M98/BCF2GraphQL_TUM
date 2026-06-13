"""
GraphQL resolvers for IFC diff queries.

Resolves file-level and element-level diffs between two IFC versions
(`ifcFileDiff`, `ifcElementDiff`) plus the `IfcElement.diff` field. The
actual diff computation lives in `ifc_diff.py`; these resolvers only pick
the version pair to compare and adapt the result to the GraphQL schema.
"""

import asyncio

from ifc_reader import list_versions
from ifc_diff import compute_file_diff, compute_element_diff


def _pick_version_pair(versions, ifc_name_a, ifc_name_b):
    """Select the two IFC versions to compare, falling back to the last two if names are not given."""
    if len(versions) < 2:
        return None

    def find(name):
        # Return the first version whose fileName matches, or None
        return next((v for v in versions if v["fileName"] == name), None)

    ver_a = find(ifc_name_a) if ifc_name_a else versions[-2]  # second-to-last if not specified
    ver_b = find(ifc_name_b) if ifc_name_b else versions[-1]  # latest if not specified

    if ifc_name_a and ver_a is None:
        return None  # caller asked for a file that doesn't exist
    if ifc_name_b and ver_b is None:
        return None
    if ver_a["fileName"] == ver_b["fileName"]:
        return None  # comparing a file to itself is meaningless

    return ver_a, ver_b


async def resolve_ifc_file_diff(obj, info, ifcProjectGuid, ifcNameA=None, ifcNameB=None):
    """Resolver for ifcFileDiff — returns added/deleted/modified element lists for a whole model."""
    # Load all versions for this project from the ifcs/ directory
    versions = await asyncio.to_thread(list_versions, ifc_project_guid=ifcProjectGuid)
    pair = _pick_version_pair(versions, ifcNameA, ifcNameB)
    if pair is None:
        return None
    ver_a, ver_b = pair
    # Run the diff in a thread so it doesn't block the async event loop
    result = await asyncio.to_thread(compute_file_diff, ver_a["filePath"], ver_b["filePath"])
    result["versionA"] = ver_a
    result["versionB"] = ver_b
    return result


async def resolve_ifc_element_diff(obj, info, ifcProjectGuid, globalId, ifcNameA=None, ifcNameB=None):
    """Resolver for the top-level ifcElementDiff query — caller supplies the project GUID and element GUID."""
    versions = await asyncio.to_thread(list_versions, ifc_project_guid=ifcProjectGuid)
    pair = _pick_version_pair(versions, ifcNameA, ifcNameB)
    if pair is None:
        return None
    ver_a, ver_b = pair
    result = await asyncio.to_thread(
        compute_element_diff, ver_a["filePath"], ver_b["filePath"], globalId
    )
    if result is None:
        return None
    result["versionA"] = ver_a
    result["versionB"] = ver_b
    return result


async def resolve_ifc_element_diff_field(element, info, ifcNameA=None, ifcNameB=None):
    """Resolver for the diff field on IfcElement — project and GUID are taken from the parent element."""
    global_id = element.get("globalId")
    file_name = element.get("fileName")  # the file this element was resolved from
    if not global_id or not file_name:
        return None

    # Load all versions across all projects, then narrow to the same project as this element
    all_versions = await asyncio.to_thread(list_versions)
    this_ver = next((v for v in all_versions if v["fileName"] == file_name), None)
    if this_ver is None:
        return None

    # Only compare versions that belong to the same IFC project
    project_versions = [v for v in all_versions if v["id"] == this_ver["id"]]
    pair = _pick_version_pair(project_versions, ifcNameA, ifcNameB)
    if pair is None:
        return None

    ver_a, ver_b = pair
    result = await asyncio.to_thread(
        compute_element_diff, ver_a["filePath"], ver_b["filePath"], global_id
    )
    if result is None:
        return None
    result["versionA"] = ver_a
    result["versionB"] = ver_b
    return result
