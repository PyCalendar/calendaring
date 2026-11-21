# Hand-written source; tools/generate_sync.py derives ../_sync/ from this file.
"""The in-memory reference backend passes the conformance suite, in seven profiles.

The library's four: a backend that can do everything, one shaped like an
issue tracker (tasks only, mapped statuses and priorities, assigned
identity), a writable one without most optional features, and a read-only
feed.  Three more here reach the branches those leave out; CI checks the
branch coverage of the suite.  The supported branches of
``recurrence.edit-occurrence``, ``recurrence.edit-this-and-future`` and
``task.complete-recurring`` wait for edit-icalendar (API_DESIGN.md Q3).

The second half is backends that each break one rule, and the suite test
that must catch it: a test that cannot fail tests nothing.
"""

from __future__ import annotations

import copy
import inspect
import time as time_module
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar

import icalendar
import pytest
from icalendar_searcher import Searcher

from calendaring import (
    AsyncBackend,
    AsyncCollection,
    AsyncItem,
    AsyncTask,
    BackendError,
    BrokenObject,
    Component,
    Feature,
    Item,
    ListingLimitError,
    NotFoundError,
    Support,
    SyncToken,
    Task,
    TransportError,
    VerificationError,
)
from calendaring._async.conformance import snapshot
from calendaring.loss import LossPolicy
from calendaring.memory import (
    FEED,
    FULL,
    MINIMAL,
    TRACKER,
    AsyncMemoryBackend,
    AsyncMemoryCollection,
    MemoryCalendar,
    MemoryObject,
    MemoryStore,
    Profile,
)
from calendaring.testing import AsyncConformance, fresh_uid
from calendaring.timetracking import TimeEntry
from calendaring.types import StoredObject


class TestFullProfile(AsyncConformance):
    async def open_backend(self) -> AsyncBackend:
        return AsyncMemoryBackend(profile=FULL)


class TestTrackerProfile(AsyncConformance):
    async def open_backend(self) -> AsyncBackend:
        return AsyncMemoryBackend(profile=TRACKER)


ONE_KIND = Profile(
    "one-kind",
    {**FULL.capabilities, Feature.COLLECTION_MIXED: Support.UNSUPPORTED, Feature.CREATE_COLLECTION: Support.LOSSY},
    FULL.components,
)


class TestOneKindProfile(AsyncConformance):
    """Every kind, but a collection for one at a time (CCS): each test gets one for the kind it writes."""

    async def open_backend(self) -> AsyncBackend:
        return AsyncMemoryBackend(profile=ONE_KIND)


class TestMinimalProfile(AsyncConformance):
    """It cannot make collections, so the suite borrows one that already holds something."""

    async def open_backend(self) -> AsyncBackend:
        backend = AsyncMemoryBackend(profile=MINIMAL)
        backend.store.calendars["default"].put(Task.new(summary="not the suite's").to_ical())
        return backend

    async def test_the_borrowed_collection_is_left_as_it_was(self) -> None:
        backend = await self.open_backend()
        self._made, self._borrowed = [], []
        collection = await self.open_collection(backend)
        before = {i.native_id for i in await collection.search()}
        await collection.add_task(summary="the suite's", uid=fresh_uid())
        await self.tidy_up(backend)
        assert {i.native_id for i in await collection.search()} == before


class TestReusedCollections(AsyncConformance):
    """Where making collections is rate-limited (Nextcloud), the suite keeps the ones it makes and uses them again.

    One backend serves every test of the class, so each test meets the
    collections the tests before it kept, as on the server.
    """

    shared: ClassVar[AsyncMemoryBackend | None] = None

    def reuses_collections(self, backend: AsyncBackend) -> bool:
        return True

    async def open_backend(self) -> AsyncBackend:
        if TestReusedCollections.shared is None:
            TestReusedCollections.shared = AsyncMemoryBackend(profile=FULL)
        return TestReusedCollections.shared

    async def test_a_collection_is_made_once_and_kept(self) -> None:
        backend = AsyncMemoryBackend(profile=FULL)
        self._made, self._borrowed = [], []
        first = await self.open_collection(backend)
        await first.add_task(summary="the suite's", uid=fresh_uid())
        await self.tidy_up(backend)
        assert first.id in [c.id for c in await backend.collections()]
        assert await first.search() == []
        other = await (await backend.collection(first.id)).add_task(summary="another client's")
        self._made, self._borrowed = [], []
        again = await self.open_collection(backend)
        assert again.id == first.id
        await again.add_task(summary="the suite's", uid=fresh_uid())
        await self.tidy_up(backend)
        assert [i.native_id for i in await again.search()] == [other.native_id]
        assert [c.name for c in await backend.collections()].count(first.name) == 1


class TestFeedProfile(AsyncConformance):
    async def open_backend(self) -> AsyncBackend:
        backend = AsyncMemoryBackend(profile=FEED)
        # A feed is read-only as a whole, which a backend may say as well (§5.2).
        backend.capabilities = backend.capabilities.updated({Feature.WRITE: Support.UNSUPPORTED})
        return backend

    async def seed(self, collection: AsyncCollection, items: Sequence[Item]) -> list[AsyncItem]:
        assert isinstance(collection, AsyncMemoryBackend.collection_class)
        native_ids = [collection.store.put(item.to_ical()) for item in items]
        return [await collection.get_by_native_id(n) for n in native_ids]

    async def unseed(self, collection: AsyncCollection, item: AsyncItem) -> None:
        assert isinstance(collection, AsyncMemoryBackend.collection_class)
        assert item.native_id is not None
        del collection.store.objects[item.native_id]


# Values mapped as the tracker maps them, but with the UID the client gave (Ox's three priorities).
MAPPED_KEEPING_UIDS = Profile(
    "mapped-keeping-uids",
    {**FULL.capabilities, Feature.TASK_PRIORITY: TRACKER.capabilities[Feature.TASK_PRIORITY]},
    FULL.components,
)


class TestMappedKeepingUidsProfile(AsyncConformance):
    async def open_backend(self) -> AsyncBackend:
        return AsyncMemoryBackend(profile=MAPPED_KEEPING_UIDS)


# Three more profiles, for the branches the four above leave out.
EVENTS_ONLY = Profile(
    "events-only",
    {
        **FULL.capabilities,
        Feature.COMPONENT_TASK: Support.UNSUPPORTED,
        Feature.COMPONENT_JOURNAL: Support.UNSUPPORTED,
        Feature.CREATE_COLLECTION: Support.UNSUPPORTED,
        Feature.DELETE_COLLECTION: Support.UNSUPPORTED,
        Feature.CHANGES_KNOWN: Support.UNSUPPORTED,
    },
    frozenset({Component.EVENT}),
)

PLAIN_TASKS = Profile(
    "plain-tasks",
    {
        **TRACKER.capabilities,
        Feature.TASK_STATUS: Support.UNSUPPORTED,
        Feature.TASK_PRIORITY: {"level": Support.LOSSY},  # mapped by rules of its own
        Feature.CATEGORIES: {"level": Support.LOSSY},
        Feature.IDENTITY_CLIENT_NATIVE_ID: Support.FULL,
        Feature.CREATE_COLLECTION: Support.UNKNOWN,
    },
    frozenset({Component.TASK}),
)

TASK_FEED = Profile(
    "task-feed",
    {
        **FEED.capabilities,
        Feature.COMPONENT_EVENT: Support.UNSUPPORTED,
        Feature.COMPONENT_JOURNAL: Support.UNSUPPORTED,
        Feature.RECURRENCE: Support.UNSUPPORTED,
    },
    frozenset({Component.TASK}),
)


# Two servers that do not keep the requested components: SOGo makes a calendar for
# everything, Bedework one for events only.  The second is refused when tasks are asked for.
IGNORES_COMPONENTS = Profile(
    "ignores-components",
    {
        **FULL.capabilities,
        Feature.COMPONENT_JOURNAL: Support.UNSUPPORTED,
        Feature.CREATE_COLLECTION: {"level": Support.LOSSY, "note": "the requested components are not kept"},
    },
    frozenset({Component.EVENT, Component.TASK}),
)

EVENTS_WHATEVER = Profile(
    "events-whatever",
    {**IGNORES_COMPONENTS.capabilities, Feature.COMPONENT_TASK: Support.UNSUPPORTED},
    frozenset({Component.EVENT}),
)


class IgnoresComponents(AsyncMemoryBackend):
    async def _create_collection(self, name: str, components: frozenset[Component]) -> AsyncCollection:
        return await super()._create_collection(name, self.profile.components)


class TestIgnoresComponentsProfile(AsyncConformance):
    """A calendar for every component the backend holds, whatever is asked (SOGo)."""

    async def open_backend(self) -> AsyncBackend:
        return IgnoresComponents(profile=IGNORES_COMPONENTS)


class TestEventsWhateverProfile(AsyncConformance):
    """An events-only calendar, whatever is asked (Bedework)."""

    async def open_backend(self) -> AsyncBackend:
        return IgnoresComponents(profile=EVENTS_WHATEVER)


class TestEventsOnlyProfile(AsyncConformance):
    """Writable without tasks; a change feed without known=; move, but no second collection."""

    async def open_backend(self) -> AsyncBackend:
        return AsyncMemoryBackend(profile=EVENTS_ONLY)


class CannotCreate(AsyncMemoryBackend):
    async def _create_collection(self, name: str, components: frozenset[Component]) -> AsyncCollection:
        raise BackendError("this server turned out not to make collections")


class TestPlainTasksProfile(AsyncConformance):
    """No status, priority and categories mapped by its own rules, and an unprobed create-collection."""

    allow_unknown = True

    async def open_backend(self) -> AsyncBackend:
        return CannotCreate(profile=PLAIN_TASKS)


class TestTaskFeedProfile(TestFeedProfile):
    """A read-only collection of tasks, none recurring."""

    async def open_backend(self) -> AsyncBackend:
        return AsyncMemoryBackend(profile=TASK_FEED)


class OwnTimeLog(AsyncMemoryCollection):
    """A tracker's own time log, as Request Tracker keeps it: who, how long and a comment, not when."""

    def _log(self) -> list[tuple[str, TimeEntry]]:
        if not hasattr(self.store, "time_log"):
            self.store.time_log = []  # type: ignore[attr-defined]  # kept with the calendar, as the tracker would
        return self.store.time_log  # type: ignore[attr-defined, no-any-return]

    async def _time_entries(
        self, start: datetime | None, end: datetime | None, *, parent: str | None = None
    ) -> list[tuple[str | None, TimeEntry]]:
        return [
            (uid, e) for uid, e in self._log() if start is None or end is None or (e.logged and start <= e.logged < end)
        ]

    async def _time_log(self, item: Item) -> list[TimeEntry]:
        return [e for uid, e in self._log() if uid == item.uid]

    async def _add_time(self, item: Item, entry: TimeEntry) -> TimeEntry:
        kept = replace(entry, start=None, end=None, id=f"log-{len(self._log()) + 1}")
        self._log().append((item.uid, kept))
        return kept

    async def _amend_time(self, item: Item, old: TimeEntry, new: TimeEntry) -> TimeEntry:
        kept = replace(new, start=None, end=None)
        self._log()[:] = [(uid, kept if e.id == old.id else e) for uid, e in self._log()]
        return kept

    async def _delete_time(self, item: Item, entry: TimeEntry) -> None:
        self._log()[:] = [(uid, e) for uid, e in self._log() if e.id != entry.id]


class TotalOnly(AsyncMemoryCollection):
    """A tracker that keeps only the total time spent on a task."""

    def _totals(self) -> dict[str, timedelta]:
        if not hasattr(self.store, "totals"):
            self.store.totals = {}  # type: ignore[attr-defined]  # kept with the calendar, as the tracker would
        return self.store.totals  # type: ignore[attr-defined, no-any-return]

    async def _time_entries(
        self, start: datetime | None, end: datetime | None, *, parent: str | None = None
    ) -> list[tuple[str | None, TimeEntry]]:
        return [(uid, TimeEntry(total)) for uid, total in self._totals().items()]

    async def _time_log(self, item: Item) -> list[TimeEntry]:
        total = self._totals().get(item.uid)
        return [] if total is None else [TimeEntry(total)]

    async def _add_time(self, item: Item, entry: TimeEntry) -> TimeEntry:
        self._totals()[item.uid] = self._totals().get(item.uid, timedelta(0)) + entry.person_time
        return TimeEntry(entry.duration)


def time_log_profile(name: str, time_log: dict[str, Any]) -> Profile:
    return Profile(name, {**TRACKER.capabilities, Feature.TIME_LOG: time_log}, TRACKER.components)


OWN_TIME_LOG = time_log_profile("own-time-log", {"level": Support.LOSSY, "lost": ["start-time", "end"]})
TOTAL_ONLY = time_log_profile("total-only", {"level": Support.LOSSY, "total-only": True})


class TestOwnTimeLogProfile(AsyncConformance):
    """A tracker whose time log drops when the work was done."""

    async def open_backend(self) -> AsyncBackend:
        class Backend(AsyncMemoryBackend):
            collection_class = OwnTimeLog

        return Backend(profile=OWN_TIME_LOG)


class TestTotalOnlyProfile(AsyncConformance):
    """A tracker that keeps only a total."""

    async def open_backend(self) -> AsyncBackend:
        class Backend(AsyncMemoryBackend):
            collection_class = TotalOnly

        return Backend(profile=TOTAL_ONLY)


def sharing(profile: Profile, store: MemoryStore | None = None) -> AsyncMemoryBackend:
    """A backend on ``store``, like a second connection to a server that keeps its data."""
    backend = AsyncMemoryBackend(profile=profile)
    if store is not None:
        backend.store = backend.native = store
    return backend


class LosesAnswers(AsyncMemoryCollection):
    """Stores what it is given, then fails the write as if the answer was lost."""

    async def _create(self, calendar: icalendar.Calendar, *, native_id: str | None, wire: bytes | None) -> StoredObject:
        await super()._create(calendar, native_id=native_id, wire=wire)
        raise TransportError("read timed out")


class SweptMeanwhile(AsyncMemoryCollection):
    """Another run deletes each item just before this one does."""

    async def delete(self, item: Item | str, *, overwrite: bool = False) -> None:
        await super().delete(item, overwrite=overwrite)
        await super().delete(item, overwrite=overwrite)


class TestTidyingUp:
    """What the suite leaves behind on a backend that keeps data between tests."""

    @pytest.fixture(autouse=True)
    def _nothing_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every memory backend's id is "memory": what an earlier test kept must not leak in."""
        monkeypatch.setattr(AsyncConformance, "_kept", {})

    def suite(self) -> AsyncConformance:
        suite = AsyncConformance()
        suite._made, suite._borrowed = [], []
        return suite

    async def test_a_concurrent_write_to_a_borrowed_collection_survives(self) -> None:
        backend = sharing(MINIMAL)
        suite = self.suite()
        collection = await suite.open_collection(backend)
        await collection.add_task(summary="the suite's", uid=fresh_uid())
        [shared] = await sharing(MINIMAL, backend.store).collections()
        await shared.add_task(summary="the user's, from their phone")
        await suite.tidy_up(backend)
        assert [str(i.component["SUMMARY"]) for i in await shared.search()] == ["the user's, from their phone"]

    async def test_a_broken_object_in_a_borrowed_collection_is_left_alone(self) -> None:
        backend = sharing(MINIMAL)
        bad = backend.store.calendars["default"].put(
            b"BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nDUE:2026-10-20\r\nEND:VTODO\r\n"
        )
        suite = self.suite()
        collection = await suite.open_collection(backend)
        assert await snapshot(collection) == []
        await collection.add_task(summary="the suite's", uid=fresh_uid())
        await suite.tidy_up(backend)
        assert list(backend.store.calendars["default"].objects) == [bad]

    async def test_a_write_whose_answer_was_lost_is_tidied_up(self) -> None:
        class Backend(AsyncMemoryBackend):
            collection_class = LosesAnswers

        backend = Backend(profile=MINIMAL)
        suite = self.suite()
        collection = await suite.open_collection(backend)
        with pytest.raises(TransportError):
            await collection.add_task(summary="stored all the same", uid=fresh_uid())
        assert await collection.search()
        await suite.tidy_up(backend)
        assert list(backend.store.calendars["default"].objects) == []

    async def test_a_crashed_runs_leftovers_are_swept(self) -> None:
        backend = sharing(MINIMAL)
        crashed = Task.new(
            summary="a crashed run's", uid=f"x-t{int(time_module.time()) - 2 * 86400}@conformance.invalid"
        )
        completion = Task.new(summary="the server's, for it")
        completion.component.add("RELATED-TO", crashed.uid)
        live = Task.new(summary="a live run's", uid=fresh_uid())
        for item in (crashed, completion, live, Task.new(summary="the user's")):
            backend.store.calendars["default"].put(item.to_ical())
        suite = self.suite()
        collection = await suite.open_collection(backend)
        assert sorted(str(i.component["SUMMARY"]) for i in await collection.search()) == ["a live run's", "the user's"]
        await suite.tidy_up(backend)
        assert len(backend.store.calendars["default"].objects) == 2

    async def test_an_older_runs_unstamped_leftover_is_swept(self) -> None:
        backend = sharing(MINIMAL)
        for uid in ("corpus-task-0123456789ab@conformance.invalid", f"x-t{'9' * 30}@conformance.invalid"):
            backend.store.calendars["default"].put(Task.new(summary="before the stamp", uid=uid).to_ical())
        collection = await self.suite().open_collection(backend)
        assert await collection.search() == []

    async def test_a_leftover_another_run_swept_first_is_no_error(self) -> None:
        class Backend(AsyncMemoryBackend):
            collection_class = SweptMeanwhile

        backend = Backend(profile=MINIMAL)
        crashed = Task.new(
            summary="a crashed run's", uid=f"x-t{int(time_module.time()) - 2 * 86400}@conformance.invalid"
        )
        backend.store.calendars["default"].put(crashed.to_ical())
        collection = await self.suite().open_collection(backend)
        assert await collection.search() == []

    async def test_a_write_without_the_runs_mark_is_refused(self) -> None:
        backend = sharing(MINIMAL)
        collection = await self.suite().open_collection(backend)
        with pytest.raises(pytest.fail.Exception, match="conformance.invalid"):
            await collection.add_task(summary="no run's mark")
        assert list(backend.store.calendars["default"].objects) == []

    async def test_a_borrowed_collection_keeps_its_name(self) -> None:
        backend = sharing(MINIMAL)
        suite = self.suite()
        await suite.test_set_name(backend, await suite.open_collection(backend))
        await suite.tidy_up(backend)
        assert (await backend.collection("default")).name == "Default"

    async def test_without_delete_collection_one_collection_is_reused(self) -> None:
        store = sharing(TRACKER).store
        for _ in range(3):
            backend = sharing(TRACKER, store)
            suite = self.suite()
            collection = await suite.open_collection(backend)
            await collection.add_task(summary="the suite's", uid=fresh_uid())
            await suite.test_create_and_delete_collection(backend)
            await suite.tidy_up(backend)
        assert sorted(store.calendars) == ["conformance", "conformance-new", "default"]
        assert [len(c.objects) for c in store.calendars.values()] == [0, 0, 0]

    def counting(self, backend: AsyncMemoryBackend, listings: list[None]) -> AsyncMemoryBackend:
        """``backend``, noting in ``listings`` each time its collections are listed."""
        listed = backend.collections

        async def collections() -> list[AsyncCollection]:
            listings.append(None)
            return await listed()

        backend.collections = collections  # type: ignore[method-assign]
        return backend

    async def test_a_kept_collection_is_found_again_without_listing_them_all(self) -> None:
        # 469 calendars on a Synology took minutes to list, in every test.
        store = sharing(TRACKER).store
        listings: list[None] = []
        ids = set()
        for _ in range(3):
            backend = self.counting(sharing(TRACKER, store), listings)
            suite = self.suite()
            ids.add((await suite.open_collection(backend, name="kept-once")).id)
            await suite.tidy_up(backend)
        assert len(ids) == 1
        assert len(listings) == 1

    async def test_a_kept_events_collection_is_found_again_without_a_listing(self) -> None:
        class Reusing(AsyncConformance):
            def reuses_collections(self, backend: AsyncBackend) -> bool:
                return True

        suite = Reusing()
        suite._made, suite._borrowed = [], []
        backend = sharing(FULL)
        await suite.test_create_collection_holds_one_of_what_was_asked(backend)
        assert (backend.id, "conformance-events") in AsyncConformance._kept

    async def test_one_listing_finds_every_collection_a_run_left(self) -> None:
        store = sharing(TRACKER).store
        for name in ("left-a", "left-b"):
            store.add_calendar(name, name, [Component.TASK])
        listings: list[None] = []
        for name in ("left-a", "left-b"):
            suite = self.suite()
            assert (await suite.open_collection(self.counting(sharing(TRACKER, store), listings), name)).id == name
        assert len(listings) == 1

    async def test_a_kept_collection_deleted_meanwhile_is_made_again(self) -> None:
        store = sharing(TRACKER).store
        listings: list[None] = []
        ids, counts = [], []
        for gone in (True, False, False):
            suite = self.suite()
            ids.append((await suite.open_collection(self.counting(sharing(TRACKER, store), listings), "gone")).id)
            counts.append(len(listings))
            if gone:
                del store.calendars[ids[-1]]
        assert ids[1] in store.calendars
        assert ids[1:] == [ids[1], ids[1]]
        assert counts[2] == counts[1]  # the one made again is kept

    async def test_a_kept_collection_renamed_meanwhile_is_not_used(self) -> None:
        store = sharing(TRACKER).store
        suite = self.suite()
        first = await suite.open_collection(sharing(TRACKER, store), "renamed")
        store.calendars[first.id].name = "the user's now"
        suite = self.suite()
        again = await suite.open_collection(sharing(TRACKER, store), "renamed")
        assert again.id != first.id
        assert again.name == "renamed"

    async def test_a_kept_id_now_shared_by_several_names_falls_back_to_listing(self) -> None:
        # The default reopen() looks up by id, and else by name: several of that name are ambiguous.
        store = sharing(TRACKER).store
        suite = self.suite()
        first = await suite.open_collection(sharing(TRACKER, store), "twice")
        del store.calendars[first.id]
        for id in ("twice-1", "twice-2"):
            store.add_calendar(id, "twice", [Component.TASK])
        suite = self.suite()
        assert (await suite.open_collection(sharing(TRACKER, store), "twice")).id in ("twice-1", "twice-2")


# Backends that each break one rule, and the suite test that must catch it.


def _listed(o: MemoryObject, calendar: icalendar.Calendar | None = None) -> StoredObject:
    return StoredObject(o.native_id, o.etag, calendar if calendar is not None else o.calendar, o.data, o)


class FirstPageOnly(AsyncMemoryCollection):
    """Lists through a paged API and never asks for the second page."""

    def _page(self) -> list[MemoryObject]:
        return list(self.store.objects.values())[:50]

    async def _query(self, searcher: Searcher | None) -> list[StoredObject]:
        return [_listed(o) for o in self._page()]

    async def _list_etags(self) -> dict[str, str]:
        return {o.native_id: o.etag for o in self._page()}


class CutsWithAnError(FirstPageOnly):
    """Stalwart's way: a listing longer than the server's limit fails, with nothing of it returned."""

    def _page(self) -> list[MemoryObject]:
        if len(self.store.objects) > 50:
            raise ListingLimitError("the server cut the listing at 50")
        return super()._page()


class DeclaresItsLimit(CutsWithAnError):
    def __init__(self, backend: AsyncMemoryBackend, store: MemoryCalendar) -> None:
        super().__init__(backend, store)
        self.capabilities = self.capabilities.updated({Feature.LISTING_COMPLETE: Support.UNSUPPORTED})


class DeclaresAndCutsSilently(FirstPageOnly):
    """Declares the limit, but hands out the first page as if it were all."""

    def __init__(self, backend: AsyncMemoryBackend, store: MemoryCalendar) -> None:
        super().__init__(backend, store)
        self.capabilities = self.capabilities.updated({Feature.LISTING_COMPLETE: Support.UNSUPPORTED})


class MatchesUidSubstrings(AsyncMemoryCollection):
    """Looks a UID up with a server text-match, newest first."""

    async def _find_uid(self, uid: str) -> StoredObject:
        for o in reversed(list(self.store.objects.values())):
            if uid in o.uid:
                return _listed(o)
        raise NotFoundError(f"no item with UID {uid!r}")


class SearchesTheAccount(AsyncMemoryCollection):
    """Looks a UID up in every collection of the account."""

    async def _find_uid(self, uid: str) -> StoredObject:
        for calendar in self._backend.store.calendars.values():
            for o in calendar.objects.values():
                if o.uid == uid:
                    return _listed(o)
        raise NotFoundError(f"no item with UID {uid!r}")


class ReplacesEventUids(AsyncMemoryCollection):
    """Gives every event a UID of its own, while declaring identity.client-uid."""

    async def _create(self, calendar: icalendar.Calendar, *, native_id: str | None, wire: bytes | None) -> StoredObject:
        for component in calendar.subcomponents:
            if component.name == "VEVENT":
                component["UID"] = f"server-{self._backend.store.next()}"
        return await super()._create(calendar, native_id=native_id, wire=None)


class StaleListing(AsyncMemoryCollection):
    """Lists the content it first saw under each object's current etag."""

    seen: ClassVar[dict[str, icalendar.Calendar]] = {}

    async def _query(self, searcher: Searcher | None) -> list[StoredObject]:
        cached = [self.seen.setdefault(o.native_id, o.calendar) for o in self.store.objects.values()]
        return [_listed(o, copy.deepcopy(c)) for o, c in zip(self.store.objects.values(), cached, strict=True)]


class ListViewStream(AsyncMemoryCollection):
    """Streams the fields of a list view: no DESCRIPTION, no CATEGORIES."""

    async def _iter_query(self, searcher: Searcher | None) -> AsyncIterator[StoredObject]:
        for o in self.store.objects.values():
            calendar = o.calendar
            for component in calendar.subcomponents:
                for name in ("DESCRIPTION", "CATEGORIES"):
                    component.pop(name, None)
            yield _listed(o, calendar)


class WordIndex(AsyncMemoryCollection):
    """Narrows a text search with a word index, so "rain" misses "Training"."""

    async def _query(self, searcher: Searcher | None) -> list[StoredObject]:
        wanted = {str(v) for v in getattr(searcher, "_property_filters", {}).values()}
        out = []
        for o in self.store.objects.values():
            words = {w for c in o.calendar.subcomponents for w in str(c.get("SUMMARY", "")).split()}
            if not wanted or wanted & words:
                out.append(_listed(o))
        return out


class NoDeletionsFeed(AsyncMemoryCollection):
    """A feed whose change listing never drops an id it has seen."""

    seen: ClassVar[dict[str, str]] = {}

    async def _list_etags(self) -> dict[str, str]:
        self.seen.update(await super()._list_etags())
        return dict(self.seen)


class FrozenFeed(AsyncMemoryCollection):
    """Serves its first download forever."""

    first: list[StoredObject] | None = None

    async def _query(self, searcher: Searcher | None) -> list[StoredObject]:
        if FrozenFeed.first is None and self.store.objects:  # the first download that has something in it
            FrozenFeed.first = [_listed(o) for o in self.store.objects.values()]
        return FrozenFeed.first or []

    async def _list_etags(self) -> dict[str, str]:
        return {s.native_id: s.etag for s in await self._query(None)}


class LeakyChangeFeed(AsyncMemoryCollection):
    """Reports changes in every collection of the account."""

    async def _changes(
        self, token: SyncToken | None, known: Mapping[str, str] | None
    ) -> tuple[Sequence[StoredObject | BrokenObject], list[str], SyncToken]:
        changed, deleted, new_token = await super()._changes(token, known)
        others = [
            _listed(o)
            for c in self._backend.store.calendars.values()
            if c is not self.store
            for o in c.objects.values()
        ]
        return [*changed, *others], deleted, new_token


class Mangling(AsyncMemoryCollection):
    """Stores a mangled copy of whatever it is given, declaring that it loses nothing."""

    def mangle(self, component: icalendar.Component) -> None:
        raise NotImplementedError

    def _mangled(self, calendar: icalendar.Calendar, wire: bytes | None) -> icalendar.Calendar:
        out = icalendar.Calendar.from_ical(wire) if wire is not None else copy.deepcopy(calendar)
        for component in out.subcomponents:
            if component.name in ("VEVENT", "VTODO", "VJOURNAL"):
                self.mangle(component)
        return out

    async def _create(self, calendar: icalendar.Calendar, *, native_id: str | None, wire: bytes | None) -> StoredObject:
        return await super()._create(self._mangled(calendar, wire), native_id=native_id, wire=None)


class DropsAlarms(Mangling):
    def mangle(self, component: icalendar.Component) -> None:
        component.subcomponents[:] = [c for c in component.subcomponents if c.name != "VALARM"]


class AsciiOnly(Mangling):
    def mangle(self, component: icalendar.Component) -> None:
        for name in ("SUMMARY", "DESCRIPTION", "LOCATION"):
            if name in component:
                text = "".join(ch if ord(ch) < 128 else "?" for ch in str(component.pop(name)))
                component.add(name, text)


class FlattensNewlines(Mangling):
    def mangle(self, component: icalendar.Component) -> None:
        if "DESCRIPTION" in component:
            component["DESCRIPTION"] = icalendar.vText(str(component["DESCRIPTION"]).replace("\n", " "))


class StoresUtc(Mangling):
    def mangle(self, component: icalendar.Component) -> None:
        for name in ("DTSTART", "DTEND"):
            value = component.decoded(name, None)
            if isinstance(value, datetime):
                component.pop(name)
                component.add(name, value.astimezone(UTC))


class DatesAsDateTimes(Mangling):
    def mangle(self, component: icalendar.Component) -> None:
        for name in ("DTSTART", "DTEND", "DUE"):
            value = component.decoded(name, None)
            if isinstance(value, date) and not isinstance(value, datetime):
                component.pop(name)
                component.add(name, datetime.combine(value, time(), UTC))


class DropsEventDetails(Mangling):
    def mangle(self, component: icalendar.Component) -> None:
        for name in ("LOCATION", "URL", "EXDATE", "X-CONFORMANCE-COLOUR"):
            component.pop(name, None)


class DropsOverrides(AsyncMemoryCollection):
    async def _create(self, calendar: icalendar.Calendar, *, native_id: str | None, wire: bytes | None) -> StoredObject:
        calendar = copy.deepcopy(calendar)
        calendar.subcomponents[:] = [c for c in calendar.subcomponents if "RECURRENCE-ID" not in c]
        return await super()._create(calendar, native_id=native_id, wire=None)


class DropsNestedXProperties(AsyncMemoryCollection):
    """Keeps X- properties on the items, and drops them from their alarms."""

    async def _create(self, calendar: icalendar.Calendar, *, native_id: str | None, wire: bytes | None) -> StoredObject:
        calendar = copy.deepcopy(calendar)
        for component in calendar.walk():
            if component.name == "VALARM":
                for name in [n for n in component if n.startswith("X-")]:
                    del component[name]
        return await super()._create(calendar, native_id=native_id, wire=None)


class FirstValueOnly(Mangling):
    def mangle(self, component: icalendar.Component) -> None:
        for name in ("CATEGORIES", "ATTENDEE"):
            values = component.get(name)
            if isinstance(values, list):
                component[name] = values[0]


class StartsInRange(AsyncMemoryCollection):
    """Narrows a time range by DTSTART alone, and leaves tasks out of it."""

    async def _query(self, searcher: Searcher | None) -> list[StoredObject]:
        start, end = getattr(searcher, "start", None), getattr(searcher, "end", None)
        out = []
        for o in self.store.objects.values():
            components = [c for c in o.calendar.subcomponents if c.name in ("VEVENT", "VTODO")]
            if start is None or any(
                c.name == "VEVENT" and start <= c.decoded("DTSTART") < end for c in components if "DTSTART" in c
            ):
                out.append(_listed(o))
        return out


class CompletesNothing(AsyncMemoryCollection):
    """Declares task.complete-recurring and writes nothing."""

    def __init__(self, backend: AsyncMemoryBackend, store: MemoryCalendar) -> None:
        super().__init__(backend, store)
        self.capabilities = self.capabilities.updated({Feature.TASK_COMPLETE_RECURRING: Support.FULL})

    async def _complete_recurring(
        self, task: Task, at: datetime | None, mode: str, *, loss: LossPolicy | None, verify: bool | None
    ) -> AsyncTask:
        return self.bind(task)


class InventsStartTimes(OwnTimeLog):
    """Declares the start time lost, then makes one up on reading."""

    async def _time_log(self, item: Item) -> list[TimeEntry]:
        return [replace(e, start=e.logged) for e in await super()._time_log(item)]


class IgnoresTheRange(AsyncMemoryCollection):
    """Answers a time-range search for entries with every entry."""

    async def _time_entries(
        self, start: datetime | None, end: datetime | None, *, parent: str | None = None
    ) -> list[tuple[str | None, TimeEntry]]:
        return await super()._time_entries(None, None, parent=parent)


# What the suite raises when it catches a defect: its own assertion, or a
# pytest.raises that did not raise.  The round trip catches through verify=True.
ASSERTS = (AssertionError, pytest.fail.Exception)

CAUGHT: list[tuple[type[AsyncMemoryCollection], str, dict[str, Any], Any]] = [
    (FirstPageOnly, "test_listings_are_not_cut_at_a_page", {}, ASSERTS),
    (CutsWithAnError, "test_listings_are_not_cut_at_a_page", {}, ListingLimitError),
    (DeclaresAndCutsSilently, "test_listings_are_not_cut_at_a_page", {}, ASSERTS),
    (MatchesUidSubstrings, "test_get_finds_the_exact_uid", {}, ASSERTS),
    (SearchesTheAccount, "test_get_finds_the_exact_uid", {}, ASSERTS),
    (ReplacesEventUids, "test_get_finds_the_exact_uid", {}, ASSERTS),
    (StaleListing, "test_a_listing_shows_a_save_made_elsewhere", {}, ASSERTS),
    (ListViewStream, "test_iter_search_yields_what_search_returns", {}, ASSERTS),
    (WordIndex, "test_search_gives_the_client_side_answer", {"name": "summary-contains"}, ASSERTS),
    (FrozenFeed, "test_read_paths_on_seeded_items", {}, ASSERTS),
    (NoDeletionsFeed, "test_read_paths_on_seeded_items", {}, ASSERTS),
    (LeakyChangeFeed, "test_changes_follow_a_move", {}, ASSERTS),
    (DropsAlarms, "test_real_world_objects_round_trip", {"name": "event-series-in-a-time-zone"}, VerificationError),
    (DropsAlarms, "test_real_world_objects_round_trip", {"name": "task-due-on-a-date"}, VerificationError),
    (AsciiOnly, "test_real_world_objects_round_trip", {"name": "journal-entry"}, VerificationError),
    (FlattensNewlines, "test_real_world_objects_round_trip", {"name": "task-due-on-a-date"}, VerificationError),
    (StoresUtc, "test_real_world_objects_round_trip", {"name": "event-series-in-a-time-zone"}, VerificationError),
    (StoresUtc, "test_real_world_objects_round_trip", {"name": "floating-event"}, VerificationError),
    (DatesAsDateTimes, "test_real_world_objects_round_trip", {"name": "all-day-event-with-rdate"}, VerificationError),
    (DatesAsDateTimes, "test_real_world_objects_round_trip", {"name": "task-due-on-a-date"}, VerificationError),
    (
        DropsEventDetails,
        "test_real_world_objects_round_trip",
        {"name": "event-series-in-a-time-zone"},
        VerificationError,
    ),
    (DropsOverrides, "test_real_world_objects_round_trip", {"name": "event-series-in-a-time-zone"}, VerificationError),
    (FirstValueOnly, "test_real_world_objects_round_trip", {"name": "task-due-on-a-date"}, VerificationError),
    (
        DropsNestedXProperties,
        "test_real_world_objects_round_trip",
        {"name": "event-series-in-a-time-zone"},
        VerificationError,
    ),
    (DropsNestedXProperties, "test_real_world_objects_round_trip", {"name": "task-due-on-a-date"}, VerificationError),
    (StartsInRange, "test_search_gives_the_client_side_answer", {"name": "events-in-range"}, ASSERTS),
    (StartsInRange, "test_search_gives_the_client_side_answer", {"name": "tasks-in-range"}, ASSERTS),
    (CompletesNothing, "test_complete_a_recurring_task", {}, ASSERTS),
    (InventsStartTimes, "test_time_log_round_trips_as_declared", {}, ASSERTS),
    (IgnoresTheRange, "test_time_entries_in_a_range", {}, ASSERTS),
]


@pytest.mark.parametrize(
    ("broken", "test", "params", "expected"),
    CAUGHT,
    ids=[f"{c[0].__name__}-{c[2].get('name', '')}".rstrip("-") for c in CAUGHT],
)
async def test_the_suite_catches(
    broken: type[AsyncMemoryCollection], test: str, params: dict[str, Any], expected: Any
) -> None:
    class Backend(AsyncMemoryBackend):
        collection_class = broken

    StaleListing.seen.clear()
    NoDeletionsFeed.seen.clear()
    FrozenFeed.first = None
    profile, suite = (FEED, TestFeedProfile()) if broken in (FrozenFeed, NoDeletionsFeed) else (FULL, TestFullProfile())
    if issubclass(broken, OwnTimeLog):
        profile = OWN_TIME_LOG
    suite._made, suite._borrowed = [], []
    backend = Backend(profile=profile)
    collection = await suite.open_collection(backend)
    method = getattr(suite, test)
    wanted = inspect.signature(method).parameters
    fixtures = {"backend": backend, "collection": collection}
    with pytest.raises(expected):
        await method(**{k: v for k, v in fixtures.items() if k in wanted}, **params)


async def test_the_suite_catches_a_collection_wider_than_its_backend() -> None:
    backend = AsyncMemoryBackend(profile=FULL)
    backend.capabilities = backend.capabilities.updated({Feature.WRITE: Support.UNSUPPORTED})
    with pytest.raises(AssertionError):
        await TestFullProfile().test_collection_never_widens_backend(backend, await backend.collection("default"))


async def test_the_suite_accepts_a_declared_listing_limit() -> None:
    class Backend(AsyncMemoryBackend):
        collection_class = DeclaresItsLimit

    suite = TestFullProfile()
    suite._made, suite._borrowed = [], []
    backend = Backend(profile=FULL)
    await suite.test_listings_are_not_cut_at_a_page(await suite.open_collection(backend))
