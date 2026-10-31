"""The CalDAV backend's capability declaration, derived from caldav's server hints (API_DESIGN.md §5.1).

caldav's ``FeatureSet`` describes servers; this table describes what the
backend does with one.  Its levels map as §5.1 says: ``full``/``quirk`` →
``FULL``; ``unsupported``, ``broken``, ``ungraceful`` → ``UNSUPPORTED``;
``fragile``, ``unknown`` → ``UNKNOWN``.  For a server caldav has no hints
for, every server-dependent feature is ``UNKNOWN``.  Most of caldav's hints
describe workarounds it applies itself, so few map to a feature; what a
hinted server stores beyond those is taken from caldav's save-load tests
having passed (``STORED``).  What the backend does client-side whatever the
server says is declared here directly.
"""

from __future__ import annotations

from typing import Any

from .capabilities import (
    BACKEND_FEATURES,
    EDIT_ICALENDAR_PENDING,
    Capabilities,
    Capability,
    Feature,
    Support,
    held_only,
)
from .types import Component

__all__ = ["backend_capabilities", "collection_capabilities"]

F, L, E, U, X = Support.FULL, Support.LOSSY, Support.EMULATED, Support.UNSUPPORTED, Support.UNKNOWN

LEVELS = {
    "full": F,
    "quirk": F,
    "unsupported": U,
    "broken": U,
    "ungraceful": U,
    "fragile": X,
    "unknown": X,
}

# Feature -> the caldav feature its level comes from.
FROM_SERVER = {
    Feature.CREATE_COLLECTION: "create-calendar",
    Feature.DELETE_COLLECTION: "delete-calendar",
    Feature.COLLECTION_SET_NAME: "create-calendar.set-displayname",
    Feature.COLLECTION_MIXED: "create-calendar.with-supported-component-types.multiple",
    Feature.RECURRENCE: "save-load.event.recurrences",
    Feature.TASK_RELATIONS_PARENT: "save-load.icalendar.related-to",
    Feature.TASK_RELATIONS_DEPENDS_ON: "save-load.icalendar.related-to",
    Feature.TIMEZONES: "save-load.event.timezone",
    Feature.TASK_UNCOMPLETE: "save-load.todo.uncomplete",
}

COMPONENT_FROM_SERVER = {
    Component.EVENT: (Feature.COMPONENT_EVENT, "save-load.event"),
    Component.TASK: (Feature.COMPONENT_TASK, "save-load.todo"),
    Component.JOURNAL: (Feature.COMPONENT_JOURNAL, "save-load.journal"),
}

# What the backend does the same way on every server, for now.
FIXED: dict[Feature, Any] = {
    **dict.fromkeys(Feature, F),
    Feature.SEARCH_SERVER_SIDE: {"level": E, "note": "the server narrows by component, and events by time range"},
    Feature.SEARCH_TIME_RANGE: E,
    Feature.SEARCH_TEXT: E,
    Feature.CHANGES: {"level": E, "method": "etag-listing"},
    Feature.IDENTITY_FOREIGN_ID: {"level": U, "note": "the UID is native"},
    Feature.RECURRENCE_EDIT_OCCURRENCE: {"level": U, "note": EDIT_ICALENDAR_PENDING},
    Feature.RECURRENCE_EDIT_THIS_AND_FUTURE: U,
    Feature.TASK_COMPLETE_RECURRING: {"level": U, "note": EDIT_ICALENDAR_PENDING},
}


def _hinted(features: Any) -> bool:
    """Whether caldav knows anything about this server; without hints its defaults are guesses."""
    return bool(getattr(features, "_server_features", None))


def _level(features: Any, name: str) -> Support:
    if not _hinted(features):
        return X
    return LEVELS.get(str(features.is_supported(name, str)), X)


# What the server must store, or honour, as sent: FULL on a hinted server, UNKNOWN on another.
STORED = frozenset(
    {
        Feature.WRITE,
        Feature.WRITE_CONDITIONAL,
        Feature.IDENTITY_CLIENT_UID,
        Feature.IDENTITY_CLIENT_NATIVE_ID,
        Feature.PROPERTIES_PASSTHROUGH,
        Feature.CATEGORIES,
        Feature.ATTENDEES,
        Feature.TASK_STATUS,
        Feature.TASK_PRIORITY,
        Feature.TASK_PERCENT_COMPLETE,
        Feature.TASK_START,
        Feature.TASK_DUE,
        Feature.TASK_COMPLETED,
        Feature.TASK_PLANNED,
        Feature.TASK_ESTIMATE,
        Feature.ALARMS,
        Feature.DATES,
        Feature.TIME_LOG,
    }
)


def _table(features: Any) -> dict[Feature, Any]:
    table = dict(FIXED)
    if not _hinted(features):
        table.update(dict.fromkeys(STORED, X))
    for feature, name in FROM_SERVER.items():
        table[feature] = _level(features, name)
    if table[Feature.CREATE_COLLECTION] is F:
        notes = []
        if _level(features, "create-calendar.with-supported-component-types") is U:
            notes.append("the requested components are not kept: the server decides")
        else:
            # Infomaniak and CCS: a calendar asked for these is made without them
            if refused := sorted(c.value for c in Component if _level(features, COMPONENT_FROM_SERVER[c][1]) is U):
                notes.append(f"made without {', '.join(refused)}, which the server does not store")
            if table[Feature.COLLECTION_MIXED] is U:  # CCS
                notes.append("made for one component only, the first in sorted order")
        if notes:
            table[Feature.CREATE_COLLECTION] = {"level": L, "note": "; ".join(notes)}
    return table


def _move(features: Any) -> dict[str, Any]:
    """Copy, then delete; unless the server keeps a UID in one calendar only (``save.duplicate-uid.cross-calendar``)."""
    hint = features.is_supported("save.duplicate-uid.cross-calendar", dict) if _hinted(features) else {}
    cross = str(hint.get("support", "unknown"))
    if cross in ("full", "unknown"):
        return {"level": E, "note": "added to the target, then deleted here"}
    if cross == "unsupported" and hint.get("behaviour") != "silently-ignored":  # Zimbra moves the original
        return {"level": E, "order": "add-may-move", "note": "added to the target, then deleted here if still there"}
    if _level(features, "save-load.reuse-deleted-uid") is U:
        return {"level": U, "note": "the UID can be neither in two calendars at once nor reused once deleted"}
    return {"level": E, "order": "delete-first", "note": "deleted here, then added to the target"}


def _changes(features: Any) -> dict[str, Any]:
    """A native change feed where the server has a reliable one; else the etag listing, which always works.

    Reliable includes refusing a token it no longer honours, with an error of any kind: a server
    that answers one with a full listing (``broken``: Stalwart, OX) would look like a report where
    everything changed and nothing was deleted.
    """
    refuses = str(features.is_supported("sync-token.invalid-token", str)) != "broken"
    if _level(features, "sync-token") is F and _level(features, "sync-token.delete") is F and refuses:
        return {"level": F, "method": "sync-token"}
    return {"level": E, "method": "etag-listing"}


def _time_range(features: Any) -> dict[str, Any]:
    """Events only, and only where the server matches recurrences and open ranges as RFC 4791 says,
    and answers a range from an index that is current.

    Tasks are always filtered client-side: RFC 4791 §9.9's rules for a
    ``VTODO`` are where servers disagree most, and an under-match loses data.
    """
    needed = (
        "search.time-range.event",
        "search.time-range.accurate",
        "search.time-range.open.start",
        "search.time-range.open.end",
        "search.recurrences.includes-implicit.event",
    )
    cache = features.is_supported("search-cache", dict)
    if cache.get("behaviour") == "delay" and cache.get("filtered-only"):  # a plain listing is current, a range is not
        return {"level": E, "components": []}
    if all(_level(features, name) is F for name in needed):
        return {"level": E, "components": ["VEVENT"]}
    return {"level": E, "components": []}


def stored_components(features: Any, listed: frozenset[Component]) -> frozenset[Component]:
    """What a calendar lists, less the kinds its server is known to refuse (robur lists VJOURNAL, then answers 400)."""
    return frozenset(c for c in listed if _level(features, COMPONENT_FROM_SERVER[c][1]) is not U)


def backend_capabilities(features: Any) -> Capabilities:
    return Capabilities({f: v for f, v in _table(features).items() if f in BACKEND_FEATURES})


def collection_capabilities(features: Any, components: frozenset[Component]) -> Capabilities:
    """The server's table, narrowed to what the calendar says it holds."""
    table = {f: v for f, v in _table(features).items() if f not in BACKEND_FEATURES}
    table[Feature.CHANGES] = _changes(features)
    table[Feature.MOVE] = _move(features)
    table[Feature.SEARCH_TIME_RANGE] = _time_range(features)
    for kind, (feature, name) in COMPONENT_FROM_SERVER.items():
        if kind in components:
            table[feature] = _level(features, name)
    if _level(features, "save-load.icalendar.related-to") is U:
        table[Feature.TIME_LOG] = {"level": U, "note": "the server strips RELATED-TO, which ties an entry to its item"}
    held_only(table, components)
    return Capabilities({f: Capability.coerce(v) for f, v in table.items()})
