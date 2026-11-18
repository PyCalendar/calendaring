# Hand-written source; tools/generate_sync.py derives ../_sync/ from this file.
"""The caldav backend passes the conformance suite on every CalDAV server ``caldav_server`` gives (see conftest)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from conftest import wait_for_server

from calendaring import (
    AlreadyExistsError,
    AsyncBackend,
    AsyncCollection,
    AsyncItem,
    BackendError,
    Event,
    Item,
    NotFoundError,
    TransportError,
)
from calendaring.loss import ALLOW
from calendaring.testing import AsyncConformance

# A failed write is tried this many times in all while seeding
SEED_ATTEMPTS = 3


class CalDAVConformance(AsyncConformance):
    # A CalDAV server is probed, not known in advance (§5.1).
    allow_unknown = True
    server: Any
    monkeypatch: pytest.MonkeyPatch

    async def open_backend(self) -> AsyncBackend:
        backend = await AsyncBackend.open(self.server.url, kind="caldav", **self.server.options)
        wait_for_server(backend, self.monkeypatch)
        return backend

    async def seed(self, collection: AsyncCollection, items: Sequence[Item]) -> list[AsyncItem]:
        """Store each item, again after a failed write, unless it got there after all.

        Seeding is setup, not what a test is about.  Robur leaves about one
        request in 60 unanswered (a 70-second hang, then a reset) and answers
        some PUTs with a 500, so a test that seeds 120 items fails most runs on
        a write that has nothing to do with it.  Every ``BackendError`` is
        retried, since it carries no status to tell a 500 from a refusal; a
        refusal costs two more tries and is raised all the same.  A clash is
        only taken for a lost answer on a retry: the UIDs are fresh.
        """
        seeded = []
        for item in items:
            for attempt in range(SEED_ATTEMPTS):
                try:
                    seeded.append(await collection.add(item, loss=ALLOW))
                    break
                except AlreadyExistsError:
                    if attempt == 0 or not await self._seeded(collection, item, seeded):
                        raise
                    break
                except BackendError:
                    if await self._seeded(collection, item, seeded):
                        break
                    if attempt == SEED_ATTEMPTS - 1:
                        raise
        return seeded

    @staticmethod
    async def _seeded(collection: AsyncCollection, item: Item, seeded: list[AsyncItem]) -> bool:
        """Whether a write whose answer was lost stored ``item``; if so it joins ``seeded``.  A failed read says no."""
        try:
            seeded.append(await collection.get(item.uid))
        except (NotFoundError, BackendError):
            return False
        return True


class TestCalDAVServers(CalDAVConformance):
    @pytest.fixture(autouse=True)
    def _server(self, caldav_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.server = caldav_server
        self.monkeypatch = monkeypatch


class _DroppingCollection:
    """Fails ``add`` as told: a lost answer after storing the item or before, or a 500."""

    def __init__(self, outcomes: list[str], gets: list[str] | None = None) -> None:
        self.outcomes = outcomes  # per add: "ok", "stored-then-lost", "lost" or "refused" (a 500, nothing stored)
        self.gets = gets or []  # per get: "ok", "lost" (no answer) or "missing" (not found yet, though stored)
        self.stored: dict[str, Any] = {}
        self.adds = 0

    async def add(self, item: Any, **_: Any) -> Any:
        outcome = self.outcomes[self.adds] if self.adds < len(self.outcomes) else "ok"
        self.adds += 1
        if outcome not in ("lost", "refused"):
            if item.uid in self.stored:
                raise AlreadyExistsError(item.uid, taken="uid")
            self.stored[item.uid] = item
        if outcome == "refused":
            raise BackendError("PutError at '500 Internal Server Error'")
        if outcome != "ok":
            raise TransportError("read timed out")
        return item

    async def get(self, uid: str) -> Any:
        outcome = self.gets.pop(0) if self.gets else "ok"
        if outcome == "lost":
            raise TransportError("read timed out")
        try:
            if outcome == "missing":
                raise KeyError(uid)
            return self.stored[uid]
        except KeyError:
            raise NotFoundError(uid) from None


@pytest.mark.parametrize(
    ("outcomes", "gets", "adds"),
    [
        (["stored-then-lost"], [], 1),
        (["lost"], [], 2),
        (["lost", "stored-then-lost"], [], 2),
        (["refused"], [], 2),
        (["lost"], ["lost"], 2),
        (["stored-then-lost"], ["lost"], 2),
    ],
)
async def test_seed_gets_past_a_lost_answer(outcomes: list[str], gets: list[str], adds: int) -> None:
    """Robur leaves about one request in 60 unanswered, and answers some PUTs with a 500; seeding 120 items fails most runs without this."""
    collection = _DroppingCollection(outcomes, gets)
    item = Event.new(summary="paged 0")
    seeded = await CalDAVConformance().seed(collection, [item])  # type: ignore[arg-type]
    assert [i.uid for i in seeded] == [item.uid]
    assert collection.adds == adds


async def test_seed_gives_up_on_a_server_that_never_answers() -> None:
    collection = _DroppingCollection(["lost"] * 10)
    with pytest.raises(TransportError):
        await CalDAVConformance().seed(collection, [Event.new(summary="paged 0")])  # type: ignore[arg-type]
    assert collection.adds == SEED_ATTEMPTS


async def test_seed_does_not_take_a_clash_it_did_not_cause() -> None:
    """A fresh UID that is taken before any retry is a leftover or a backend bug, not a lost answer."""
    item = Event.new(summary="paged 0")
    collection = _DroppingCollection([])
    collection.stored[item.uid] = item
    with pytest.raises(AlreadyExistsError):
        await CalDAVConformance().seed(collection, [item])  # type: ignore[arg-type]
    assert collection.adds == 1


async def test_seed_gives_up_on_a_clash_it_cannot_read() -> None:
    """The lost write landed, but the item cannot be read back: the clash on the retry is raised."""
    collection = _DroppingCollection(["stored-then-lost"], ["missing", "missing"])
    with pytest.raises(AlreadyExistsError):
        await CalDAVConformance().seed(collection, [Event.new(summary="paged 0")])  # type: ignore[arg-type]
    assert collection.adds == 2
