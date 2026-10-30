"""What a user of the library does, step by step, on every kind of backend.

These tests are optimized for readability: one story each.

These tests are slightly redundant, the conformance suites should
ensure in detail that everything works as intended.

When the backend cannot support the scenario, the test asserts that
this is properly flagged (``UnsupportedError`` or ``LossyWriteError``).

Events, tasks and journals are kept apart, as many servers and most
users keep them: a calendar, a task list and a journal.  Where a backend
has only one collection, all three are that one.

Add caldav's docker servers with ``--caldav-servers=Baikal,Nextcloud``
(see ``conftest.py``).

"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from conftest import caldav_server_names, running_caldav_server, wait_out_search_cache
from icalendar_searcher import Searcher

from calendaring import (
    Backend,
    Collection,
    Component,
    ConflictError,
    Event,
    Feature,
    LossyWriteError,
    Support,
    SyncItem,
    UnsupportedError,
)
from calendaring.memory import MINIMAL, TRACKER, MemoryBackend

LOCAL = [
    "memory",
    "memory, tracker profile",
    "memory, minimal profile",
    "files, vdir",
    "files, one collection directory",
    "files, one .ics file",
]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "where" in metafunc.fixturenames:
        metafunc.parametrize("where", LOCAL + caldav_server_names(metafunc.config))


def open_backend(where: str, directory: Path, request: pytest.FixtureRequest) -> Backend:
    """An empty backend of the kind named, as a user's configuration would open it."""
    match where:
        case "memory":
            return MemoryBackend()
        case "memory, tracker profile":
            return MemoryBackend(TRACKER)
        case "memory, minimal profile":
            return MemoryBackend(MINIMAL)
        case "files, vdir":
            directory.mkdir()
            return Backend.open(str(directory))
        case "files, one collection directory":
            directory.mkdir()
            (directory / "displayname").write_text("My calendar")
            return Backend.open(str(directory))
        case "files, one .ics file":
            directory.mkdir()
            return Backend.open(str(directory / "calendar.ics"))
    server = running_caldav_server(request, where)
    backend = Backend.open(server.url, kind="caldav", **server.options)
    wait_out_search_cache(backend, request.getfixturevalue("monkeypatch"))
    return backend


# Next week: a fixed date would one day be past every server's time range.
TODAY = datetime.now(UTC).date()
MONDAY = TODAY + timedelta(days=7 - TODAY.weekday())


def day(n: int) -> date:
    """A day of the week these stories take place in: 0 is its Monday."""
    return MONDAY + timedelta(days=n)


def at(n: int, hour: int) -> datetime:
    return datetime.combine(day(n), time(hour), tzinfo=UTC)


def summaries(items: Sequence[SyncItem]) -> list[str]:
    return sorted(str(i.component["SUMMARY"]) for i in items)


def unsupported(calendar: Collection, feature: Feature) -> bool:
    """The calendar says it cannot do this.  ``UNKNOWN`` is not that: the operation is attempted."""
    return calendar.capabilities.level(feature) is Support.UNSUPPORTED


@pytest.fixture
def backend(where: str, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Backend]:
    with open_backend(where, tmp_path / "mine", request) as backend:
        yield backend


def fresh_calendar(
    backend: Backend, request: pytest.FixtureRequest, kind: Component, label: str = ""
) -> Iterator[Collection]:
    """A new collection for ``kind`` where the backend can make one; else the one it has, as new as the backend.

    Where the backend can make none for ``kind``, a story marked ``checks_refusal`` gets one for another
    kind, and checks the refusal there; any other story is skipped.
    """
    name = f"scenario {request.node.originalname} ({kind.value}{label})"
    on_a_server = backend.kind == "caldav"
    if backend.capabilities.level(Feature.CREATE_COLLECTION) is Support.UNSUPPORTED:
        if on_a_server:
            pytest.skip("the server cannot make calendars, and the stories must not write into the user's own")
        yield backend.collections()[0]
        return
    if on_a_server and backend.capabilities.level(Feature.DELETE_COLLECTION) is Support.UNSUPPORTED:
        pytest.skip("the server cannot delete calendars, so every story would leave one behind")
    # A tracker makes no calendar for events, Bedework none for tasks, and many servers none for journals.
    others = [c for c in Component if c is not kind] if request.node.get_closest_marker("checks_refusal") else []
    for asked in [kind, *others]:
        try:
            calendar = backend.create_collection(name, components=(asked,))
            break
        except UnsupportedError:
            continue
    else:
        pytest.skip(f"the backend cannot make a collection for {kind.value}")
    yield calendar
    if backend.capabilities.level(Feature.DELETE_COLLECTION) is not Support.UNSUPPORTED:
        calendar.delete_collection()


@pytest.fixture
def calendar(backend: Backend, request: pytest.FixtureRequest) -> Iterator[Collection]:
    """A calendar for events, for the test alone, holding nothing yet."""
    yield from fresh_calendar(backend, request, Component.EVENT)


@pytest.fixture
def tasklist(backend: Backend, request: pytest.FixtureRequest) -> Iterator[Collection]:
    """A task list for the test alone, holding nothing yet."""
    yield from fresh_calendar(backend, request, Component.TASK)


@pytest.fixture
def journal(backend: Backend, request: pytest.FixtureRequest) -> Iterator[Collection]:
    """A journal for the test alone, holding nothing yet."""
    yield from fresh_calendar(backend, request, Component.JOURNAL)


@pytest.fixture
def other_tasklist(
    backend: Backend, where: str, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Collection]:
    """A second task list: on the same backend, or on another one of the same kind if it holds only one."""
    if backend.capabilities.level(Feature.CREATE_COLLECTION) is not Support.UNSUPPORTED:
        yield from fresh_calendar(backend, request, Component.TASK, ", other")
        return
    with open_backend(where, tmp_path / "other", request) as other:
        yield other.collections()[0]


@pytest.mark.checks_refusal
def test_a_new_calendar_is_listed_and_can_be_renamed(backend: Backend, calendar: Collection) -> None:
    ## The calendar fixture is a freshly made collection
    ## backend.collections() should list up all collections, including calendar
    assert calendar.id in [c.id for c in backend.collections()]

    ## Can the backend set a name on a collection?
    ## If not, assert UnsupportedError is raised when trying to rename it
    if unsupported(calendar, Feature.COLLECTION_SET_NAME):
        with pytest.raises(UnsupportedError):
            calendar.set_name("Renamed")
        assert backend.collection(calendar.id).name != "Renamed"
        return

    ## Do the rename
    calendar.set_name("Renamed")

    # Ask the backend again, rather than trusting the object we renamed.
    assert backend.collection(calendar.id).name == "Renamed"


@pytest.mark.checks_refusal
def test_an_event_is_added_found_changed_and_deleted(calendar: Collection) -> None:

    ## Assert that we get an appropriate error if trying to insert an event
    ## into a collection that cannot hold events
    if unsupported(calendar, Feature.COMPONENT_EVENT):
        # A task tracker holds no events.
        with pytest.raises(UnsupportedError):
            calendar.add_event(summary="Planning meeting", start=at(0, 9), end=at(0, 10))
        assert calendar.search() == []
        return

    ## Adding an event into the collection
    meeting = calendar.add_event(summary="Planning meeting", start=at(0, 9), end=at(0, 10))

    ## Do a date search for events
    found = calendar.events(start=at(0, 0), end=at(1, 0))
    ## Assert event is found
    assert [e.uid for e in found] == [meeting.uid]

    ## Editing an event
    meeting.component.summary = "Planning meeting, moved to room 2"
    meeting.save()

    ## Asserting the edit can be read back from the collection
    assert calendar.reload(meeting).component.summary == "Planning meeting, moved to room 2"

    meeting.delete()
    assert calendar.events() == []


@pytest.mark.checks_refusal
def test_a_task_is_completed_and_uncompleted(tasklist: Collection) -> None:
    """The plann use case: add a task, tick it off, and change one's mind."""

    ## A collection that cannot hold tasks says so, rather than storing something else
    if unsupported(tasklist, Feature.COMPONENT_TASK):
        with pytest.raises(UnsupportedError):
            tasklist.add_task(summary="Buy milk")
        assert tasklist.search() == []
        return

    task = tasklist.add_task(summary="Buy milk")
    assert [t.uid for t in tasklist.tasks(include_completed=False)] == [task.uid]

    task.complete()
    # A completed task drops out of the to-do list, but is not gone.
    assert tasklist.tasks(include_completed=False) == []
    assert tasklist.reload(task).component.status == "COMPLETED"

    ## Ticked off by mistake: the milk is still in the shop
    ## (or maybe it was spilled on the way?)
    if unsupported(tasklist, Feature.TASK_UNCOMPLETE):
        # GMX keeps a task completed: it says so, rather than seeming to reopen it
        with pytest.raises(UnsupportedError):
            task.uncomplete()
        return
    task.uncomplete()
    assert [t.uid for t in tasklist.tasks(include_completed=False)] == [task.uid]


def test_a_due_date_is_kept(tasklist: Collection) -> None:
    """Add a task with a due-date"""
    ## For a task, icalendar's ``end`` is DUE.

    ## Due-date unsupported?  Then we should get a LossyWriteError
    if unsupported(tasklist, Feature.TASK_DUE):
        with pytest.raises(LossyWriteError):
            tasklist.add_task(summary="Pay the rent", end=day(-1))
        assert tasklist.tasks() == []
        return

    ## Add the task, reload the task list
    task = tasklist.add_task(summary="Pay the rent", end=day(-1))
    assert tasklist.reload(task).component.end == day(-1)


def test_a_calendar_for_events_refuses_tasks(calendar: Collection) -> None:
    """The backend is never asked to store what the calendar says it does not hold."""
    if not unsupported(calendar, Feature.COMPONENT_TASK):
        pytest.skip("the backend made a calendar for every component anyway (SOGo does), or has only one")
    with pytest.raises(UnsupportedError):
        calendar.add_task(summary="Buy milk")
    assert calendar.search() == []


@pytest.mark.checks_refusal
def test_a_journal_entry_is_written_where_the_backend_keeps_journals(journal: Collection) -> None:
    if unsupported(journal, Feature.COMPONENT_JOURNAL):
        # Several CalDAV servers (SOGo, Zimbra, CCS, OX) have no journals, and nor do trackers.
        with pytest.raises(UnsupportedError):
            journal.add_journal(summary="Notes from the planning meeting", start=day(0))
        assert journal.search() == []
        return
    notes = journal.add_journal(summary="Notes from the planning meeting", start=day(0))
    assert [j.uid for j in journal.journals()] == [notes.uid]


def test_a_recurring_event_is_expanded_into_occurrences(calendar: Collection) -> None:
    if unsupported(calendar, Feature.COMPONENT_EVENT):
        pytest.skip("the backend holds no events; test_an_event_is_added_... checks the refusal")
    standup = Event.new(summary="Stand-up", start=at(0, 9), end=at(0, 10))
    standup.component.add("RRULE", {"FREQ": "DAILY", "COUNT": 5})
    if unsupported(calendar, Feature.RECURRENCE):
        # A backend that cannot store the rule must not silently store a one-off event.
        with pytest.raises(LossyWriteError):
            calendar.add(standup)
        assert calendar.events() == []
        return
    calendar.add(standup)

    # Wednesday to Friday: three of the five mornings.
    mornings = calendar.events(start=at(2, 0), end=at(5, 0), expand=True)
    assert sorted(m.component.start for m in mornings) == [at(2, 9), at(3, 9), at(4, 9)]
    assert all(m.is_occurrence and m.uid == standup.uid for m in mornings)


def test_tasks_are_found_by_category_and_by_text(tasklist: Collection) -> None:

    ## The unsupported test - either it's supported or it should raise the right error
    if unsupported(tasklist, Feature.CATEGORIES):
        with pytest.raises(LossyWriteError):
            tasklist.add_task(summary="Write the report", categories=["work"])
        # Not stored without its category, either.
        assert tasklist.tasks() == []
        return

    ## Let's add some tasks to the tasklist ...
    tasklist.add_task(summary="Write the report", categories=["work"])
    tasklist.add_task(summary="Book the dentist", categories=["home"])

    ## ... and then search for them by category ...
    by_category = Searcher(todo=True)
    by_category.add_property_filter("CATEGORIES", "work")
    assert summaries(tasklist.search(by_category)) == ["Write the report"]

    ## and by summary
    by_text = Searcher(todo=True)
    by_text.add_property_filter("SUMMARY", "dentist", operator="contains")
    assert summaries(tasklist.search(by_text)) == ["Book the dentist"]


def test_changes_tells_a_sync_client_what_happened(tasklist: Collection) -> None:
    """What a sync tool does: take a token, come back later, and get only the news."""

    ## Populate the task list with some tasks
    tasklist.add_task(summary="Kept")
    early = tasklist.add_task(summary="Edited early")
    edited = tasklist.add_task(summary="Edited")
    deleted = tasklist.add_task(summary="Deleted")

    ## again, ensure the proper exception is raised if the backend has
    ## no change feed
    if unsupported(tasklist, Feature.CHANGES):
        with pytest.raises(UnsupportedError):
            tasklist.changes()
        return

    ## A change made before the token was taken is old news
    early.component.summary = "Edited early, and then some"
    early.save()

    ## The token is the server's sync-token, or else a snapshot of every
    ## etag; where the server's tokens are unreliable (e.g. time-based),
    ## the library uses the etag snapshot.
    token = tasklist.changes().token

    ## Edit one, delete one, add one
    edited.component.summary = "Edited, and then some"
    edited.save()
    deleted.delete()
    added = tasklist.add_task(summary="Added")

    ## Fetch the changes
    news = tasklist.changes(token)

    ## news.changed and news.deleted should contain
    ## exactly the elements that were changed/deleted and nothing more
    assert sorted(i.uid for i in news.changed) == sorted([edited.uid, added.uid])
    assert news.deleted == [deleted.native_id]


def test_a_stale_copy_cannot_overwrite_a_newer_one(tasklist: Collection) -> None:
    """Two clients edit the same task: the second one to save is told, not silently overwritten."""

    ## A backend that overwrites by design has no refusal to check
    if unsupported(tasklist, Feature.WRITE_CONDITIONAL):
        pytest.skip("the backend overwrites by design, so there is no refusal to check")

    tasklist.add_task(summary="Shared task")
    [mine] = tasklist.tasks()
    [theirs] = tasklist.tasks()

    theirs.component.summary = "Their edit"
    theirs.save()

    mine.component.summary = "My edit"
    with pytest.raises(ConflictError):
        mine.save()
    assert tasklist.reload(mine).component.summary == "Their edit"


def test_a_task_is_moved_to_another_list(tasklist: Collection, other_tasklist: Collection) -> None:
    task = tasklist.add_task(summary="Renew the passport")

    ## Not all task lists support the move operation
    if unsupported(tasklist, Feature.MOVE):
        with pytest.raises(UnsupportedError):
            tasklist.move(task, other_tasklist)
        assert [t.uid for t in tasklist.tasks()] == [task.uid]
        assert other_tasklist.tasks() == []
        return

    ## Oops.  The tasklist is my company work tasklist, renewing the passport
    ## should go into my personal other_tasklist.  Let's move it ...
    tasklist.move(task, other_tasklist)

    assert tasklist.tasks() == []
    ## By summary, not UID: a tracker gives what it moves a new identity, as moving an issue does.
    assert summaries(other_tasklist.tasks()) == ["Renew the passport"]

    ## (Moving it to a task list on another backend: the next story)


def test_a_task_is_moved_to_another_backend(
    tasklist: Collection, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """From one backend to a task list kept in local files: not atomic, one step after the other."""
    task = tasklist.add_task(summary="Renew the passport")
    with open_backend("files, one collection directory", tmp_path / "personal", request) as personal:
        [personal_tasks] = personal.collections()

        ## Not all task lists support the move operation
        if unsupported(tasklist, Feature.MOVE):
            with pytest.raises(UnsupportedError):
                tasklist.move(task, personal_tasks)
            assert [t.uid for t in tasklist.tasks()] == [task.uid]
            assert personal_tasks.tasks() == []
            return

        ## Between backends nothing is atomic: the task is added to the
        ## target and deleted here, one after the other
        moved = tasklist.move(task, personal_tasks)
        assert moved.collection is personal_tasks
        assert moved.uid == task.uid

        assert tasklist.tasks() == []
        assert summaries(personal_tasks.tasks()) == ["Renew the passport"]


@pytest.mark.checks_refusal
def test_time_is_logged_against_a_task(tasklist: Collection, calendar: Collection) -> None:
    """Roadmap 1.6: time entries are events, in the task's own collection or in a calendar beside it."""
    ## Some systems have no task lists: there is no task to log time against.
    if unsupported(tasklist, Feature.COMPONENT_TASK):
        with pytest.raises(UnsupportedError):
            tasklist.add_task(summary="Fix the bike")
        return

    task = tasklist.add_task(summary="Fix the bike")

    if unsupported(tasklist, Feature.TIME_LOG):
        # A task list holding no events logs time in the calendar.
        tasklist.time_log_collection = calendar
    if unsupported(tasklist.time_log_collection or tasklist, Feature.TIME_LOG):
        # A tracker keeps no events to log time in.
        with pytest.raises(UnsupportedError):
            task.log_time(spent=timedelta(hours=1), day=day(0))
        assert summaries(tasklist.search()) == ["Fix the bike"]
        return

    ## I worked from 9 to 10
    task.log_time(start=at(0, 9), end=at(0, 10))

    ## I worked for 30 minutes more
    task.log_time(spent=timedelta(minutes=30), day=day(1))

    ## That makes 1.5 hours
    assert task.time_spent() == timedelta(hours=1, minutes=30)

    ## The task itself is still the only thing the to-do list shows.
    assert [t.uid for t in tasklist.tasks()] == [task.uid]
