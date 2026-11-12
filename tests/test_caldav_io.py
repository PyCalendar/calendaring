"""The caldav shim's generated sync mode still matches the async one, method for method (roadmap 2.1)."""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("caldav")

from calendaring._async.dav import AsyncDAV  # noqa: E402
from calendaring._sync.dav import DAV  # noqa: E402


def methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(cls).items()
        if inspect.isfunction(member) and (not name.startswith("_") or name == "__init__")
    }


def test_both_modes_have_the_same_methods_and_signatures() -> None:
    assert methods(AsyncDAV) == methods(DAV)


@pytest.mark.parametrize("name", sorted(set(methods(AsyncDAV)) - {"__init__"}))
def test_only_the_async_mode_is_async(name: str) -> None:
    assert inspect.iscoroutinefunction(getattr(AsyncDAV, name))
    assert not inspect.iscoroutinefunction(getattr(DAV, name))


def test_caldav_errors_translate() -> None:
    from caldav.lib import error as dav_error

    from calendaring._caldav_io import _translate
    from calendaring.errors import BackendError, ConflictError, NotFoundError, RateLimitError, SyncTokenError

    assert isinstance(_translate(dav_error.NotFoundError(url="x")), NotFoundError)
    assert isinstance(_translate(dav_error.ETagMismatchError(url="x")), ConflictError)
    gone = dav_error.AuthorizationError(url="x", preconditions=["{DAV:}valid-sync-token"])
    assert isinstance(_translate(gone), SyncTokenError)
    limited = _translate(dav_error.RateLimitError(url="x", retry_after="120", retry_after_seconds=120.0))
    assert isinstance(limited, RateLimitError)
    assert limited.retry_after == 120.0
    assert type(_translate(dav_error.DAVError(url="x"))) is BackendError


def test_a_401_is_authentication_and_a_403_authorization() -> None:
    from caldav.lib import error as dav_error

    from calendaring._caldav_io import _translate
    from calendaring.errors import AuthenticationError, AuthorizationError

    assert type(_translate(dav_error.AuthorizationError(url="x", status=401))) is AuthenticationError
    assert type(_translate(dav_error.AuthorizationError(url="x", status=403))) is AuthorizationError


def test_a_507_in_a_listing_is_a_cut_listing() -> None:
    """Stalwart lists up to its WebDav.maxResults and adds a 507 for the collection;
    caldav's search.limit hint names a 507 for the whole query too."""
    from caldav.lib import error as dav_error

    from calendaring._caldav_io import _translate
    from calendaring.errors import BackendError, ListingLimitError

    cut = dav_error.ResponseError("HTTP/1.1 507 Insufficient Storage", status=507)
    assert type(_translate(cut)) is ListingLimitError
    for whole in (dav_error.ReportError(url="x", status=507), dav_error.PropfindError(url="x", status=507)):
        assert type(_translate(whole)) is ListingLimitError, "a 507 for the whole query"
    assert type(_translate(dav_error.ResponseError("HTTP/1.1 500 X", status=500))) is BackendError
    assert type(_translate(dav_error.PutError(url="x", status=507))) is BackendError, "a full disk, not a listing"


def test_a_refused_create_says_what_was_taken() -> None:
    from caldav.lib import error as dav_error

    from calendaring._caldav_io import creating
    from calendaring.errors import AlreadyExistsError

    with pytest.raises(AlreadyExistsError) as uid, creating():
        raise dav_error.UidConflictError(url="x")
    assert uid.value.taken == "uid"
    with pytest.raises(AlreadyExistsError) as found, creating():  # caldav's own UID search found it
        raise dav_error.UidConflictConsistencyError(url="x")
    assert found.value.taken == "uid"
    with pytest.raises(AlreadyExistsError) as name, creating():
        raise dav_error.ConsistencyError("If-None-Match failed")
    assert name.value.taken == "native_id"


def test_the_etag_listing_leaves_out_the_calendar_itself() -> None:
    from calendaring._caldav_io import _etags

    class Response:
        def expand_simple_props(self, props: object) -> dict[str, dict[str, str]]:
            return {
                "/cal/": {"{DAV:}getetag": '"c"'},
                "/cal/a.ics": {"{DAV:}getetag": '"a"'},
                "/cal/b.ics": {},
            }

    assert _etags(Response(), "https://dav.example.com/cal") == {"/cal/a.ics": '"a"'}
