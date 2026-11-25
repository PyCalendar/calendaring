# The X- properties calendaring writes

Where iCalendar has no property for something, calendaring writes an `X-`
property under the prefix `X-PYCAL-` (see [API_DESIGN
§3.6](design/API_DESIGN.md#36-properties-with-no-standard-home)).  Other
pycal tools, such as plann, write the same ones.  Each is listed here with
the standard property that would replace it, should one appear.

| Property | On | Value | Meaning | Standard replacement |
|---|---|---|---|---|
| `X-PYCAL-TIME-ENTRY` | `VEVENT` | `TRUE` | the event is a time-log entry, for the task or event its `RELATED-TO;RELTYPE=PARENT` names | none yet |
| `X-PYCAL-TIMER-START` | `VTODO` | UTC date-time | the timer on the task is running, since then; removed when it stops | none yet |

## Retired

No longer written or read.  Data that still carries them is kept as it is,
like any other `X-` property, and lost where the backend drops `X-`
properties.

| Property | On | Written by | Why it went |
|---|---|---|---|
| `X-PYCAL-PLANNED-START`, `X-PYCAL-PLANNED-END` | `VTODO` | 0.1.0, `Task.planned_start` and `planned_end` | no calendar client reads a planned start or end on a task; time planned for one is a `VEVENT` linked to it with `RELATED-TO`, which every client shows |
