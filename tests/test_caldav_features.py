"""The CalDAV backend's capability table follows caldav's hints as API_DESIGN.md §5.1 says (roadmap 2.1)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("caldav")

from caldav.compatibility_hints import FeatureSet  # noqa: E402

from calendaring._caldav_features import (  # noqa: E402
    backend_capabilities,
    collection_capabilities,
    stored_components,
)
from calendaring.capabilities import Feature, Support  # noqa: E402
from calendaring.types import Component  # noqa: E402

ALL = frozenset(Component)


def features(table: dict[str, Any] | None = None) -> Any:
    return FeatureSet(table)  # type: ignore[no-untyped-call]  # caldav leaves it unannotated


def needs_hint(name: str) -> pytest.MarkDecorator:
    """For a hint newer than the caldav installed (due in caldav 3.8)."""
    return pytest.mark.skipif(name not in FeatureSet.FEATURES, reason=f"caldav has no {name} hint yet")


def hinted(**levels: str) -> Any:
    """A server caldav has hints for: ``sync_token="unsupported"`` sets ``sync-token``."""
    return features({name.replace("_", "-"): {"support": level} for name, level in levels.items()})


def test_an_unhinted_server_is_unknown_not_unsupported() -> None:
    table = collection_capabilities(features(), ALL)
    assert table.level(Feature.RECURRENCE) is Support.UNKNOWN
    assert table.level(Feature.COMPONENT_TASK) is Support.UNKNOWN
    assert backend_capabilities(features()).level(Feature.CREATE_COLLECTION) is Support.UNKNOWN


def test_what_the_server_stores_is_unknown_without_hints() -> None:
    unhinted = collection_capabilities(features(), ALL)
    known = collection_capabilities(hinted(save_load="full"), ALL)
    for feature in (
        Feature.PROPERTIES_PASSTHROUGH,
        Feature.CATEGORIES,
        Feature.WRITE_CONDITIONAL,
        Feature.TASK_DUE,
        Feature.ALARMS,
        Feature.DATES,
        Feature.TIMEZONES,
        Feature.TIME_LOG,
    ):
        assert unhinted.level(feature) is Support.UNKNOWN, feature
        assert known.level(feature) is Support.FULL, feature


def test_what_the_backend_does_itself_is_declared_whatever_the_server() -> None:
    table = collection_capabilities(features(), ALL)
    assert table.level(Feature.MOVE) is Support.EMULATED
    assert table.level(Feature.CHANGES_KNOWN) is Support.FULL
    assert table.details(Feature.CHANGES) == {"method": "etag-listing"}
    assert table.details(Feature.SEARCH_TIME_RANGE) == {"components": []}


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("full", Support.FULL),
        ("quirk", Support.FULL),
        ("unsupported", Support.UNSUPPORTED),
        ("broken", Support.UNSUPPORTED),
        ("ungraceful", Support.UNSUPPORTED),
        ("fragile", Support.UNKNOWN),
    ],
)
def test_levels_map_as_section_5_1_says(level: str, expected: Support) -> None:
    features = hinted(**{"create-calendar": level, "save-load": "full"})
    assert backend_capabilities(features).level(Feature.CREATE_COLLECTION) is expected


def test_time_zones_follow_the_servers_hint() -> None:
    server = features({"save-load": {"support": "full"}, "save-load.event.timezone": {"support": "unsupported"}})
    assert collection_capabilities(server, ALL).level(Feature.TIMEZONES) is Support.UNSUPPORTED


def test_a_calendar_without_a_component_cannot_hold_it() -> None:
    table = collection_capabilities(hinted(save_load="full"), frozenset({Component.TASK}))
    assert table.level(Feature.COMPONENT_TASK) is Support.FULL
    assert table.level(Feature.COMPONENT_EVENT) is Support.UNSUPPORTED


def test_a_calendar_without_events_has_nowhere_to_log_time() -> None:
    tasks = collection_capabilities(hinted(save_load="full"), frozenset({Component.TASK}))
    assert tasks.level(Feature.TIME_LOG) is Support.UNSUPPORTED
    assert "time_log_collection" in (tasks[Feature.TIME_LOG].note or "")
    both = collection_capabilities(hinted(save_load="full"), frozenset({Component.TASK, Component.EVENT}))
    assert both.level(Feature.TIME_LOG) is Support.FULL


def test_no_time_log_where_the_server_strips_related_to() -> None:
    """A time entry points at its task with RELATED-TO: Zimbra and robur store the entry without it."""
    server = features({"save-load": {"support": "full"}, "save-load.icalendar.related-to": {"support": "unsupported"}})
    events = collection_capabilities(server, frozenset({Component.EVENT}))
    assert events.level(Feature.TIME_LOG) is Support.UNSUPPORTED
    assert "RELATED-TO" in (events[Feature.TIME_LOG].note or "")


def test_no_uncomplete_where_the_server_keeps_a_task_completed() -> None:
    """GMX answers the PUT of a pending task with 204, and loads it back completed."""
    server = features({"save-load": {"support": "full"}, "save-load.todo.uncomplete": {"support": "unsupported"}})
    assert collection_capabilities(server, ALL).level(Feature.TASK_UNCOMPLETE) is Support.UNSUPPORTED


def test_a_conditional_delete_is_emulated_where_the_server_ignores_if_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """SOGo, Zimbra, Bedework and Posteo delete on a stale If-Match; a caldav that reads the ETag first emulates it."""
    from caldav.calendarobjectresource import CalendarObjectResource

    server = features({"save-load": {"support": "full"}, "delete.if-match": {"support": "broken"}})
    monkeypatch.setattr(CalendarObjectResource, "_ignores_delete_if_match", lambda *a: True, raising=False)
    assert collection_capabilities(server, ALL).level(Feature.WRITE_CONDITIONAL) is Support.EMULATED
    monkeypatch.delattr(CalendarObjectResource, "_ignores_delete_if_match")
    older = collection_capabilities(server, ALL)  # a caldav that deletes whatever the ETag
    assert older.level(Feature.WRITE_CONDITIONAL) is Support.UNSUPPORTED


def test_time_entries_lose_their_users_where_the_server_refuses_participants() -> None:
    """CCS, Bedework 3, robur, GMX, Ox and Zimbra refuse an event holding a PARTICIPANT."""
    server = hinted(save_load="full", **{"save-load.icalendar.participant": "ungraceful"})
    table = collection_capabilities(server, ALL)
    assert table.level(Feature.TIME_LOG) is Support.LOSSY
    assert table.details(Feature.TIME_LOG)["lost"] == ["users"]
    tasks_only = collection_capabilities(server, frozenset({Component.TASK}))
    assert tasks_only.level(Feature.TIME_LOG) is Support.UNSUPPORTED  # nowhere to log time stays the reason


@pytest.mark.parametrize(
    ("hint", "feature"),
    [
        ("save-load.todo.attendee", Feature.ATTENDEES),  # Ox: 500
        ("save-load.todo.recurrences", Feature.RECURRENCE),  # Ox: 400
        ("save-load.icalendar.x-property", Feature.PROPERTIES_PASSTHROUGH),  # Ox drops it from a task
        ("save-load.icalendar.x-property", Feature.TASK_PLANNED),  # X-PYCAL-PLANNED-*
        ("save-load.icalendar.unknown-property", Feature.TASK_ESTIMATE),  # Ox, Zimbra: ESTIMATED-DURATION
    ],
)
def test_what_a_task_list_cannot_store(hint: str, feature: Feature) -> None:
    """These hints are probed on a task: a calendar of events keeps the feature, and so does one holding
    events too, whose events would otherwise lose it (levels are per collection, not per component)."""
    server = features(
        {"save-load": {"support": "full"}, hint: {"support": "ungraceful", "behaviour": "dropped, in a task"}}
    )
    assert collection_capabilities(server, frozenset({Component.TASK})).level(feature) is Support.UNSUPPORTED
    if feature in (Feature.TASK_PLANNED, Feature.TASK_ESTIMATE):  # nothing of an event's to lose
        assert collection_capabilities(server, ALL).level(feature) is Support.UNSUPPORTED
    else:
        assert collection_capabilities(server, ALL).level(feature) is Support.FULL
        assert collection_capabilities(server, frozenset({Component.EVENT})).level(feature) is Support.FULL


def test_x_properties_dropped_from_events_too_lower_every_collection() -> None:
    """The x-property hint names where it was dropped; not only from a task means from events too."""
    server = features(
        {
            "save-load": {"support": "full"},
            "save-load.icalendar.x-property": {
                "support": "unsupported",
                "behaviour": "X-CALDAV-SERVER-TESTER is dropped, in an event",
            },
        }
    )
    events = collection_capabilities(server, frozenset({Component.EVENT}))
    assert events.level(Feature.PROPERTIES_PASSTHROUGH) is Support.UNSUPPORTED


def test_an_x_property_hint_with_an_empty_behaviour_is_read() -> None:
    """A config file may write ``behaviour:`` with no value."""
    server = features(
        {
            "save-load": {"support": "full"},
            "save-load.icalendar.x-property": {"support": "unsupported", "behaviour": None},
        }
    )
    events = collection_capabilities(server, frozenset({Component.EVENT}))
    assert events.level(Feature.PROPERTIES_PASSTHROUGH) is Support.UNSUPPORTED


@needs_hint("save-load.todo.status")
def test_statuses_a_task_list_does_not_keep() -> None:
    """Zimbra stores PENDING and FAILED as NEEDS-ACTION and refuses CANCELLED."""
    from caldav import compatibility_hints

    server = features({"save-load": {"support": "full"}, **compatibility_hints.zimbra_8_8})
    table = collection_capabilities(server, frozenset({Component.TASK}))
    assert table.level(Feature.TASK_STATUS) is Support.LOSSY
    assert table.details(Feature.TASK_STATUS)["values"] == ["NEEDS-ACTION", "IN-PROCESS", "COMPLETED"]
    assert table.details(Feature.TASK_STATUS)["map"] == {"PENDING": "NEEDS-ACTION", "FAILED": "NEEDS-ACTION"}
    assert collection_capabilities(server, frozenset({Component.EVENT})).level(Feature.TASK_STATUS) is not Support.LOSSY


@needs_hint("save-load.todo.status.rfc10051")
def test_rfc_10051_statuses_lost_where_the_rfc_5545_ones_are_kept() -> None:
    """Ox keeps RFC 5545's four statuses and drops PENDING and FAILED."""
    from caldav import compatibility_hints

    server = features({"save-load": {"support": "full"}, **compatibility_hints.ox})
    table = collection_capabilities(server, frozenset({Component.TASK}))
    details = table.details(Feature.TASK_STATUS)
    assert details["values"] == ["NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"]
    assert details["map"] == {}
    assert "does not advertise RFC 10051" in (table[Feature.TASK_STATUS].note or "")


@needs_hint("save-load.todo.status.rfc10051")
def test_a_parent_status_hint_alone_is_not_counted_twice() -> None:
    """caldav answers the child with the parent's node where only the parent is set."""
    hint = {"support": "ungraceful", "values": ["NEEDS-ACTION", "IN-PROCESS", "COMPLETED"], "refused": ["CANCELLED"]}
    server = features({"save-load": {"support": "full"}, "save-load.todo.status": hint})
    details = collection_capabilities(server, frozenset({Component.TASK})).details(Feature.TASK_STATUS)
    assert details["values"] == ["NEEDS-ACTION", "IN-PROCESS", "COMPLETED"]


@needs_hint("save-load.todo.status.rfc10051")
def test_statuses_not_known_to_be_kept_are_not_declared_kept() -> None:
    """A hint the tester could not settle says nothing kept: PENDING and FAILED are left out."""
    server = features(
        {
            "save-load": {"support": "full"},
            "save-load.todo.status": {"support": "unsupported", "values": ["NEEDS-ACTION"]},
            "save-load.todo.status.rfc10051": {"support": "unknown"},
        }
    )
    details = collection_capabilities(server, frozenset({Component.TASK})).details(Feature.TASK_STATUS)
    assert details["values"] == ["NEEDS-ACTION"]


@needs_hint("save-load.todo.priority")
def test_priority_zero_is_left_out_not_sent_as_the_servers_stand_in() -> None:
    """Ox keeps 1, 5 and 9 and stores 0 as 5; no PRIORITY means the same as 0."""
    from caldav import compatibility_hints

    server = features({"save-load": {"support": "full"}, **compatibility_hints.ox})
    details = collection_capabilities(server, frozenset({Component.TASK})).details(Feature.TASK_PRIORITY)
    assert details["values"] == [1, 5, 9]
    assert 0 not in details["map"]
    assert details["map"][7] == 9


@needs_hint("save-load.todo.priority")
def test_priorities_from_a_config_file_are_numbers() -> None:
    """A hint read from YAML or JSON may carry the priorities as strings."""
    hint = {"support": "unsupported", "values": ["1", "5", "9"], "map": {"0": "5", "2": "1"}}
    server = features({"save-load": {"support": "full"}, "save-load.todo.priority": hint})
    details = collection_capabilities(server, frozenset({Component.TASK})).details(Feature.TASK_PRIORITY)
    assert details["values"] == [1, 5, 9]
    assert details["map"] == {2: 1}


@needs_hint("save-load.icalendar.x-parameter")
def test_no_wire_writes_where_the_server_drops_x_parameters() -> None:
    """Ox and Zimbra drop SUMMARY;X-...: the bytes are not stored as sent."""
    server = hinted(save_load="full", **{"save-load.icalendar.x-parameter": "unsupported"})
    assert collection_capabilities(server, ALL).level(Feature.WRITE_WIRE) is Support.UNSUPPORTED


def test_no_native_id_in_a_task_list_where_the_server_renames_tasks_to_their_uid() -> None:
    """Ox keeps an event's name and names a task <uid>.ics."""
    server = features(
        {
            "save-load": {"support": "full"},
            "save-load.stable-url": {"support": "unsupported", "uid_ics_components": ["VTODO"]},
        }
    )
    tasks = collection_capabilities(server, frozenset({Component.TASK}))
    assert tasks.level(Feature.IDENTITY_CLIENT_NATIVE_ID) is Support.UNSUPPORTED
    events = collection_capabilities(server, frozenset({Component.EVENT}))
    assert events.level(Feature.IDENTITY_CLIENT_NATIVE_ID) is Support.FULL


def test_no_native_id_where_the_server_renames_every_object_to_its_uid() -> None:
    """Zimbra redirects a PUT under another name to <uid>.ics."""
    renaming = features(
        {"save-load": {"support": "full"}, "save-load.stable-url": {"support": "unsupported", "forces_uid_ics": True}}
    )
    assert collection_capabilities(renaming, ALL).level(Feature.IDENTITY_CLIENT_NATIVE_ID) is Support.UNSUPPORTED
    unstable = hinted(save_load="full", **{"save-load.stable-url": "unsupported"})  # Ox: other reasons
    assert collection_capabilities(unstable, ALL).level(Feature.IDENTITY_CLIENT_NATIVE_ID) is Support.FULL


def test_one_kind_per_calendar_where_the_server_refuses_several() -> None:
    """CCS refuses a MKCALENDAR naming VEVENT and VTODO; Bedework 3, Zimbra and Ox misreport one."""
    multiple = "create-calendar.with-supported-component-types.multiple"
    for support in ("unsupported", "broken"):
        server = features({"save-load": {"support": "full"}, multiple: {"support": support}})
        table = backend_capabilities(server)
        assert table.level(Feature.COLLECTION_MIXED) is Support.UNSUPPORTED
        assert table.level(Feature.CREATE_COLLECTION) is Support.LOSSY
    ccs = features(
        {
            "save-load": {"support": "full"},
            "save-load.journal": {"support": "unsupported"},
            multiple: {"support": "unsupported"},
        }
    )
    note = backend_capabilities(ccs)[Feature.CREATE_COLLECTION].note or ""
    assert "VJOURNAL" in note
    assert "one component only" in note
    table = backend_capabilities(hinted(save_load="full"))
    assert table.level(Feature.COLLECTION_MIXED) is Support.FULL
    assert table.level(Feature.CREATE_COLLECTION) is Support.FULL


def test_sync_token_is_used_only_where_deletions_are_reported_too() -> None:
    assert collection_capabilities(hinted(sync_token="full"), ALL).details(Feature.CHANGES) == {"method": "sync-token"}
    broken_delete = hinted(**{"sync-token": "full", "sync-token.delete": "unsupported"})
    assert collection_capabilities(broken_delete, ALL).details(Feature.CHANGES) == {"method": "etag-listing"}


def test_sync_token_is_not_used_where_an_expired_token_is_answered_with_a_listing() -> None:
    # Stalwart, OX: 207 and everything for a token they never issued, so deletions would be lost.
    broken = hinted(**{"sync-token": "full", "sync-token.invalid-token": "broken"})
    assert collection_capabilities(broken, ALL).details(Feature.CHANGES) == {"method": "etag-listing"}


def test_time_range_narrows_events_only_where_recurrences_match_too() -> None:
    assert collection_capabilities(hinted(search="full"), ALL).details(Feature.SEARCH_TIME_RANGE) == {
        "components": ["VEVENT"]
    }
    no_implicit = hinted(**{"search": "full", "search.recurrences.includes-implicit.event": "unsupported"})
    assert collection_capabilities(no_implicit, ALL).details(Feature.SEARCH_TIME_RANGE) == {"components": []}


def test_time_range_is_not_sent_where_filtered_searches_lag_behind_writes() -> None:
    # Purelymail: a plain listing is current at once, a time-range REPORT answers from an index that lags
    lagging = features(
        {
            "search": {"support": "full"},
            "search-cache": {"behaviour": "delay", "delay": 180, "filtered-only": True},
        }
    )
    assert collection_capabilities(lagging, ALL).details(Feature.SEARCH_TIME_RANGE) == {"components": []}
    # Bedework lags in plain listings too: filtering on the client would not help, so the range is still sent
    everywhere = features({"search": {"support": "full"}, "search-cache": {"behaviour": "delay", "delay": 3}})
    assert collection_capabilities(everywhere, ALL).details(Feature.SEARCH_TIME_RANGE) == {"components": ["VEVENT"]}


def test_a_calendar_does_not_hold_what_the_server_refuses_to_store() -> None:
    # robur lists VJOURNAL as supported, then answers a journal with 400
    listed = frozenset({Component.EVENT, Component.TASK, Component.JOURNAL})
    refusing = hinted(save_load="full", **{"save-load.journal": "ungraceful"})
    assert stored_components(refusing, listed) == {Component.EVENT, Component.TASK}
    assert stored_components(hinted(save_load="full"), listed) == listed
    assert stored_components(features(), listed) == listed  # unhinted: nothing known


def test_a_calendar_is_made_lossily_where_the_server_ignores_the_component_set() -> None:
    server = hinted(save_load="full", create_calendar="full")
    assert backend_capabilities(server).level(Feature.CREATE_COLLECTION) is Support.FULL
    ignored = features(
        {
            "save-load": {"support": "full"},
            "create-calendar": {"support": "full"},
            "create-calendar.with-supported-component-types": {"support": "unsupported"},
        }
    )
    capability = backend_capabilities(ignored)[Feature.CREATE_COLLECTION]
    assert capability.level is Support.LOSSY
    assert "components" in (capability.note or "")


def test_a_calendar_is_made_lossily_where_the_server_cannot_store_a_component() -> None:
    # Infomaniak and CCS: a calendar asked for journals is made without them
    server = hinted(save_load="full", create_calendar="full", **{"save-load.journal": "unsupported"})
    capability = backend_capabilities(server)[Feature.CREATE_COLLECTION]
    assert capability.level is Support.LOSSY
    assert "VJOURNAL" in (capability.note or "")


def move_of(**hints: str) -> Any:
    return collection_capabilities(hinted(save_load="full", **hints), ALL)[Feature.MOVE]


def test_a_move_copies_then_deletes_where_a_uid_may_be_in_two_calendars() -> None:
    move = move_of(**{"save.duplicate-uid.cross-calendar": "full"})
    assert move.level is Support.EMULATED
    assert "order" not in move.details


def test_a_move_deletes_first_where_the_copy_would_be_refused() -> None:
    # CCS, Cyrus, Ox: the same UID in a second calendar is an error
    move = move_of(**{"save.duplicate-uid.cross-calendar": "ungraceful"})
    assert move.level is Support.EMULATED
    assert move.details["order"] == "delete-first"


def test_a_move_checks_what_the_copy_did_where_the_server_moves_or_ignores_it() -> None:
    # Zimbra moves the original when the copy is saved
    move = move_of(**{"save.duplicate-uid.cross-calendar": "unsupported"})
    assert move.level is Support.EMULATED
    assert move.details["order"] == "add-may-move"


def test_a_move_deletes_first_where_the_copy_would_be_dropped() -> None:
    server = features(
        {
            "save-load": {"support": "full"},
            "save.duplicate-uid.cross-calendar": {"support": "unsupported", "behaviour": "silently-ignored"},
        }
    )
    assert collection_capabilities(server, ALL)[Feature.MOVE].details["order"] == "delete-first"


def test_a_move_is_impossible_where_neither_a_copy_nor_a_reused_uid_is_allowed() -> None:
    hints = {"save.duplicate-uid.cross-calendar": "ungraceful", "save-load.reuse-deleted-uid": "unsupported"}
    assert move_of(**hints).level is Support.UNSUPPORTED


def test_a_copy_that_moves_needs_no_reused_uid() -> None:
    move = move_of(**{"save.duplicate-uid.cross-calendar": "unsupported", "save-load.reuse-deleted-uid": "unsupported"})
    assert move.details["order"] == "add-may-move"
