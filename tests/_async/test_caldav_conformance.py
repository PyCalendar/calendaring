# Hand-written source; tools/generate_sync.py derives ../_sync/ from this file.
"""The caldav backend passes the conformance suite on every CalDAV server ``caldav_server`` gives (see conftest)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import wait_for_server

from calendaring import AsyncBackend, AsyncCollection, Component, Feature, NotFoundError
from calendaring._async.caldav import AsyncCalDAVBackend, AsyncCalDAVCollection
from calendaring._caldav_io import Calendar
from calendaring.testing import AsyncConformance


class CalDAVConformance(AsyncConformance):
    # A CalDAV server is probed, not known in advance (§5.1).
    allow_unknown = True
    server: Any
    monkeypatch: pytest.MonkeyPatch

    async def open_backend(self) -> AsyncBackend:
        backend = await AsyncBackend.open(self.server.url, kind="caldav", **self.server.options)
        wait_for_server(backend, self.monkeypatch)
        return backend

    async def reopen(self, backend: AsyncBackend, kept: AsyncCollection) -> AsyncCollection:
        # From its URL: a lookup by id lists every calendar, minutes on an account with hundreds.
        assert isinstance(backend, AsyncCalDAVBackend)
        assert isinstance(kept, AsyncCalDAVCollection)
        client: Any = backend._dav.client  # caldav types Calendar's client as the sync one
        return await backend._wrap(Calendar(client=client, url=kept.url))


class TestCalDAVServers(CalDAVConformance):
    @pytest.fixture(autouse=True)
    def _server(self, caldav_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.server = caldav_server
        self.monkeypatch = monkeypatch


async def test_a_kept_calendar_is_reopened_from_its_url(caldav_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = TestCalDAVServers()
    suite.server, suite.monkeypatch = caldav_server, monkeypatch
    async with await suite.open_backend() as backend:
        if not all(backend.capabilities.supports(f) for f in (Feature.CREATE_COLLECTION, Feature.DELETE_COLLECTION)):
            pytest.skip("the server cannot make and delete a calendar")
        made = await backend.create_collection("conformance-reopened", components=(Component.TASK,))
        try:
            async with await suite.open_backend() as other:
                again = await suite.reopen(other, made)
                assert (again.id, again.name) == (made.id, made.name)
        finally:
            await made.delete_collection()
        async with await suite.open_backend() as other:
            with pytest.raises(NotFoundError):
                await suite.reopen(other, made)
