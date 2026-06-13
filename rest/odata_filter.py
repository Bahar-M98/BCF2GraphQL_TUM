"""
Minimal OData $filter support for the BCF 3.0 Events services (Sections 3.9 and
3.10), e.g.:

    $filter=author eq 'Architect@example.com' and type eq 'status_updated' and date gt 2015-12-05T00:00:00+01:00

Supports the subset of the OData v4 grammar actually used by BCF clients:
clauses of the form `<field> <op> <value>` joined with `and`, where `<op>` is
one of eq, ne, gt, ge, lt, le.
"""

import re
from datetime import datetime

_OPS = {"eq", "ne", "gt", "ge", "lt", "le"}

_CLAUSE_RE = re.compile(
    r"(?P<field>\w+)\s+(?P<op>eq|ne|gt|ge|lt|le)\s+(?P<value>'[^']*'|\S+)",
    re.IGNORECASE,
)

# Event fields that are nested under each action rather than top-level.
_ACTION_FIELDS = {"type"}


def parse_filter(filter_str: str) -> list[tuple[str, str, str]]:
    """
    Parse an OData $filter string into a list of (field, op, value) clauses,
    combined with AND semantics.

    Raises ValueError if the filter string cannot be parsed.
    """
    clauses = []
    for raw_clause in re.split(r"\s+and\s+", filter_str.strip(), flags=re.IGNORECASE):
        raw_clause = raw_clause.strip()
        if not raw_clause:
            continue
        match = _CLAUSE_RE.fullmatch(raw_clause)
        if not match:
            raise ValueError(f"Unsupported $filter clause: {raw_clause!r}")
        field = match.group("field")
        op = match.group("op").lower()
        value = match.group("value")
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        clauses.append((field, op, value))
    return clauses


def _coerce(field: str, value: str):
    """Convert a date-shaped value to a datetime for comparison; otherwise leave as-is."""
    if field == "date":
        return datetime.fromisoformat(value)
    return value


def _field_value(event: dict, field: str):
    """Extract the comparable value for `field` from an event dict."""
    if field == "date":
        date_val = event.get("date")
        if isinstance(date_val, dict):
            date_val = date_val.get("ISO8601")
        return datetime.fromisoformat(date_val) if date_val else None
    return event.get(field)


def _compare(left, op: str, right) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if left is None or right is None:
        return False
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    raise ValueError(f"Unsupported operator: {op}")


def _matches(event: dict, field: str, op: str, value: str) -> bool:
    if field in _ACTION_FIELDS:
        # `type` lives on each action, not on the event itself: the event
        # matches if any of its actions has a matching value.
        return any(
            _compare(action.get(field), op, _coerce(field, value))
            for action in event.get("actions", [])
        )
    return _compare(_field_value(event, field), op, _coerce(field, value))


def apply_filter(events: list[dict], filter_str: str | None) -> list[dict]:
    """Return only the events matching the given OData $filter string."""
    if not filter_str:
        return events
    clauses = parse_filter(filter_str)
    return [
        event for event in events
        if all(_matches(event, field, op, value) for field, op, value in clauses)
    ]
