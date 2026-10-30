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
