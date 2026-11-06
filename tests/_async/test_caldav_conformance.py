# Hand-written source; tools/generate_sync.py derives ../_sync/ from this file.
"""The caldav backend passes the conformance suite on every CalDAV server ``caldav_server`` gives (see conftest)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import wait_out_search_cache

from calendaring import AsyncBackend
from calendaring.testing import AsyncConformance


class CalDAVConformance(AsyncConformance):
    # A CalDAV server is probed, not known in advance (§5.1).
    allow_unknown = True
    server: Any
    monkeypatch: pytest.MonkeyPatch

    async def open_backend(self) -> AsyncBackend:
        backend = await AsyncBackend.open(self.server.url, kind="caldav", **self.server.options)
        wait_out_search_cache(backend, self.monkeypatch)
        return backend


class TestCalDAVServers(CalDAVConformance):
    @pytest.fixture(autouse=True)
    def _server(self, caldav_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.server = caldav_server
        self.monkeypatch = monkeypatch
