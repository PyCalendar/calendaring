"""Mode-free write planning: what a backend cannot store (§5.3), and what it stored differently (verify).

The generic layer derives losses from the capability table, so a backend
that declares a property ``UNSUPPORTED``, or ``LOSSY`` with ``values`` (and
optionally a ``map`` from a value to the one stored instead), gets the loss
check without code of its own.  A backend with mapping rules beyond that
overrides ``_plan_losses`` on its collection.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import itertools
import zoneinfo
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, TypeVar

import icalendar
import recurring_ical_events
from icalendar.parser import Contentline

from .capabilities import Capabilities, Feature, Support
from .items import X_PLANNED_END, X_PLANNED_START
from .loss import Loss

__all__ = ["Change", "differences", "plan_calendar_losses", "plan_losses", "property_lines"]

T = TypeVar("T")

# A loss and the edit that makes the component storable.
Change = tuple[Loss, Callable[[], None]]

ANY_COMPONENT: dict[str, Feature] = {
    "CATEGORIES": Feature.CATEGORIES,
    "ATTENDEE": Feature.ATTENDEES,
    "RRULE": Feature.RECURRENCE,
    "RDATE": Feature.RECURRENCE,
    "EXDATE": Feature.RECURRENCE,
}

# The properties whose values are dates or times, for timezones and dates.
TIME_PROPERTIES = ("DTSTART", "DTEND", "DUE", "RECURRENCE-ID", "RDATE", "EXDATE")

# What a backend without the feature stores instead (§5.1).
CONVERSIONS: dict[Feature, Callable[[Any], Any]] = {
    # A time with a TZID, or a floating one (read as local time, as the searcher does), becomes UTC.
    Feature.TIMEZONES: lambda t: t.astimezone(UTC) if isinstance(t, datetime) and t.tzname() != "UTC" else t,
    # A floating time only, read as local time; a TZID is kept (Zimbra, SOGo).
    Feature.TIMEZONES_FLOATING: lambda t: t.astimezone(UTC) if isinstance(t, datetime) and t.tzinfo is None else t,
    # A date becomes midnight UTC.
    Feature.DATES: lambda t: (
        t if isinstance(t, datetime) or not isinstance(t, date) else datetime.combine(t, time(), UTC)
    ),
}

TASK_ONLY: dict[str, Feature] = {
    "STATUS": Feature.TASK_STATUS,
    "PRIORITY": Feature.TASK_PRIORITY,
    "PERCENT-COMPLETE": Feature.TASK_PERCENT_COMPLETE,
    "DTSTART": Feature.TASK_START,
    "DUE": Feature.TASK_DUE,
    "COMPLETED": Feature.TASK_COMPLETED,
    X_PLANNED_START: Feature.TASK_PLANNED,
    X_PLANNED_END: Feature.TASK_PLANNED,
    "ESTIMATED-DURATION": Feature.TASK_ESTIMATE,
}

RELTYPE_FEATURE: dict[str, Feature] = {
    "PARENT": Feature.TASK_RELATIONS_PARENT,
    "CHILD": Feature.TASK_RELATIONS_PARENT,
    "SIBLING": Feature.TASK_RELATIONS_PARENT,
}

# RFC 5545 §3.2's parameter defaults: a server that spells one out has changed nothing.
PARAMETER_DEFAULTS = {
    "CUTYPE": "INDIVIDUAL",
    "ENCODING": "8BIT",
    "FBTYPE": "BUSY",
    "PARTSTAT": "NEEDS-ACTION",
    "RELATED": "START",
    "RELTYPE": "PARENT",
    "ROLE": "REQ-PARTICIPANT",
    "RSVP": "FALSE",
}

# Parameters a server may add without changing what a line means: an X- one anywhere, and on an
# attendee or organizer its name, address and scheduling state (SOGo adds RSVP, Ox EMAIL).
ADDABLE_PARAMETERS = {
    "ATTENDEE": frozenset({"CN", "EMAIL", "RSVP", "SCHEDULE-AGENT", "SCHEDULE-STATUS", "SCHEDULE-FORCE-SEND"}),
    "ORGANIZER": frozenset({"CN", "EMAIL", "SCHEDULE-AGENT", "SCHEDULE-STATUS", "SCHEDULE-FORCE-SEND"}),
}

# Single date-time properties a server may store in UTC instead of with their TZID.
INSTANTS = frozenset({"DTSTART", "DTEND", "DUE", "COMPLETED", "RECURRENCE-ID", X_PLANNED_START, X_PLANNED_END})

# Properties a server may add lines of without losing what was sent: the organizer as an attendee.
ADDED_LINES_KEEP_ALL = frozenset({"ATTENDEE"})

# Properties servers legitimately change, and identity a backend may assign (§5.3).
NOT_VERIFIED = frozenset({"DTSTAMP", "SEQUENCE", "LAST-MODIFIED", "UID"})


def _values(component: icalendar.Component, name: str) -> list[Any]:
    value = component.get(name)
    return value if isinstance(value, list) else [value]


def _key(name: str, value: Any) -> Any:
    return int(value) if name == "PRIORITY" else str(value)


def _drop(component: icalendar.Component, name: str) -> Callable[[], None]:
    return lambda: component.pop(name, None)


def _replace(component: icalendar.Component, name: str, stored: Any) -> Callable[[], None]:
    def apply() -> None:
        component.pop(name, None)
        component.add(name, stored)

    return apply


def _keep_related(component: icalendar.Component, dropped: list[Any]) -> Callable[[], None]:
    def apply() -> None:
        kept = [v for v in _values(component, "RELATED-TO") if v not in dropped]
        component.pop("RELATED-TO", None)
        for value in kept:
            component.add("RELATED-TO", value)

    return apply


def _times(component: icalendar.Component, name: str) -> list[Any]:
    """One property's dates and times; ``RDATE`` and ``EXDATE`` may hold several, on several lines."""
    out: list[Any] = []
    for value in _values(component, name):
        if hasattr(value, "dts"):
            out.extend(d.dt for d in value.dts)
        else:
            out.append(value.dt)
    return out


def _convert(component: icalendar.Component, name: str, new: list[Any]) -> Callable[[], None]:
    def apply() -> None:
        component.pop(name, None)
        component.add(name, new if name in ("RDATE", "EXDATE") else new[0])

    return apply


def _time_losses(component: icalendar.Component, capabilities: Capabilities) -> list[Change]:
    changes: list[Change] = []
    kept = (Support.FULL, Support.EMULATED, Support.UNKNOWN)
    for feature, convert in CONVERSIONS.items():
        if capabilities.level(feature) in kept:
            continue
        if feature is Feature.TIMEZONES_FLOATING and capabilities.level(Feature.TIMEZONES) not in kept:
            continue  # timezones converts floating times already
        for name in TIME_PROPERTIES:
            if name not in component:
                continue
            sent = _times(component, name)
            stored = [convert(t) for t in sent]
            if any(new is not old for new, old in zip(stored, sent, strict=True)):  # a conversion returns a new value
                changes.append((Loss(name, sent[0], stored[0]), _convert(component, name, stored)))
    return changes


def _alarm_losses(component: icalendar.Component, capabilities: Capabilities) -> list[Change]:
    alarms = [c for c in component.subcomponents if c.name == "VALARM"]
    level = capabilities.level(Feature.ALARMS)
    if not alarms or level not in (Support.UNSUPPORTED, Support.LOSSY):
        return []
    sent = alarms[0].to_ical().decode()
    if level is Support.LOSSY:  # rewritten in a way not modelled here (Ox): sent as it is
        return [(Loss("VALARM", sent, sent, note=capabilities[Feature.ALARMS].note), lambda: None)]

    def apply() -> None:
        component.subcomponents[:] = [c for c in component.subcomponents if c.name != "VALARM"]

    return [(Loss("VALARM", sent, None), apply)]


def plan_losses(component: icalendar.Component, capabilities: Capabilities) -> list[Change]:
    """The losses writing ``component`` would cause, each with the edit that applies it."""
    changes: list[Change] = _alarm_losses(component, capabilities)
    is_task = component.name == "VTODO"
    table = ANY_COMPONENT | TASK_ONLY if is_task else ANY_COMPONENT
    for name in list(component):
        feature = table.get(name)
        if feature is None:
            if (
                name.startswith("X-")
                and not name.startswith("X-PYCAL-")
                and capabilities.level(Feature.PROPERTIES_PASSTHROUGH) is Support.UNSUPPORTED
            ):
                changes.append((Loss(name, component[name], None), _drop(component, name)))
            continue
        level = capabilities.level(feature)
        if level is Support.UNSUPPORTED:
            changes.append((Loss(name, component[name], None), _drop(component, name)))
        elif level is Support.LOSSY and "values" in capabilities.details(feature):
            details = capabilities.details(feature)
            sent = _key(name, component[name])
            if sent not in details["values"]:
                mapping = details.get("map", {})
                stored = mapping.get(sent, mapping.get(str(sent)))
                apply = _drop(component, name) if stored is None else _replace(component, name, stored)
                changes.append((Loss(name, sent, stored), apply))
        elif level is Support.LOSSY and (needed := capabilities.details(feature).get("requires")):
            if not any(other in component for other in needed):  # Zimbra: a task's DTSTART needs a DUE
                changes.append((Loss(name, component[name], None), _drop(component, name)))
    if is_task and "RELATED-TO" in component:
        by_feature: dict[Feature, list[Any]] = {}
        for value in _values(component, "RELATED-TO"):
            reltype = str(value.params.get("RELTYPE", "PARENT")).upper()
            feature = RELTYPE_FEATURE.get(reltype, Feature.TASK_RELATIONS_DEPENDS_ON)
            if capabilities.level(feature) is Support.UNSUPPORTED:
                by_feature.setdefault(feature, []).append(value)
        dropped = [v for values in by_feature.values() for v in values]
        if dropped:
            sent = [str(v) for v in dropped]
            changes.append(
                (Loss("RELATED-TO", sent if len(sent) > 1 else sent[0], None), _keep_related(component, dropped))
            )
    if (
        "RDATE" in component
        and not is_task  # probed on events, and a calendar's level is for both
        and "RDATE" not in {loss.property for loss, _ in changes}
        and capabilities.level(Feature.RECURRENCE_RDATE) is Support.UNSUPPORTED  # Zimbra keeps an RRULE, not an RDATE
    ):
        changes.append((Loss("RDATE", component["RDATE"], None), _drop(component, "RDATE")))
    dropped_names = {loss.property for loss, _ in changes}
    changes.extend(c for c in _time_losses(component, capabilities) if c[0].property not in dropped_names)
    return changes


def _lines(component: icalendar.Component) -> list[bytes]:
    """The component's own content lines, unfolded: not those of its subcomponents."""
    own = copy.copy(component)
    own.subcomponents = []
    return own.to_ical().replace(b"\r\n ", b"").split(b"\r\n")  # type: ignore[no-any-return]


def plan_calendar_losses(calendar: icalendar.Calendar, capabilities: Capabilities) -> list[Change]:
    """The losses across a stored object's components: overrides go with the RRULE they belong to."""
    if capabilities.level(Feature.RECURRENCE) is not Support.UNSUPPORTED:
        return _rdate_override_losses(calendar, capabilities)
    overrides = [c for c in calendar.subcomponents if "RECURRENCE-ID" in c]
    if not overrides or len(overrides) == len([c for c in calendar.subcomponents if c.name != "VTIMEZONE"]):
        return []  # occurrences without a master are items of their own

    def apply() -> None:
        calendar.subcomponents[:] = [c for c in calendar.subcomponents if "RECURRENCE-ID" not in c]

    sent = [c.get("RECURRENCE-ID").to_ical().decode() for c in overrides]
    return [(Loss("RECURRENCE-ID", sent if len(sent) > 1 else sent[0], None), apply)]


def _rdate_override_losses(calendar: icalendar.Calendar, capabilities: Capabilities) -> list[Change]:
    """An event's override of an occurrence only its RDATE gives goes with the RDATE (Zimbra): it would be an orphan."""
    if capabilities.level(Feature.RECURRENCE_RDATE) is not Support.UNSUPPORTED:
        return []
    master = next((c for c in calendar.walk("VEVENT") if "RECURRENCE-ID" not in c and "RDATE" in c), None)
    if master is None:
        return []
    rrule_only = icalendar.Calendar()
    rrule_only.add_component(without := copy.deepcopy(master))
    without.pop("RDATE")

    def rrule_gives(when: Any) -> bool:
        return "RRULE" in master and any(  # an occurrence starting there, not one merely overlapping it
            o.decoded("DTSTART") == when
            for o in recurring_ical_events.of(rrule_only).between(when, when + timedelta(seconds=1))
        )

    rdates = {d[0] if isinstance(d, tuple) else d for d in _times(master, "RDATE")}  # a PERIOD by its start
    orphans = [
        c
        for c in calendar.walk("VEVENT")
        if "RECURRENCE-ID" in c and (when := c.decoded("RECURRENCE-ID")) in rdates and not rrule_gives(when)
    ]
    if not orphans:
        return []

    def apply() -> None:
        calendar.subcomponents[:] = [c for c in calendar.subcomponents if not any(c is o for o in orphans)]

    sent = [c.get("RECURRENCE-ID").to_ical().decode() for c in orphans]
    return [(Loss("RECURRENCE-ID", sent if len(sent) > 1 else sent[0], None), apply)]


def property_lines(component: icalendar.Component, name: str) -> list[bytes]:
    """One property's content lines, unfolded and sorted, for an order-insensitive comparison.

    Only the component's own: a ``VALARM``'s ``DESCRIPTION`` is not its event's.
    """
    out = []
    for line in _lines(component):
        head = line.split(b":", 1)[0].split(b";", 1)[0]
        if head.upper() == name.encode():
            out.append(line)
    return sorted(out)


def _comparable(component: icalendar.Component, name: str) -> list[Any]:
    """What ``verify`` compares for a property, so that a server's harmless rewrites compare equal.

    A parameter at its RFC 5545 default is the same as none, and parameters are in any order.
    ``CATEGORIES`` is a set however it is split over lines and commas, and
    an aware date-time on a single event is an instant, whether it is stored
    with its TZID or in UTC.  A floating time is not an instant, and in a
    recurring series a TZID is not either, so converting one is a change.
    """
    if name == "CATEGORIES":
        return sorted({str(c) for value in _values(component, name) for c in getattr(value, "cats", [value])})
    if name in {"RDATE", "EXDATE"}:  # a list of values, however it is split over lines
        return sorted(
            {
                (n, params, one)
                for n, params, value in map(_without_defaults, property_lines(component, name))
                for one in str(value).split(",")
            }
        )
    if name in INSTANTS and name in component and not isinstance(component[name], list) and not _recurs(component):
        value = component.decoded(name)
        if isinstance(value, datetime) and value.tzinfo is not None:
            return [value.astimezone(UTC)]
    return sorted(_without_defaults(line) for line in property_lines(component, name))


def _without_defaults(line: bytes) -> tuple[Any, ...]:
    """A content line with its parameters in order, those at their RFC 5545 default left out.

    The value is kept as written: an escaped comma is not a list separator.  A parameter's
    list of values is a set, like the parameters themselves.
    """
    try:
        name, params, value = Contentline(line.decode()).raw_parts()  # type: ignore[no-untyped-call]  # icalendar leaves it unannotated
    except ValueError:  # icalendar's own lines parse, so this one is a server's: compared verbatim
        return line.decode(), (), ""
    kept = sorted(
        (key.upper(), tuple(sorted(_zone(str(v)) if key.upper() == "TZID" else str(v) for v in values)))
        for key, raw in params.items()
        for values in [raw if isinstance(raw, list) else [raw]]
        if not (len(values) == 1 and PARAMETER_DEFAULTS.get(key.upper()) == str(values[0]).upper())
    )
    return name.upper(), tuple(kept), value


@functools.cache
def _zone(tzid: str) -> str:
    """A time zone by how it behaves, so that one tzdata renamed into another (Europe/Oslo, now a link to Berlin) compares equal.

    The UTC offset at the start of every month from 1970 on: tzdata merges
    zones only where they agree from 1970, and Europe/Sofia, an hour ahead of
    Berlin all year, is not Berlin.  A zone this machine does not know is its name.
    """
    try:
        zone = zoneinfo.ZoneInfo(tzid)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return tzid
    offsets = (zone.utcoffset(datetime(year, month, 1)) for year in range(1970, 2041) for month in range(1, 13))
    return "zone:" + hashlib.sha1(repr(list(offsets)).encode()).hexdigest()


def _recurs(component: icalendar.Component) -> bool:
    """A series' times are local times: moved to UTC, its occurrences shift at a DST change."""
    return "RRULE" in component or "RDATE" in component


def _stored_as_sent(sent: icalendar.Component, stored: icalendar.Component) -> bool:
    """Whether ``stored`` holds everything ``sent`` did, as the top level is compared: what a server adds is fine."""
    if sent.name != stored.name:
        return False
    if not all(_kept(sent, stored, name) for name in dict.fromkeys(sent) if name not in NOT_VERIFIED):
        return False
    return _matched(sent.subcomponents, stored.subcomponents)


def _matched(sent: list[icalendar.Component], stored: list[icalendar.Component]) -> bool:
    """Each sent subcomponent has a stored one of its own, in any order.

    Tried every way, not first come first served: a sent alarm that matches two stored
    ones must not take the one another sent alarm needs.  A component has few.
    """
    return _paired(sent, stored, _stored_as_sent)


def _paired(sent: list[T], stored: list[T], fits: Callable[[T, T], bool]) -> bool:
    """Each of ``sent`` ``fits`` one of ``stored`` of its own, tried every way."""
    if not sent:
        return True
    first, rest = sent[0], sent[1:]
    return any(
        fits(first, other) and _paired(rest, stored[:i] + stored[i + 1 :], fits) for i, other in enumerate(stored)
    )


def _kept(sent: icalendar.Component, stored: icalendar.Component, name: str) -> bool:
    """Whether ``stored`` has ``name`` as ``sent`` had it.

    A parameter the server added that says nothing about the value (``_covered``) is no
    difference.  One it dropped or changed is, and so is a line less: a stored line holds one
    sent line only.  A line more is too, except an attendee that is the stored organizer: a
    scheduling server adds the organizer as one (Ox).
    """
    mine, theirs = _comparable(sent, name), _comparable(stored, name)
    if mine == theirs:
        return True
    if not all(isinstance(line, tuple) for line in [*mine, *theirs]):
        return False
    if name in ADDED_LINES_KEEP_ALL and len(theirs) > len(mine):
        organizer = str(stored.get("ORGANIZER", "")).lower()
        extra = len(theirs) - len(mine)
        candidates = [i for i, line in enumerate(theirs) if organizer and str(line[2]).lower() == organizer]
        return any(
            _paired(mine, [line for i, line in enumerate(theirs) if i not in left_out], _covered)
            for left_out in itertools.combinations(candidates, extra)
        )
    return len(mine) == len(theirs) and _paired(mine, theirs, _covered)


def _covered(sent: tuple[Any, ...], stored: tuple[Any, ...]) -> bool:
    """Whether a stored line, as ``_without_defaults`` gives it, is the sent one with harmless parameters added.

    A parameter left out means its default, so one added with another value (PARTSTAT=DECLINED)
    changes the line, and so does one that changes how the value reads (TZID, VALUE, RELTYPE).
    """
    if (sent[0], sent[2]) != (stored[0], stored[2]) or not set(sent[1]) <= set(stored[1]):
        return False
    addable = ADDABLE_PARAMETERS.get(str(sent[0]), frozenset())
    return all(key.startswith("X-") or key in addable for key, _ in set(stored[1]) - set(sent[1]))


def _subcomponents(component: icalendar.Component, kind: str) -> list[icalendar.Component]:
    return [c for c in component.subcomponents if c.name == kind]


def _ical(components: list[icalendar.Component]) -> str:
    return b"".join(c.to_ical() for c in components).decode()


def _by_occurrence(calendar: icalendar.Calendar) -> dict[str | None, icalendar.Component]:
    out = {}
    for component in calendar.subcomponents:
        if component.name == "VTIMEZONE":
            continue
        rid = component.get("RECURRENCE-ID")
        out[None if rid is None else rid.to_ical().decode()] = component
    return out


def undeclared(found: list[Loss], capabilities: Capabilities) -> list[Loss]:
    """The ``differences`` the declaration does not say a write may cause: a ``LOSSY`` alarm may come back changed.

    Changed, not gone: as many alarms must come back as were sent.
    """
    if capabilities.level(Feature.ALARMS) is Support.LOSSY:
        return [
            loss for loss in found if not (loss.property == "VALARM" and _alarms(loss.sent) == _alarms(loss.stored))
        ]
    return found


def _alarms(text: object) -> int:
    return str(text or "").count("BEGIN:VALARM")


def differences(sent: icalendar.Calendar, stored: icalendar.Calendar) -> list[Loss]:
    """Every property that was sent and is stored differently, or not at all."""
    out: list[Loss] = []
    stored_parts = _by_occurrence(stored)
    for rid, component in _by_occurrence(sent).items():
        other = stored_parts.get(rid)
        if other is None:
            out.append(Loss(f"RECURRENCE-ID={rid}" if rid else str(component.name), component.name, None))
            continue
        for name in dict.fromkeys(component):
            if name in NOT_VERIFIED:
                continue
            if not _kept(component, other, name):
                mine, theirs = property_lines(component, name), property_lines(other, name)
                out.append(Loss(name, b"\n".join(mine).decode(), b"\n".join(theirs).decode() or None))
        for kind in dict.fromkeys(str(c.name) for c in [*component.subcomponents, *other.subcomponents]):
            mine_subs = _subcomponents(component, kind)
            theirs_subs = _subcomponents(other, kind)
            if not _matched(mine_subs, theirs_subs):
                # Name what was not kept: the sent ones nothing stored matches, and the stored ones that match
                # nothing sent.  Where each matches but they cannot be paired off, that is all of them.
                lost = [m for m in mine_subs if not any(_stored_as_sent(m, t) for t in theirs_subs)]
                instead = [t for t in theirs_subs if not any(_stored_as_sent(m, t) for m in mine_subs)]
                if not lost:
                    lost, instead = mine_subs, theirs_subs
                out.append(Loss(kind, _ical(lost), _ical(instead) or None))
    return out
