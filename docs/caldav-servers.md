# The tests on caldav's servers

CI runs the tests on the embedded Xandikos and Radicale only.  The
servers below were run by hand (see
[CONTRIBUTING.md](../CONTRIBUTING.md#testing-against-more-caldav-servers)),
with caldav's master branch plus the change moving its search-cache
helper into `caldav.testing` (due in caldav 3.8).

## The stories (`tests/test_scenarios.py`), 2026-10-30

12 stories per server, run with `-n 16 --dist loadgroup`.

| Server | Result | Why |
|---|---|---|
| Baikal, Bedework 3, CCS, Cyrus, Davical, Davis, Nextcloud, Ox, Radicale, Xandikos, Zimbra (docker and work-tests), infomaniak, robur | pass | |
| SOGo, gmx | pass, 1 skipped | a calendar asked for events holds tasks too, so "a calendar for events refuses tasks" has nothing to check |
| Bedework | pass, 5 skipped | it makes no task lists; the task stories that check a refusal still run |
| Stalwart | pass; now and then an error under load | the docker server closed the connection on 3 MKCALENDARs in two parallel runs; alone, and in 150 quick create/delete cycles, it never did |
| ecloud | pass, over several runs | it allows about a dozen new calendars an hour (429 on MKCALENDAR, without Retry-After), and each story makes one or two: a full run errors on most stories, and rerunning those an hour apart passed them all |
| posteo | all skipped | it cannot make calendars, and the stories must not write into the user's own |
| jeanes | all skipped | it cannot delete calendars, so every story would leave one behind |
| purelymail | pass | run alone: its search index lags writes by up to 180 s, and the stories wait for that only before a search that needs the index |

With caldav 3.7, whose `caldav.testing` has no search-cache helper,
nothing waits for an index that lags behind writes: three stories fail on
Bedework 3 (an event or task just written is not found yet), and
purelymail fails most stories that search.

## The conformance tests (`tests/*/test_caldav_conformance.py`), 2026-11-17

One server at a time, with caldav's master branch.  Zimbra, Ox, Cyrus,
Davical, Davis, Baikal, Nextcloud, Radicale, Xandikos, CCS, Bedework 3
and Stalwart passed on 2026-11-12.  SOGo waits for caldav's SOGo hints;
ecloud has not been run to the end.

| Server | Result | Why |
|---|---|---|
| Bedework 5 (sync suite only) | pass, 35 skipped | the skips need tasks or journals, which its calendar does not hold.  It stores each ATTENDEE twice, and caldav drops the copy |
| gmx | pass | |
| robur | passes on rerun | about one request in 60 hangs for 70 s and is then reset, and some PUTs get a 500; seeding retries a failed write, but a test that reads 120 items still fails some runs, and `--last-failed` passed them all |
| purelymail | pass | run alone, in 1 h 54 |
| infomaniak | pass, 2 skipped | its calendar holds no journals.  It stores URL as `URL;VALUE=URI`, which verify took for a change before 2026-11-18 |
| posteo | pass, 10 skipped | it cannot make calendars, so the 8 tests that need a second one (move, a token from another collection, creating one) skip; and it holds no journals |
| jeanes | pass | 5 h 09: the account held 469 leftover calendars, each listing one PROPFIND per calendar.  384 were deleted through DSM's web API during the run |
