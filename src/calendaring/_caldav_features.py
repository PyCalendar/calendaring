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

from ._caldav_io import CalendarObjectResource
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
    Feature.TIMEZONES_FLOATING: "save-load.event.floating",
    Feature.TASK_UNCOMPLETE: "save-load.todo.uncomplete",
}

# Feature -> the caldav feature saying a task keeps the property only beside others, those, and a note.
REQUIRES_FROM_SERVER = {
    Feature.TASK_START: (
        "save-load.todo.dtstart-without-due",
        ["DUE"],
        "the server drops the DTSTART of a task without a DUE",  # Zimbra
    ),
    Feature.TASK_COMPLETED: (
        "save-load.todo.completed-without-status",
        ["STATUS"],
        "the server drops the COMPLETED of a task without a STATUS",  # Ox
    ),
}

# Feature -> the caldav feature probed on a task.  A level is the collection's, not a component's, so
# a calendar holding events too keeps these: lowering them would cost its events what they can keep.
TASK_FROM_SERVER = {
    Feature.ATTENDEES: "save-load.todo.attendee",
    Feature.RECURRENCE: "save-load.todo.recurrences",
    Feature.PROPERTIES_PASSTHROUGH: "save-load.icalendar.x-property",  # Ox drops X- properties from tasks only
}

# The same for what only a task has: lowered wherever tasks are held.
TASK_ONLY_FROM_SERVER = {
    Feature.TASK_PLANNED: "save-load.icalendar.x-property",  # X-PYCAL-PLANNED-*
    Feature.TASK_ESTIMATE: "save-load.icalendar.unknown-property",  # probed with ESTIMATED-DURATION
}

# Feature -> the caldav features listing the values a task keeps (``values``) and stores as another
# (``map``), each with the values it probes: what is kept where the server keeps them all.
VALUES_FROM_SERVER = {
    Feature.TASK_STATUS: (
        ("save-load.todo.status", ("NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED")),
        ("save-load.todo.status.rfc10051", ("PENDING", "FAILED")),  # Ox drops them, Zimbra maps them
    ),
    Feature.TASK_PRIORITY: (("save-load.todo.priority", tuple(range(10))),),
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
    if not _hinted(features) or name not in getattr(features, "FEATURES", {name}):  # a hint newer than this caldav
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
    if table[Feature.TIMEZONES_FLOATING] is U:  # stored, in UTC: Zimbra and SOGo
        table[Feature.TIMEZONES_FLOATING] = {"level": L, "note": "a floating time is stored in UTC"}
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
    if _level(features, "save.if-none-match") is U:  # the server overwrites a name already taken
        # caldav then GETs the name before the PUT where a duplicate UID is refused and names are kept (SOGo),
        # and otherwise searches by UID, which leaves the name unguarded
        if str(features.is_supported("save.no-uid-conflict", str)) in {"full", "quirk", "ungraceful"} and (
            features.is_supported("save-load.stable-url")
        ):
            table[Feature.IDENTITY_CLIENT_NATIVE_ID] = {
                "level": E,
                "note": "caldav looks the name up before the PUT, racily",
            }
        else:
            table[Feature.IDENTITY_CLIENT_NATIVE_ID] = {
                "level": U,
                "note": "the server overwrites an object under a name already taken",
            }
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


def _values(features: Any, feature: Feature) -> dict[str, Any] | None:
    """LOSSY with the values the server keeps, where one of the hints says it does not keep them all.
    A refused value is left out, as is a dropped one: the loss planner drops it before sending.
    PRIORITY:0 is not sent as the server's stand-in either (Ox: 5), since no PRIORITY means the same as 0."""
    hints = VALUES_FROM_SERVER[feature]
    if not any(_level(features, name) is U for name, _ in hints):
        return None
    number = int if feature is Feature.TASK_PRIORITY else str  # a hint from YAML or JSON may carry "5"
    values: list[Any] = []
    mapping: dict[Any, Any] = {}
    notes = []
    seen: list[dict[str, Any]] = []
    for name, probed in hints:
        level = _level(features, name)
        if level is F:
            values.extend(probed)
        if level is not U:
            continue  # unknown: nothing is known to be kept
        hint = features.is_supported(name, dict)
        if hint in seen:  # caldav answers a child with its parent's node where only the parent is set
            continue
        seen.append(hint)
        values.extend(number(v) for v in hint.get("values", []))
        mapping.update({number(k): number(v) for k, v in hint.get("map", {}).items() if number(k) != 0})
        if hint.get("behaviour"):
            notes.append(hint["behaviour"])
    return {"level": L, "values": values, "map": mapping, "note": "; ".join(notes) or None}


def multiget_serves_deleted(features: Any) -> bool:
    """Zimbra answers a GET of a deleted object with 404, and a calendar-multiget of it with the old data."""
    return _level(features, "multiget.deleted") is U


def backend_capabilities(features: Any) -> Capabilities:
    return Capabilities({f: v for f, v in _table(features).items() if f in BACKEND_FEATURES})


def _alarm(features: Any) -> Any:
    """What a task's VALARM becomes: dropped, or rewritten (Ox: an absolute trigger and its own description)."""
    level = _level(features, "save-load.todo.alarm")
    if level is not U:
        return None
    hint = features.is_supported("save-load.todo.alarm", dict)
    if hint.get("support") != "broken":
        return U
    note = "the server rewrites a task's alarm"
    return {"level": L, "note": f"{note}: {hint['behaviour']}" if hint.get("behaviour") else note}


def collection_capabilities(features: Any, components: frozenset[Component]) -> Capabilities:
    """The server's table, narrowed to what the calendar says it holds."""
    table = {f: v for f, v in _table(features).items() if f not in BACKEND_FEATURES}
    table[Feature.CHANGES] = _changes(features)
    table[Feature.MOVE] = _move(features)
    table[Feature.SEARCH_TIME_RANGE] = _time_range(features)
    for kind, (feature, name) in COMPONENT_FROM_SERVER.items():
        if kind in components:
            table[feature] = _level(features, name)
    if _hinted(features) and str(features.is_supported("delete.if-match", str)) == "broken":
        if hasattr(CalendarObjectResource, "_ignores_delete_if_match"):  # caldav 3.8
            table[Feature.WRITE_CONDITIONAL] = {
                "level": E,
                "note": "the server deletes on a stale If-Match; caldav reads the ETag first, a window before the delete",
            }
        else:
            table[Feature.WRITE_CONDITIONAL] = {
                "level": U,
                "note": "the server deletes on a stale If-Match: delete() is not conditional before caldav 3.8; save() is",
            }
    if _level(features, "save-load.icalendar.related-to") is U:
        table[Feature.TIME_LOG] = {"level": U, "note": "the server strips RELATED-TO, which ties an entry to its item"}
    elif _level(features, "save-load.icalendar.participant") is U:
        table[Feature.TIME_LOG] = {
            "level": L,
            "lost": ["users"],
            "note": "the server refuses an event with a PARTICIPANT, which names who did the work",
        }
    if Component.EVENT not in components and _level(features, "save-load.event.floating") is U:
        # Probed on events, and SOGo keeps a task's floating times: a task list is not known to lose them
        table[Feature.TIMEZONES_FLOATING] = {"level": X, "note": "floating times are probed on events only"}
    if Component.TASK in components:
        lowered = {**TASK_ONLY_FROM_SERVER, **(TASK_FROM_SERVER if Component.EVENT not in components else {})}
        for feature, name in lowered.items():
            if _level(features, name) is U:
                table[feature] = U
        if Component.EVENT not in components and (alarm := _alarm(features)) is not None:
            table[Feature.ALARMS] = alarm
        for feature in VALUES_FROM_SERVER:  # Ox keeps priorities 1, 5 and 9; Zimbra refuses CANCELLED
            if (declared := _values(features, feature)) is not None:
                table[feature] = declared
        for feature, (name, needed, note) in REQUIRES_FROM_SERVER.items():
            if _level(features, name) is U:
                table[feature] = {"level": L, "requires": needed, "note": note}
    stable_url = features.is_supported("save-load.stable-url", dict) if _hinted(features) else {}
    if stable_url.get("forces_uid_ics"):
        table[Feature.IDENTITY_CLIENT_NATIVE_ID] = {"level": U, "note": "the server renames every object <uid>.ics"}
    elif renamed := sorted(c.value for c in components if c.value in stable_url.get("uid_ics_components", ())):
        table[Feature.IDENTITY_CLIENT_NATIVE_ID] = {
            "level": U,
            "note": f"the server renames a {', '.join(renamed)} <uid>.ics",
        }
    x_property = features.is_supported("save-load.icalendar.x-property", dict) if _hinted(features) else {}
    if _level(features, "save-load.icalendar.x-property") is U and not (x_property.get("behaviour") or "").endswith(
        "in a task"
    ):  # dropped from events too
        table[Feature.PROPERTIES_PASSTHROUGH] = U
    if _level(features, "save-load.icalendar.x-parameter") is U:
        table[Feature.WRITE_WIRE] = {"level": U, "note": "the server drops X- parameters: bytes are not stored as sent"}
    held_only(table, components)
    return Capabilities({f: Capability.coerce(v) for f, v in table.items()})
