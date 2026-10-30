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
