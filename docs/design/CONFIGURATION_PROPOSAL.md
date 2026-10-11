# One configuration file for caldav, calendaring-jmap and calendaring

**Status:** proposal for the pycal team, drafted by Claude Opus 5.5 on
2026-10-09; the format alternatives were added on 2026-10-11. Nothing is
decided.

**Feeds:** [roadmap 1.4](ROADMAP.md#14-configuration-and-credentials) and
[API spec §10, Q8](API_DESIGN.md#10-open-questions)

## The problem

A user with a CalDAV server, a JMAP account, a directory of `.ics` files
and a GitHub repository should describe them once. Today:

| | File | Format | Env vars |
|---|---|---|---|
| `caldav` | `~/.config/caldav/calendar.{conf,yaml,json}`, `/etc/caldav/…`, `CALDAV_CONFIG_FILE` | sections with `caldav_*` keys; `inherits`, `contains` (meta-sections), globs, `disable`, `${VAR}` expansion, `features:` profile | `CALDAV_*` |
| `calendaring-jmap` | `~/.config/calendaring-jmap/calendar.yaml`, `JMAP_CONFIG_FILE` | one flat mapping: `url`, `username`, `password`, `auth_type`, `timeout` | `JMAP_*` |
| `calendaring` | none yet (roadmap 1.4) | — | — |
| `plann` | caldav's file and parser (the parser started in plann and moved to caldav) | | |

Requirements:

1. One file covers every backend.
2. Each library works on its own: `caldav` must not need `calendaring` or
   `calendaring-jmap` installed, and vice versa.
3. No dependency cycles.
4. Existing caldav config files should keep working unchanged. This is
   wanted rather than required: a migration path would do, but there is
   no reason to break files that work.
5. Secrets never have to be stored in plain text (plann
   [issue #49](https://github.com/pycalendar/plann/issues/49) leaves
   credential storage to this library).

## The format: three alternatives

All three take the structure from caldav's format: named sections,
`inherits`, meta-sections with `contains`, globs, `disable` and `${VAR}`
expansion. The format in the caldav file is flexible,
and reusing its structure makes a shared parser possible. The formats
differ in how a section says what it connects to, and in how much of
caldav's format they keep. The examples use the same backends where they
can, with GitHub, the first tracker beyond the funded scope
([roadmap](ROADMAP.md#beyond-the-funded-scope)).

### F1. Backend prefixes, in caldav's file

The section's key prefix says which backend it is for. `caldav_url` is
already written this way, so the other backends follow the same pattern:

```yaml
work:
  caldav_url: https://dav.example.com/
  caldav_username: alice
  caldav_password_command: pass show dav/work      # new: secret from a command
  features: nextcloud

fastmail:
  jmap_url: https://api.fastmail.com/jmap/session
  jmap_username: alice@example.com
  jmap_password_keyring: fastmail                  # new: secret from the OS keyring

notes:
  files_path: ~/calendars/notes                    # vdir or single .ics

holidays:
  feed_url: webcal://example.com/holidays.ics

issues:
  github_repo: pycalendar/calendaring
  github_token_command: gh auth token

everything:
  contains: [work, fastmail, notes, holidays, issues]
```

- An explicit `backend:` key settles the rare section that has more than
  one prefix.
- Every existing caldav file is valid as it is, and caldav reads the new
  file as well (see [Compatibility with caldav](#compatibility-with-caldav)).
- The cost: no other library writes its keys this way. calendaring-jmap's
  own file says `url`, not `jmap_url`, so a JMAP user moving to the shared
  file has to rename every key. A plugin backend has to claim a prefix,
  and two plugins can claim the same one.

### F2. A `kind` key, with the keys `Backend.connect()` takes

A section is the keyword arguments of
[`Backend.connect()`](API_DESIGN.md#22-backend): `url`, `username`,
`password`, `token`, `kind` and backend-specific options.

```yaml
work:
  kind: caldav
  url: https://dav.example.com/
  username: alice
  password_command: pass show dav/work

fastmail:
  kind: jmap
  url: https://api.fastmail.com/jmap/session
  username: alice@example.com
  password_keyring: fastmail

notes:
  url: ~/calendars/notes             # a path means kind: files

holidays:
  url: webcal://example.com/holidays.ics     # webcal: means kind: feed

issues:
  kind: github                       # trackers cannot be detected
  repo: pycalendar/calendaring
  token_command: gh auth token

legacy:                              # an old caldav section, read unchanged
  caldav_url: https://old.example.com/
  caldav_user: alice

everything:
  contains: [work, fastmail, notes, holidays, issues, legacy]
```

- `kind` follows the rules `connect()` already has: a path or `file:`
  means files, `webcal:` means a feed, `http(s):` is probed for feed or
  CalDAV, and JMAP and the trackers must say `kind:`. Once the secret keys
  are resolved and the structural keys (`contains`, `inherits`, `disable`)
  are removed, `Workspace.from_config()` calls `connect(**section)` for
  each section, so the key names need no mapping. One gap: `connect()`
  takes `url` as a required argument, and a GitHub section needs none
  (`api.github.com` is the default). API_DESIGN §2.2 has to make `url`
  optional for such kinds.
- A section with `caldav_*` keys is read as `kind: caldav`, so existing
  files work in `calendaring` unchanged. `features` is the one key both
  styles share; any other mix of prefixed and unprefixed keys in one
  section is an error.
- calendaring-jmap's current file (`url`, `username`, `password`,
  `auth_type`, `timeout`) keeps every key name. Its users move to the
  shared file by indenting theirs under a section name and adding
  `kind: jmap`, since nothing probes for JMAP.
- A plugin backend registers a `kind` name, and its options are its own
  keys; nothing needs a prefix.
- The cost: caldav reads only `caldav_*` sections. For caldav (and plann)
  to use the new-style `work` section above, caldav's parser must learn
  `kind: caldav` with unprefixed keys. That is about ten lines in
  `extract_conn_params_from_section`, but it is a caldav change, and until
  it ships, a user who wants plann to see a server keeps that section in
  the old style. Also, caldav's `*` expands to every section, and caldav
  picks up any section with a `features:` key, ignoring an unprefixed
  `url`. So an F2 section must not use `features` until caldav knows
  `kind`, the same issue as F1 (see below).

### F3. A new file in TOML, which includes the caldav file

A new file, `~/.config/pycal/calendar.toml`, in F2's keys. Old caldav
files are not converted; they are pulled in:

```toml
include = ["~/.config/caldav/calendar.conf"]   # its sections become kind = "caldav"

[fastmail]
kind = "jmap"
url = "https://api.fastmail.com/jmap/session"
username = "alice@example.com"
password_keyring = "fastmail"

[issues]
kind = "github"
repo = "pycalendar/calendaring"
token_command = "gh auth token"

[everything]
contains = ["work", "fastmail", "issues"]      # work comes from the included file
```

- `tomllib` is in the standard library from Python 3.11, which is
  `calendaring`'s floor ([D4](PRIOR_ART_AND_DECISIONS.md#d4-python-version-floor)), so reading
  the file itself needs no PyYAML. An included caldav file written in YAML,
  as caldav's docs write it, still does.
- caldav's file stays caldav's, and caldav's format does not change.
- The cost: two files, and the user has to know which one a section lives
  in. caldav and plann never see the TOML sections, so the "one file for
  every backend" requirement holds only from `calendaring`'s side. It is
  also a fifth format, which roadmap 1.4 says to avoid, and TOML's
  `[section]` syntax looks nothing like the YAML in caldav's docs.

### Comparison

| | F1. prefixes | F2. `kind` + `connect()` keys | F3. new TOML file |
|---|---|---|---|
| Old caldav files work in `calendaring` | yes | yes | yes, by `include` |
| caldav and plann read the new sections | yes, after the secret-key and search-path changes | after those changes plus ~10 lines for `kind` | never |
| calendaring-jmap's file | keys renamed | keys kept; indent, add `kind` | re-typed as TOML |
| Maps onto `Backend.connect()` | through a prefix table | directly | directly |
| Plugin backends | must claim a prefix | register a `kind` | register a `kind` |
| Files the user keeps | 1 | 1 | 2 |
| Needs PyYAML | yes | yes | only for an included YAML file |

**Proposal: F2.** It keeps caldav's file as it is and every key name of
calendaring-jmap's in one file, and a section is the argument list of the
API that consumes it. F1 needs no format change in caldav, but pays for it by renaming
every JMAP key and handing out prefixes to plugins. F3 is the clean break,
but it gives up the single file, which is requirement 1. The caldav change
F2 needs is small, and F2 works before it lands: the old style keeps working
everywhere.

### Compatibility with caldav

Checked against caldav `master` (`e7b5b1d8`) on 2026-10-11:

- **caldav ignores sections it cannot use, with one exception.**
  `extract_conn_params_from_section` returns `None` for a section with
  neither a `caldav_url` nor a `features` key, so `jmap_*`, `github_*` and
  `kind:` sections are skipped, even when `*` expands to them. The
  exception: a non-CalDAV section with a `features:` key would be picked
  up, so `features` must stay CalDAV-only, or caldav must learn to check
  the prefix or `kind`.
- **`inherits` leaks across backends.** `config_section` merges the parent
  section's keys in, so a JMAP section that inherits from a section with a
  `caldav_url` (to share a username, say) looks like CalDAV to caldav.
  The shared parser should refuse inheritance between different kinds.
- **caldav needs two changes under F1 and F2:**
  - It must understand the new secret keys below. Today it keeps only the
    keys in its `CONNKEYS` list, so `caldav_password_command` would be
    dropped silently, and caldav would connect with no password.
  - It must search the new file path (question 2 below).

  Under parser option B below both land in caldav's parser; under
  parser option C they come with the shared package.
- **caldav parses YAML with `yaml.Loader`**, which builds arbitrary Python
  objects from tags (`caldav/config.py`, line 108). A file shared by
  several tools, and searched in `/etc/`, should be read with
  `yaml.safe_load`. That is a caldav fix whichever alternative is chosen.

### Secrets

The same for all three formats: every password (or token) can also
come from `password_command` (the command's stdout, the way vdirsyncer's
`fetch = ["command", …]` works) or `password_keyring` (looked up with
`keyring`, an optional extra), spelled `caldav_password_command` and so on
under F1. `${VAR}` expansion stays as it is. GitHub is the case that shows
`_command` earns its place: `gh auth token` hands over the token the user
already has, and no keyring setup is needed.

## Where the parser lives

The generic part of caldav's `config.py` (section expansion, `inherits`,
env expansion, file discovery) is about 150 of its 650 lines. The rest is
caldav-specific: `caldav_*` keys, feature profiles, and test-server
handling.

| Option | Packages | Duplication | Follows D1? | calendaring-jmap reads the full format standalone? |
|---|---|---|---|---|
| **A.** Each library keeps or copies its own parser | 0 new | 3 copies that drift | yes | yes, by copying |
| **B.** `calendaring` imports caldav's generic functions; calendaring-jmap keeps its simple flat loader | 0 new | none | not quite: generic code stays in caldav | **no**, only through calendaring |
| **C.** A small package (say `pycal-config`) holding the generic parser and the secrets lookup, used by all three | 1 new | none | yes | yes |
| **D.** Move the parser into `calendaring` and have caldav import it | 0 new | none | yes | — **rejected**: caldav would depend on calendaring, and calendaring on caldav, a cycle |

[D1](PRIOR_ART_AND_DECISIONS.md#d1-packaging-principle) is the principle
that logic which isn't CalDAV logic doesn't belong in `caldav`.

**Proposal: B now, and C only if the team wants calendaring-jmap to read
the shared file without calendaring.** B costs nothing today:
`calendaring` already depends on `caldav`, and caldav's parser is public
(`expand_config_section`, `config_section`, `read_config`,
`expand_env_vars`). What makes a later move to C cheap is that **the format
is documented once, as a specification**, not defined by whichever parser
reads it. If the code later moves from caldav into a package, no user's
file changes.

With format F2, option B leaves calendaring-jmap close to reading the
shared file on its own: its flat loader already reads one F2 section, so
pointing it at a named section is all it needs for files without
`inherits` or meta-sections.

The number of packages matters. Every new one is another release, CI
setup and version matrix for the same few people. C is worth that only if
someone uses calendaring-jmap without calendaring and wants more than
one flat section.

## Questions for the team

1. Should `calendaring-jmap` on its own read the shared file, with
   meta-sections and inheritance? If yes, go with parser option C; if no, B.
2. The file name. caldav's `~/.config/caldav/calendar.conf` is an odd home
   for JMAP and GitHub sections. **Proposal:** `~/.config/pycal/calendar.yaml`
   (`calendar.toml` if F3 is chosen) as the first place searched, with caldav's current paths still read as
   fallbacks, so that nobody has to move a file. caldav does not search
   `~/.config/pycal/` today, so this is one of the two caldav changes
   above.
3. Are `_password_command` and `_password_keyring` the right two
   mechanisms, or is one of them enough?

---

*Drafted with AI assistance (Claude Opus 5.5 via Claude Code) from the
source of `caldav` (`caldav/config.py`, `docs/source/configfile.rst`) and
`calendaring-jmap` (`_config.py`) as checked out on 2026-10-09, rechecked on 2026-10-11 (caldav `e7b5b1d8`,
calendaring-jmap `004c6b4`).*
