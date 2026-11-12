"""Write planning: the losses derived from a declaration, and what verify compares."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
import pytest

from calendaring import Capabilities, Event, Feature, Support, Task, TaskStatus
from calendaring._mapping import differences, plan_calendar_losses, plan_losses, undeclared
from calendaring.memory import TRACKER

OSLO = ZoneInfo("Europe/Oslo")


def with_alarm(event: Event) -> Event:
    alarm = icalendar.Alarm()
    alarm.add("ACTION", "DISPLAY")
    alarm.add("DESCRIPTION", "soon")
    alarm.add("TRIGGER", timedelta(minutes=-15))
    event.component.add_component(alarm)
    return event


def planned(event: Event, table: dict[Feature, Support]) -> tuple[list[str], icalendar.Component]:
    component = copy.deepcopy(event.component)
    changes = plan_losses(component, Capabilities({**dict.fromkeys(Feature, Support.FULL), **table}))
    for _, apply in changes:
        apply()
    return [loss.property for loss, _ in changes], component


def test_a_dropped_alarm_is_a_difference() -> None:
    sent = with_alarm(Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC), description="d"))
    stored = copy.deepcopy(sent.icalendar)
    for component in stored.walk("VEVENT"):
        component.subcomponents[:] = []
    assert [d.property for d in differences(sent.icalendar, stored)] == ["VALARM"]
    assert differences(sent.icalendar, copy.deepcopy(sent.icalendar)) == []


def test_alarms_unsupported_drops_them() -> None:
    event = with_alarm(Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC)))
    losses, component = planned(event, {Feature.ALARMS: Support.UNSUPPORTED})
    assert losses == ["VALARM"]
    assert component.subcomponents == []


def test_alarms_lossy_is_a_loss_and_sent_as_they_are() -> None:
    """Ox rewrites a task's alarm in a way the backend does not model: it is sent as it is."""
    event = with_alarm(Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC)))
    sent = event.component.subcomponents[0].to_ical().decode()
    table = {**dict.fromkeys(Feature, Support.FULL), Feature.ALARMS: {"level": Support.LOSSY, "note": "rewritten"}}
    [(loss, apply)] = plan_losses(event.component, Capabilities(table))
    apply()
    assert (loss.property, loss.sent, loss.stored, loss.note) == ("VALARM", sent, sent, "rewritten")
    assert [c.name for c in event.component.subcomponents] == ["VALARM"]


def test_a_lossy_alarm_rewritten_is_no_undeclared_difference() -> None:
    sent = with_alarm(Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC)))
    stored = copy.deepcopy(sent.icalendar)
    for alarm in stored.walk("VALARM"):
        alarm["DESCRIPTION"] = "Open-XChange"
    found = differences(sent.icalendar, stored)
    table = dict.fromkeys(Feature, Support.FULL)
    assert undeclared(found, Capabilities(table)) == found
    assert undeclared(found, Capabilities({**table, Feature.ALARMS: Support.LOSSY})) == []


def test_a_lossy_alarm_dropped_is_still_an_undeclared_difference() -> None:
    """LOSSY alarms may come back changed (Ox), not be gone."""
    sent = with_alarm(Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC)))
    stored = copy.deepcopy(sent.icalendar)
    for event in stored.walk("VEVENT"):
        event.subcomponents = []
    found = differences(sent.icalendar, stored)
    lossy = Capabilities({**dict.fromkeys(Feature, Support.FULL), Feature.ALARMS: Support.LOSSY})
    assert [loss.property for loss in undeclared(found, lossy)] == ["VALARM"]


def test_timezones_lossy_stores_utc() -> None:
    event = Event.new(
        summary="x", start=datetime(2026, 11, 3, 9, tzinfo=OSLO), end=datetime(2026, 11, 3, 10, tzinfo=OSLO)
    )
    floating = Event.new(summary="y", start=datetime(2026, 11, 3, 9))
    losses, component = planned(event, {Feature.TIMEZONES: Support.LOSSY})
    assert sorted(losses) == ["DTEND", "DTSTART"]
    assert component.decoded("DTSTART") == datetime(2026, 11, 3, 8, tzinfo=UTC)
    losses, component = planned(floating, {Feature.TIMEZONES: Support.LOSSY})
    assert losses == ["DTSTART"]
    assert component.decoded("DTSTART").tzinfo == UTC
    assert planned(event, {Feature.TIMEZONES: Support.FULL})[0] == []


def test_floating_lossy_stores_floating_times_in_utc_only() -> None:
    """Zimbra and SOGo keep a TZID but store a floating time in UTC."""
    zoned = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=OSLO))
    floating = Event.new(summary="y", start=datetime(2026, 11, 3, 9))
    assert planned(zoned, {Feature.TIMEZONES_FLOATING: Support.LOSSY})[0] == []
    losses, component = planned(floating, {Feature.TIMEZONES_FLOATING: Support.LOSSY})
    assert losses == ["DTSTART"]
    assert component.decoded("DTSTART").tzinfo == UTC
    assert planned(floating, {Feature.TIMEZONES_FLOATING: Support.FULL})[0] == []


def test_dates_lossy_stores_midnight_utc() -> None:
    event = Event.new(summary="x", start=date(2026, 11, 3), end=date(2026, 11, 4))
    event.component.add("EXDATE", [date(2026, 11, 10)])
    losses, component = planned(event, {Feature.DATES: Support.LOSSY, Feature.RECURRENCE: Support.FULL})
    assert sorted(losses) == ["DTEND", "DTSTART", "EXDATE"]
    assert component.decoded("DTSTART") == datetime(2026, 11, 3, tzinfo=UTC)
    assert b"EXDATE:20261110T000000Z" in component.to_ical()


def test_recurrence_unsupported_drops_exdate() -> None:
    event = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    event.component.add("RRULE", {"FREQ": "WEEKLY"})
    event.component.add("EXDATE", [datetime(2026, 11, 10, 9, tzinfo=UTC)])
    assert sorted(planned(event, {Feature.RECURRENCE: Support.UNSUPPORTED})[0]) == ["EXDATE", "RRULE"]


def test_recurrence_unsupported_drops_the_overrides_too() -> None:
    series = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    series.component.add("RRULE", {"FREQ": "DAILY", "COUNT": 3})
    moved = Event.new(summary="moved", start=datetime(2026, 11, 4, 15, tzinfo=UTC), uid=series.uid)
    moved.component.add("RECURRENCE-ID", datetime(2026, 11, 4, 9, tzinfo=UTC))
    series.icalendar.add_component(moved.component)
    calendar = copy.deepcopy(series.icalendar)
    table = Capabilities({**dict.fromkeys(Feature, Support.FULL), Feature.RECURRENCE: Support.UNSUPPORTED})
    changes = plan_calendar_losses(calendar, table)
    assert [loss.property for loss, _ in changes] == ["RECURRENCE-ID"]
    for _, apply in changes:
        apply()
    assert [str(c["SUMMARY"]) for c in calendar.walk("VEVENT")] == ["x"]


def test_an_alarm_the_server_annotated_is_no_difference() -> None:
    sent = with_alarm(Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC)))
    stored = copy.deepcopy(sent.icalendar)
    [alarm] = stored.walk("VALARM")
    alarm.add("UID", "server-alarm-1")
    alarm.add("X-WR-ALARMUID", "server-alarm-1")
    assert differences(sent.icalendar, stored) == []
    alarm["TRIGGER"] = icalendar.vDDDTypes(timedelta(minutes=-5))
    assert [d.property for d in differences(sent.icalendar, stored)] == ["VALARM"]


def stored_as(event: Event, line: str) -> icalendar.Calendar:
    """``event`` as a server stored it, its one ATTENDEE rewritten to ``line``."""
    text = event.icalendar.to_ical().decode().replace("ATTENDEE;CN=Bob:mailto:bob@example.com", line)
    return icalendar.Calendar.from_ical(text)


def test_a_parameter_added_with_its_default_is_no_difference() -> None:
    sent = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    sent.component.add("ATTENDEE", "mailto:bob@example.com", parameters={"CN": "Bob"})
    defaults = (
        "ATTENDEE;CUTYPE=individual;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;CN=Bob;RSVP=FALSE:mailto:bob@example.com"
    )
    assert differences(sent.icalendar, stored_as(sent, defaults)) == []


def test_a_parameter_the_server_added_is_no_difference() -> None:
    sent = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    sent.component.add("ATTENDEE", "mailto:bob@example.com", parameters={"CN": "Bob"})
    for added in (
        "ATTENDEE;CN=Bob;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:bob@example.com",  # SOGo
        "ATTENDEE;CN=Bob;EMAIL=bob@example.com:mailto:bob@example.com",  # Ox
    ):
        assert differences(sent.icalendar, stored_as(sent, added)) == []
    for changed in (
        "ATTENDEE:mailto:bob@example.com",
        "ATTENDEE;CN=Robert;RSVP=TRUE:mailto:bob@example.com",
        "ATTENDEE;CN=Bob:mailto:robert@example.com",
    ):
        assert [d.property for d in differences(sent.icalendar, stored_as(sent, changed))] == ["ATTENDEE"]


def test_a_parameter_that_changes_the_meaning_is_a_difference_when_added() -> None:
    """Only parameters that say nothing about the value may be added: a TZID, PARTSTAT or RELTYPE may."""
    series = Event.new(summary="x", start=datetime(2026, 11, 3, 9))
    series.component.add("RRULE", {"FREQ": "DAILY", "COUNT": 3})
    zoned = (
        series.icalendar.to_ical()
        .decode()
        .replace("DTSTART:20261103T090000", "DTSTART;TZID=Europe/Oslo:20261103T090000")
    )
    assert [d.property for d in differences(series.icalendar, icalendar.Calendar.from_ical(zoned))] == ["DTSTART"]
    meeting = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    meeting.component.add("ATTENDEE", "mailto:bob@example.com", parameters={"CN": "Bob"})
    declined = "ATTENDEE;CN=Bob;PARTSTAT=DECLINED:mailto:bob@example.com"
    assert [d.property for d in differences(meeting.icalendar, stored_as(meeting, declined))] == ["ATTENDEE"]
    task = Task.new(summary="x")
    task.component.add("RELATED-TO", "abc")
    child = task.icalendar.to_ical().decode().replace("RELATED-TO:abc", "RELATED-TO;RELTYPE=CHILD:abc")
    assert [d.property for d in differences(task.icalendar, icalendar.Calendar.from_ical(child))] == ["RELATED-TO"]


def test_an_x_parameter_the_server_adds_is_no_difference() -> None:
    sent = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    text = sent.icalendar.to_ical().decode().replace("SUMMARY:x", "SUMMARY;X-SERVER-ID=7:x")
    assert differences(sent.icalendar, icalendar.Calendar.from_ical(text)) == []


def test_a_parameter_added_to_one_of_two_lines_is_no_difference() -> None:
    sent = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    sent.component.add("ATTENDEE", "mailto:bob@example.com")
    sent.component.add("ATTENDEE", "mailto:bob@example.com", parameters={"CN": "Bob"})
    text = sent.icalendar.to_ical().decode()
    # The bare line takes the parameter: matched first come first served, the CN=Bob line would be left over.
    added = text.replace("ATTENDEE:mailto:", "ATTENDEE;CN=Bob;RSVP=TRUE:mailto:")
    assert differences(sent.icalendar, icalendar.Calendar.from_ical(added)) == []
    merged = text.replace("ATTENDEE:mailto:bob@example.com\r\n", "")
    assert [d.property for d in differences(sent.icalendar, icalendar.Calendar.from_ical(merged))] == ["ATTENDEE"]


def test_an_unescaped_comma_is_a_difference_and_parameter_lists_are_sets() -> None:
    sent = icalendar.Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u\r\nRESOURCES:projector\\, big\r\n"
        'ATTENDEE;DELEGATED-TO="mailto:a@example.com","mailto:b@example.com":mailto:c@example.com\r\n'
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    text = sent.to_ical().decode()
    split = icalendar.Calendar.from_ical(text.replace("projector\\, big", "projector,big"))
    assert [d.property for d in differences(sent, split)] == ["RESOURCES"]
    swapped = text.replace(
        '"mailto:a@example.com","mailto:b@example.com"', '"mailto:b@example.com","mailto:a@example.com"'
    )
    assert differences(sent, icalendar.Calendar.from_ical(swapped)) == []


def test_failed_on_a_two_state_tracker_is_stored_as_completed() -> None:
    task = Task.new(summary="x", status=TaskStatus.FAILED)
    [(loss, apply)] = plan_losses(task.component, Capabilities(TRACKER.capabilities))
    assert (loss.property, loss.sent, loss.stored) == ("STATUS", "FAILED", "COMPLETED")
    apply()
    assert task.component["STATUS"] == "COMPLETED"


def test_an_attendee_the_server_added_is_no_difference() -> None:
    """Ox adds the organizer as an attendee (RFC 6638 scheduling): that loses nothing."""
    sent = Event.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    sent.component.add("ATTENDEE", "mailto:bob@example.com", parameters={"CN": "Bob"})
    added = stored_as(
        sent,
        "ATTENDEE;CN=Admin;PARTSTAT=ACCEPTED:mailto:admin@example.com\r\n"
        "ATTENDEE;CN=Bob;EMAIL=bob@example.com:mailto:bob@example.com\r\n"
        "ORGANIZER;CN=Admin:mailto:admin@example.com",
    )
    assert differences(sent.icalendar, added) == []
    stranger = stored_as(
        sent,
        "ATTENDEE;CN=Eve:mailto:eve@example.com\r\n"
        "ATTENDEE;CN=Bob:mailto:bob@example.com\r\n"
        "ORGANIZER;CN=Admin:mailto:admin@example.com",
    )
    assert "ATTENDEE" in [d.property for d in differences(sent.icalendar, stranger)]
    instead = stored_as(sent, "ATTENDEE;CN=Admin:mailto:admin@example.com")
    assert [d.property for d in differences(sent.icalendar, instead)] == ["ATTENDEE"]


def test_a_dropped_start_is_a_difference() -> None:
    """Zimbra drops a task's DTSTART: a loss, not a KeyError."""
    sent = Task.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    stored = copy.deepcopy(sent.icalendar)
    del stored.walk("VTODO")[0]["DTSTART"]
    [loss] = differences(sent.icalendar, stored)
    assert (loss.property, loss.stored) == ("DTSTART", None)


def test_a_property_kept_only_beside_another_is_lost_without_it() -> None:
    """Zimbra keeps a task's DTSTART only where it has a DUE too."""
    table = {Feature.TASK_START: {"level": Support.LOSSY, "requires": ["DUE"]}}
    alone = Task.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC))
    component = copy.deepcopy(alone.component)
    [(loss, apply)] = plan_losses(component, Capabilities({**dict.fromkeys(Feature, Support.FULL), **table}))
    assert (loss.property, loss.stored) == ("DTSTART", None)
    apply()
    assert "DTSTART" not in component
    both = Task.new(summary="x", start=datetime(2026, 11, 3, 9, tzinfo=UTC), end=datetime(2026, 11, 4, tzinfo=UTC))
    assert plan_losses(both.component, Capabilities({**dict.fromkeys(Feature, Support.FULL), **table})) == []


def test_an_rdate_is_lost_where_only_recurrence_rdate_is_unsupported() -> None:
    """Zimbra drops every RDATE and keeps an RRULE."""
    event = Event.new(summary="x", start=datetime(2026, 11, 5, 10, tzinfo=UTC))
    event.component.add("RRULE", {"FREQ": "WEEKLY", "COUNT": 2})
    event.component.add("RDATE", [datetime(2026, 11, 26, 10, tzinfo=UTC)])
    table = {**dict.fromkeys(Feature, Support.FULL), Feature.RECURRENCE_RDATE: Support.UNSUPPORTED}
    [(loss, apply)] = plan_losses(event.component, Capabilities(table))
    assert (loss.property, loss.stored) == ("RDATE", None)
    apply()
    assert "RDATE" not in event.component
    assert "RRULE" in event.component
    no_recurrence = {**table, Feature.RECURRENCE: Support.UNSUPPORTED}
    again = Event.new(summary="x", start=datetime(2026, 11, 5, 10, tzinfo=UTC))
    again.component.add("RDATE", [datetime(2026, 11, 26, 10, tzinfo=UTC)])
    assert [loss.property for loss, _ in plan_losses(again.component, Capabilities(no_recurrence))] == ["RDATE"]


def test_a_tasks_rdate_is_kept_where_only_events_lose_theirs() -> None:
    """The hint is probed on events: on a calendar holding tasks too, a task's RDATE is not dropped."""
    task = Task.new(summary="x", start=datetime(2026, 11, 5, 10, tzinfo=UTC))
    task.component.add("RDATE", [datetime(2026, 11, 26, 10, tzinfo=UTC)])
    table = {**dict.fromkeys(Feature, Support.FULL), Feature.RECURRENCE_RDATE: Support.UNSUPPORTED}
    assert plan_losses(task.component, Capabilities(table)) == []


def test_an_override_of_a_dropped_rdate_goes_with_it() -> None:
    """An override of an RDATE occurrence would be an orphan once the RDATE is gone; one the RRULE gives stays."""
    master = Event.new(summary="x", start=datetime(2026, 11, 5, 10, tzinfo=UTC))
    master.component.add("RRULE", {"FREQ": "WEEKLY", "COUNT": 2})
    master.component.add("RDATE", [datetime(2026, 11, 26, 10, tzinfo=UTC)])
    calendar = master.icalendar
    for day in (26, 12):  # the RDATE's occurrence, and the RRULE's second
        override = icalendar.Event()
        override.add("UID", master.uid)
        override.add("DTSTAMP", datetime(2026, 11, 1, tzinfo=UTC))
        override.add("RECURRENCE-ID", datetime(2026, 11, day, 10, tzinfo=UTC))
        override.add("DTSTART", datetime(2026, 11, day, 11, tzinfo=UTC))
        override.add("SUMMARY", f"moved {day}")
        calendar.add_component(override)
    table = {**dict.fromkeys(Feature, Support.FULL), Feature.RECURRENCE_RDATE: Support.UNSUPPORTED}
    [(loss, apply)] = plan_calendar_losses(calendar, Capabilities(table))
    assert (loss.property, loss.sent, loss.stored) == ("RECURRENCE-ID", "20261126T100000Z", None)
    apply()
    assert [str(c["SUMMARY"]) for c in calendar.walk("VEVENT") if "RECURRENCE-ID" in c] == ["moved 12"]


def overridden(start: date, rrule: dict[str, Any] | None, rdate: Any, end: date | None = None) -> icalendar.Calendar:
    """A master with an RDATE and an override of the RDATE's occurrence, which moves it by an hour."""
    master = Event.new(summary="x", start=start)
    if end is not None:
        master.component.add("DTEND", end)
    if rrule is not None:
        master.component.add("RRULE", rrule)
    master.component.add("RDATE", [rdate])
    calendar = master.icalendar
    when = rdate[0] if isinstance(rdate, tuple) else rdate
    override = icalendar.Event()
    override.add("UID", master.uid)
    override.add("DTSTAMP", datetime(2026, 11, 1, tzinfo=UTC))
    override.add("RECURRENCE-ID", when)
    override.add("DTSTART", when)
    override.add("SUMMARY", "moved")
    calendar.add_component(override)
    return calendar


@pytest.mark.parametrize(
    ("calendar", "orphaned"),
    [
        pytest.param(
            overridden(
                datetime(2026, 11, 5, 9, tzinfo=UTC),
                {"FREQ": "DAILY", "COUNT": 3},
                datetime(2026, 11, 6, 10, tzinfo=UTC),
                end=datetime(2026, 11, 5, 11, tzinfo=UTC),
            ),
            True,
            id="an-rrule-occurrence-overlaps-it",
        ),
        pytest.param(
            overridden(
                datetime(2026, 11, 5, 10, tzinfo=UTC),
                {"FREQ": "WEEKLY", "COUNT": 2},
                (datetime(2026, 11, 26, 10, tzinfo=UTC), timedelta(hours=1)),
            ),
            True,
            id="period",
        ),
        pytest.param(
            overridden(date(2026, 11, 5), {"FREQ": "WEEKLY", "COUNT": 2}, date(2026, 11, 26)), True, id="all-day"
        ),
        pytest.param(
            overridden(date(2026, 11, 5), {"FREQ": "WEEKLY", "COUNT": 3}, date(2026, 11, 12)),
            False,
            id="all-day-the-rrule-gives-it-too",
        ),
        pytest.param(
            overridden(datetime(2026, 11, 5, 10, tzinfo=UTC), None, datetime(2026, 11, 26, 10, tzinfo=UTC)),
            True,
            id="no-rrule",
        ),
    ],
)
def test_an_rdate_occurrences_override_is_orphaned_unless_the_rrule_starts_one_there(
    calendar: icalendar.Calendar, orphaned: bool
) -> None:
    table = {**dict.fromkeys(Feature, Support.FULL), Feature.RECURRENCE_RDATE: Support.UNSUPPORTED}
    changes = plan_calendar_losses(calendar, Capabilities(table))
    assert [loss.property for loss, _ in changes] == (["RECURRENCE-ID"] if orphaned else [])
