# wmguard

Check a sync watermark against the truth. It flags a mark taken from a table whose MAX has frozen,
and a mark that moved past rows nobody applied. It reads timestamps and CSV files and opens no
connection.

A street-address table was registered as versioned, and editors kept editing it every day. A
nightly job copied new and changed addresses into a serving table. The job kept a high-water mark,
and each night it set the mark to `MAX(DateUpdate)` of the address table. Then it selected the rows
stamped after the mark.

With traditional versioning, an edit goes into the adds table and the deletes table. The base table
changes only when a compress moves those rows into it. The compress was blocked, so the base table
stopped changing, and its `MAX` stopped with it. The mark took that frozen value every night. The
job found nothing after the mark, applied nothing, and reported "zero pending". That was true of
the base table. It was not true of the addresses, which kept changing in the delta tables. The
serving table fell behind for weeks, and no step failed.

The repair was to read the `_evw` view, which merges the delta tables with the base table. A
second defect sat in the same job. A sync step set the mark to the newest stamp in the source
without asking whether the rows below it had been applied. The job selects strictly after the
mark. So an unapplied row at or below the new mark could never be selected again. A delta step that
returned early, or an edit that landed while a long step ran, made the mark skip rows for good.
That defect was caught before it lost any edits.

This tool judges both from numbers you export. It does not need the database, and it does not
need a geodatabase.

```
wmguard self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
...
PASS  a watermark equal to a frozen base MAX, with the view ahead, is FROZEN  <-- pinned defect
...
PASS  a base MAX inside the age limit that the view is ahead of is the normal state between compresses: a note, exit 0  <-- pinned defect
...
PASS  a row in the window with a blank key is UNDATED, never guessed APPLIED  <-- pinned defect
...
PASS  an unapplied row inside the window is STRANDED  <-- pinned defect
...
PASS  an unapplied row above the new mark (PENDING) also caps the safe mark: it never rises past a row the next run still needs  <-- pinned defect
...
PASS  an unapplied row at or below the old mark is reported as a note, not hidden behind a clean verdict  <-- pinned defect
...
PASS  a mark past every source stamp is AHEAD even when nothing is stranded, and it exits 1  <-- pinned defect
...
PASS  a source with no readable stamp compared nothing: exit 4, not clean  <-- pinned defect
...
PASS  an applied file with rows and none readable compares nothing, instead of reporting every row STRANDED  <-- pinned defect
...
PASS  without --apply nothing is written  <-- pinned defect
...
PASS  --out naming any other existing file is refused, not overwritten  <-- pinned defect
...
PASS  and a zero value counts as given: 0 is not False  <-- pinned defect
...
PASS  --ap, a prefix of a write flag, is refused and writes nothing  <-- pinned defect
...
PASS  the module imports no network library, so it has no connection to leak a credential from  <-- pinned defect
...
--------------------------------------------------------------------
186 assertions, 0 failed
```

The full run prints all 186 assertions. Each `...` line above is where this block is cut.

## What already exists

Data-loading libraries know the problem and handle it inside their own state. dlt's incremental
cursor has a `lag` setting, which, in the documentation's words, "ensures that a defined period of
data remains refreshed, capturing updates or changes within the attribution window." Its
`on_cursor_value_missing` setting decides what happens to a row whose cursor is missing or `None`:
the default raises, and the other two values include or exclude the row
([lag](https://dlthub.com/docs/general-usage/incremental/lag),
[cursor](https://dlthub.com/docs/general-usage/incremental/cursor)). If you load with dlt, set both
and you have a defence that this tool cannot give you.

dbt has a direct equivalent of the `STALE` check. A source or model can set `warn_after` and
`error_after`, which say "how old the most recent data can be before a freshness check reports a
warning" (or an error), and `loaded_at_field`, the "column dbt queries to determine the most recent
loaded timestamp"
([freshness](https://docs.getdbt.com/reference/resource-properties/freshness)). It is an age limit
that you declare in the project files. dbt queries the table itself, so it needs no export step. That page calls `dbt source freshness` a legacy command and recommends `dbt freshness`. If you
run dbt, use it for the age limit. It checks one table against the clock. It does not compare that
table with its current-state view, a stored mark, or the rows that were applied.

This tool is for the sync that someone wrote by hand, with a watermark table and a stored
procedure. No library guards that mark. The tool compares the stored mark with two things the
libraries above never see: the current-state view of the same table, and the rows that were
actually applied.

On 2026-10-13 I searched GitHub for `wmguard`, `watermark guard`, `incremental watermark
validation`, `stale high watermark detector`, `high water mark skipped rows`, `versioned _evw
watermark` and `watermark validation`. I judged the results from their descriptions and read no
code. `wmguard` returned only this repository. `watermark guard` returned image- and video-watermarking
tools. `watermark validation` returned an audio-forensics app, a PHP library and an
Azure Databricks incremental-load project, which is a pipeline and not a checker. The other four
searches returned nothing. That shows only that these searches found nothing equivalent on that
day.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no database.

The self-test prints the same 186 assertions on Windows (Python 3.13.2, 3.12.10 and 3.10.20),
on Python 3.9.25 and on Ubuntu (Python 3.12.3). The outputs are identical line for line, once the
carriage returns that Windows adds are removed. `coverage run --branch wmguard.py --self-test`
reports 100 percent of lines and branches (829 statements, 140 branches).

```
git clone https://github.com/uhsear/wmguard.git
```

## Usage

Pick a mode first. Then name the numbers that mode needs.

```
python wmguard.py --self-test
python wmguard.py frozenmark --watermark W --base-max B --view-max V
python wmguard.py frozenmark --watermark W --base-max B --now N --max-age-hours 24
python wmguard.py safemark --source source.csv --applied applied.csv --old-watermark W --new-watermark W2
python wmguard.py safemark --source source.csv --applied applied.csv --old-watermark W --new-watermark W2 --out rows.csv --apply
```

Every timestamp is `YYYY-MM-DD HH:MM:SS`, with an optional fraction of up to six digits and no time
zone. A `T` instead of the space works too, and so does a bare date. Python's `strptime` also
accepts a month, day or hour with one digit, so `2026-3-1 8:30:00` parses.

To get the numbers, run plain `SELECT` statements on the table the job reads. These statements are
standard T-SQL and the names are examples. They were not run against SQL Server in this build (see
Limits).

```sql
SELECT MAX(DateUpdate) FROM gis.Addresses;       -- --base-max
SELECT MAX(DateUpdate) FROM gis.Addresses_evw;   -- --view-max
SELECT AddressKey AS [KEY], DateUpdate AS [STAMP] FROM gis.Addresses_evw;   -- --source
SELECT AddressKey AS [KEY], SourceDateUpdate AS [STAMP] FROM gis.Serving;   -- --applied
```

Take `--view-max` from a fresh session that has set no version. An `_evw` view reads the state of
the version the session set, and it does not move on (see Sources).

| Flag | Mode | What it does |
|---|---|---|
| `MODE` | both | `frozenmark` or `safemark`. Required unless `--self-test`. |
| `--watermark` | frozenmark | The stored watermark. Required. |
| `--base-max` | frozenmark | `MAX(stamp)` of the table the job reads. Required. |
| `--view-max` | frozenmark | `MAX(stamp)` through the `_evw` view. Off by default. |
| `--now` | frozenmark | The time to measure age from. Default: this machine's clock. |
| `--max-age-hours` | frozenmark | A base MAX older than this is stale. Default `48`. `0` to `1000000`. |
| `--source` | safemark | CSV of the source rows. Required. |
| `--applied` | safemark | CSV of the rows that were applied. Required. |
| `--old-watermark` | safemark | The mark before the sync. Required. |
| `--new-watermark` | safemark | The mark the sync wrote. Required. |
| `--key-field` | safemark | The key column of `--source`. Default `KEY`. |
| `--stamp-field` | safemark | The stamp column of `--source`. Default `STAMP`. |
| `--applied-key-field` | safemark | The key column of `--applied`. Default: the source's. |
| `--applied-stamp-field` | safemark | The stamp column of `--applied`. Default: the source's. |
| `--sample` | safemark | How many rows to list under each heading. Default `10`. `0` lists none. |
| `--out` | safemark | Write the class of every source row to this new CSV. Needs `--apply`. |
| `--apply` | safemark | Actually write `--out`. Without it nothing is written. |
| `--self-test` | none | Run the assertions and exit. Other flags are ignored. |

A flag that belongs to the other mode is refused with exit 64, so a run never looks defended by a
flag it did not read. A zero value counts as given: `frozenmark --sample 0` and
`safemark --max-age-hours 0` are refused. A flag given an empty value, such as `--out ""` or
`--key-field ""`, is refused with exit 64 too, and is never read as "not given".

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused with
exit 64 and is not read as `--apply`. The self-test also pins `--app`, `--appl`, `--ou`, `--sour`,
`--old`, `--base` and `--max`.

`--out` must name a path that does not exist. A path that exists, whether an input file, a hard link
to one, a directory or any other file, is refused with exit 64, on a dry run too. The file is opened
in exclusive-create mode, so a path that appears between the check and the write is not overwritten
either. With `--apply` and a source that compared nothing, nothing is written.

## frozenmark: is the MAX frozen?

Give it the stored mark, the `MAX` the job reads, and optionally the `MAX` through the view. A
base table that a blocked compress froze has an older `MAX` than its view. The demo has no data
behind it. Every number below is typed on the command line.

```
$ python wmguard.py frozenmark --watermark "2026-01-05 00:00:00" --base-max "2026-01-05 00:00:00" --view-max "2026-03-01 08:30:00" --now "2026-03-01 09:00:00"
wmguard frozenmark: watermark 2026-01-05 00:00:00, base MAX 2026-01-05 00:00:00, view MAX 2026-03-01 08:30:00, now 2026-03-01 09:00:00, limit 48.0 h
FROZEN    the view MAX 2026-03-01 08:30:00 is 1328.5 h ahead of the base MAX 2026-01-05 00:00:00, and the base MAX is 1329.0 h old, over the limit of 48.0 h. The watermark equals the base MAX, so a job that reads the base table selects nothing.
VERDICT: 1 finding(s). Do not trust this watermark.
exit 1

$ python wmguard.py frozenmark --watermark "2026-01-05 00:00:00" --base-max "2026-01-05 00:00:00" --view-max "2026-01-05 00:00:00" --now "2026-03-01 09:00:00"
wmguard frozenmark: watermark 2026-01-05 00:00:00, base MAX 2026-01-05 00:00:00, view MAX 2026-01-05 00:00:00, now 2026-03-01 09:00:00, limit 48.0 h
IDLE      the base MAX is 1329.0 h old, and the view MAX is not ahead of it. The table is quiet, not frozen.
VERDICT: no frozen MAX and no mark past the source found.
exit 0

$ python wmguard.py frozenmark --watermark "2026-01-05 00:00:00" --base-max "2026-01-05 00:00:00" --now "2026-03-01 09:00:00"
wmguard frozenmark: watermark 2026-01-05 00:00:00, base MAX 2026-01-05 00:00:00, view MAX not given, now 2026-03-01 09:00:00, limit 48.0 h
STALE     the base MAX 2026-01-05 00:00:00 is 1329.0 h old, over the limit of 48.0 h. Without --view-max a frozen table and an idle one look the same.
VERDICT: 1 finding(s). Do not trust this watermark.
exit 1

$ python wmguard.py frozenmark --watermark "2026-03-01 08:00:00" --base-max "2026-03-01 08:00:00" --view-max "2026-03-01 08:20:00" --now "2026-03-01 08:30:00"
wmguard frozenmark: watermark 2026-03-01 08:00:00, base MAX 2026-03-01 08:00:00, view MAX 2026-03-01 08:20:00, now 2026-03-01 08:30:00, limit 48.0 h
BASE_LAGS the view MAX 2026-03-01 08:20:00 is 0.3 h ahead of the base MAX 2026-03-01 08:00:00, and the base MAX is within the limit of 48.0 h. A versioned base table lags its view until the next compress, so this alone is not a freeze.
VERDICT: no frozen MAX and no mark past the source found.
exit 0
```

The four runs show the cases that matter. The first run has a view that is 1328.5 hours ahead of
the base, a base MAX over the age limit, and a mark equal to the base. The second run has an old
base that the view does not pass, so the table is quiet and not frozen. The third run has no view.
An old base MAX then looks the same whether the table is frozen or idle, so the tool says that and
exits 1. In the fourth run the view is ahead by 20 minutes and the base is recent. A versioned
table is in that state between compresses, so it is a note and the exit code is 0.

| Code | Finding? | When |
|---|---|---|
| `FROZEN` | yes, exit 1 | The view MAX is ahead of the base MAX, the base MAX is older than `--max-age-hours`, and the mark is at or behind the base MAX. |
| `STALE` | yes, exit 1 | No view was given, and the base MAX is older than `--max-age-hours`. |
| `AHEAD` | yes, exit 1 | The mark is past every stamp the source has. A row stamped at or below it that arrives later is never selected. |
| `BASE_LAGS` | note | The view MAX is ahead of the base MAX, but the base MAX is within the age limit, or the mark is above the base MAX so it did not come from the base table. |
| `IDLE` | note | The base MAX is old, but the view is not ahead of it. The table is quiet. |
| `FRESH` | note | The base MAX is recent, and the mark is not past it. |

`FROZEN` means that the base table lags its view now and has done so for longer than the limit,
and that the mark sits where a job reading the base table would put it. One snapshot cannot show
that a table stopped moving. A blocked compress that is younger than `--max-age-hours` shows up as
`BASE_LAGS` until it ages past the limit. A job that reads the view keeps a mark above the base
MAX, so it is not flagged.

The age limit is inclusive. A base MAX exactly at the limit is not stale, and one second over is.
A base MAX in the future of `--now` is not stale. A view MAX behind the base MAX is not freeze
evidence, because deleting the newest row lowers the view MAX.

## safemark: did the mark skip rows?

The job selects the rows with a stamp strictly after the mark. Give the tool the source rows, the
rows that were applied, and the mark before and after the sync. It puts each source row in one
class, and the counts add up to the number of rows that went in. It judges only the rows stamped
above `--old-watermark`.

| Class | Meaning |
|---|---|
| `APPLIED` | The row was applied at its own stamp or a newer one. |
| `STRANDED` | The row is unapplied, and its stamp is above the old mark and at or below the new mark. No later run selects it. |
| `PENDING` | The row is unapplied, and its stamp is above the new mark. The next run still selects it. |
| `BEHIND` | The stamp is at or below the old mark. The row is outside this sync. It may be applied or not. |
| `UNDATED` | The stamp is blank or in a format the tool does not read, or the row is above the old mark and has a blank key. It is not judged. |

The demo has eight source rows and three applied rows. Both files are synthetic.

```
$ cat source.csv
KEY,STAMP
P-101,2026-03-01 01:10:00
P-102,2026-03-01 01:40:00
P-103,2026-03-01 02:05:00
P-104,2026-03-01 02:05:00
P-105,2026-03-01 03:30:00
P-106,2026-03-01 08:15:00
P-107,
P-090,2026-02-27 17:00:00
$ cat applied.csv
KEY,STAMP
P-101,2026-03-01 01:10:00
P-103,2026-03-01 02:05:00
P-105,2026-03-01 03:30:00
```

```
$ python wmguard.py safemark --source source.csv --applied applied.csv --old-watermark "2026-03-01 00:00:00" --new-watermark "2026-03-01 06:00:00"
wmguard safemark: source.csv 8 row(s), applied.csv 3 row(s)
window: stamp after 2026-03-01 00:00:00, new watermark 2026-03-01 06:00:00
APPLIED 3, STRANDED 2, PENDING 1, BEHIND 1, UNDATED 1
STRANDED  the new watermark skips 2 unapplied row(s). A job that selects stamp > watermark never sees them again. A safe mark is 2026-03-01 01:10:00.
UNDATED   1 source row(s) have a blank key or a stamp the tool cannot read, so they are not judged. A NULL stamp is never greater than a mark, and a stamp in another format is not read.
OUTSIDE   1 unapplied row(s) have a stamp at or below the old watermark, so this run does not judge them. An earlier run may have skipped them. Pass an early --old-watermark, such as 1900-01-01, to judge the whole history.
STRANDED rows:
  row 2  key P-102  stamp 2026-03-01 01:40:00
  row 4  key P-104  stamp 2026-03-01 02:05:00
UNDATED rows:
  row 7  key P-107  stamp none
safe watermark: 2026-03-01 01:10:00
VERDICT: 2 finding(s). Do not trust this watermark.
exit 1
```

Five things in that run need a second look.

- `P-102` and `P-104` are `STRANDED`. The new mark of 06:00 is above both, and neither was applied.
- `P-103` and `P-104` share the stamp 02:05, and only `P-103` was applied. The safe mark is
  01:10, and not 02:05. A mark of 02:05 would skip `P-104`. At 01:10 the job selects `P-103`
  again, so the apply step must be safe to repeat. The message says "a safe mark", and not "the
  highest": any mark below the oldest unapplied stamp skips nothing, and the tool names the newest
  applied stamp under it.
- `P-107` has no stamp. Comparing NULL with the mark gives UNKNOWN, so `stamp > mark` can never
  select it, whatever the mark is. That is a finding of its own.
- `P-090` is `BEHIND`. It is older than the old mark and is not part of this sync. It is not in
  the applied file either, so the `OUTSIDE` note counts it. The tool cannot tell a row that an
  earlier run skipped from a row that was never meant to be applied.
- The verdict says "no skipped row found above the old watermark" when it finds nothing. It does
  not claim to have judged rows below the old mark.

To judge the whole history, pass an early `--old-watermark`. `P-090` is then in the window and the
run finds it:

```
$ python wmguard.py safemark --source source.csv --applied applied.csv --old-watermark 1900-01-01 --new-watermark "2026-03-01 01:10:00"
wmguard safemark: source.csv 8 row(s), applied.csv 3 row(s)
window: stamp after 1900-01-01 00:00:00, new watermark 2026-03-01 01:10:00
APPLIED 3, STRANDED 1, PENDING 3, BEHIND 0, UNDATED 1
STRANDED  the new watermark skips 1 unapplied row(s). A job that selects stamp > watermark never sees them again. A safe mark is 1900-01-01 00:00:00.
UNDATED   1 source row(s) have a blank key or a stamp the tool cannot read, so they are not judged. A NULL stamp is never greater than a mark, and a stamp in another format is not read.
STRANDED rows:
  row 8  key P-090  stamp 2026-02-27 17:00:00
UNDATED rows:
  row 7  key P-107  stamp none
safe watermark: 1900-01-01 00:00:00
VERDICT: 2 finding(s). Do not trust this watermark.
exit 1
```

Move the mark to the safe value and run again with the original old mark:

```
$ python wmguard.py safemark --source source.csv --applied applied.csv --old-watermark "2026-03-01 00:00:00" --new-watermark "2026-03-01 01:10:00"
wmguard safemark: source.csv 8 row(s), applied.csv 3 row(s)
window: stamp after 2026-03-01 00:00:00, new watermark 2026-03-01 01:10:00
APPLIED 3, STRANDED 0, PENDING 3, BEHIND 1, UNDATED 1
UNDATED   1 source row(s) have a blank key or a stamp the tool cannot read, so they are not judged. A NULL stamp is never greater than a mark, and a stamp in another format is not read.
OUTSIDE   1 unapplied row(s) have a stamp at or below the old watermark, so this run does not judge them. An earlier run may have skipped them. Pass an early --old-watermark, such as 1900-01-01, to judge the whole history.
UNDATED rows:
  row 7  key P-107  stamp none
safe watermark: 2026-03-01 01:10:00
VERDICT: 1 finding(s). Do not trust this watermark.
exit 1
```

The stranded rows are gone. `P-102` and `P-104` are now `PENDING`, so the next run selects them.
The `UNDATED` finding stays, because no mark fixes a row with no stamp.

`--out rows.csv --apply` writes one line per source row. Without `--apply`, the same run prints
"Check only" and writes nothing. This is the file from the run above:

```
row,key,stamp,class
1,P-101,2026-03-01 01:10:00,APPLIED
2,P-102,2026-03-01 01:40:00,STRANDED
3,P-103,2026-03-01 02:05:00,APPLIED
4,P-104,2026-03-01 02:05:00,STRANDED
5,P-105,2026-03-01 03:30:00,APPLIED
6,P-106,2026-03-01 08:15:00,PENDING
7,P-107,,UNDATED
8,P-090,2026-02-27 17:00:00,BEHIND
```

Other findings of this mode:

| Code | Finding? | When |
|---|---|---|
| `STRANDED` | yes, exit 1 | At least one row is `STRANDED`. The message gives a safe mark. |
| `UNDATED` | yes, exit 1 | At least one source row has a blank key or a stamp the tool cannot read. |
| `AHEAD` | yes, exit 1 | The new mark is past the newest source stamp. This fires when no row is stranded. A mark set to the wall clock, and not to a data value, does this. |
| `REGRESSED` | note | The new mark is behind the old one. Rows between them are selected twice and none is lost. |
| `OUTSIDE` | note | At least one row at or below the old mark is not in the applied file. This run does not judge it. |
| `SAFE` | note | None of the findings above, and at least one row was compared. |

A row that was applied and then edited is `STRANDED` or `PENDING` at its new stamp, because the
applied file shows the older stamp. A key that appears twice in the applied file keeps its newest
stamp.

A blank key never matches. A source row above the old mark with a blank key is `UNDATED`. An applied
row with a blank key is counted as unreadable and left out. Without that rule, one blank-key applied
row would mark every blank-key source row as applied, and the run would exit 0.

If the applied file has rows and none has a readable key and stamp, the tool compares nothing and
exits 4. A date format it does not read, such as `3/1/2026 1:10:00 AM`, would otherwise make every
source row look unapplied, and a scheduled job could not tell that from a real finding.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing found. Notes may still print. |
| 1 | At least one finding. |
| 2 | A file could not be read, or `--out` could not be written. |
| 4 | Nothing was compared: no source row could be judged, or no applied row was readable. |
| 64 | Usage error: a bad flag, a bad stamp, a wrong-mode flag, an empty flag value, an existing `--out`, or a column the file lacks. |
| 70 | An unexpected crash in the tool. It is never a finding about the data. |

Exit 4 is not exit 0 on purpose. A source that was exported with the wrong column, or with every
stamp blank, would otherwise report a clean run. A scheduled job reads 0 as "safe".

## Limits

- **The freeze itself is not reproduced.** No ArcGIS Enterprise geodatabase or SQL Server was
  available for this build. The tool judges the numbers you give it. It does not check that
  `--view-max` really came from a view, or that the base table was ever versioned. The behaviour
  it relies on is cited in Sources and was not run.
- **The four `SELECT` statements above were not run against SQL Server.** Their semantics come from
  the Microsoft pages in Sources. The `CSV` shape they produce is the shape the self-test reads.
- **`FROZEN` is a rule and not a proof.** It needs the base MAX to be older than `--max-age-hours`.
  A table frozen for less than that time reports `BASE_LAGS`. Set the limit from the time your
  compress normally takes to catch up.
- **It assumes the job selects `stamp > mark`.** A job that selects `stamp >= mark` re-reads a row
  that sits exactly on the mark, so the tool would flag that row as stranded when it is not.
  Subtract one tick of your column (3 ms for `datetime`) from `--new-watermark` for such a job.
- **A `safemark` run judges only rows above `--old-watermark`.** To audit all history, pass an
  early `--old-watermark`, such as `1900-01-01`. Rows at or below the old mark are not judged, and
  the `OUTSIDE` note only counts the ones that are missing from the applied file.
- **Stamps are timestamps only.** A watermark that is an integer or a row version is not read. A
  stamp must have no time zone and at most six fractional digits. A `datetime2(7)` value with seven
  digits is refused and not rounded, so trim it in the query. SQL Server `datetime` is "rounded to
  increments of .000, .003, or .007 seconds" and has no time zone offset, so two exports of it
  compare exactly.
- **Other stamp formats are `UNDATED`.** Esri-formatted dates such as `3/1/2026 1:00:00 AM`, and
  epoch milliseconds, are not parsed. Convert them to `YYYY-MM-DD HH:MM:SS` in the export query.
  When every source stamp is in one of those formats, the run exits 4.
- **The exports must come from one moment.** A row edited between the `--source` export and the
  `--applied` export looks stranded or pending. Export both right after the sync, in one
  transaction if you can.
- **The applied file must carry the source stamp.** If the serving table stores its own load time,
  every row looks unapplied, and the tool reports a long list of `STRANDED` rows. Copy the source
  stamp into the serving row when you apply it.
- **A view MAX can be stale too.** A session that set a version keeps reading that version's state
  (see Sources). Take the view MAX from a session that set none.
- **`STALE` cannot tell frozen from idle.** Without `--view-max`, a quiet table over a long
  weekend looks the same as a frozen one. Give `--view-max`, or set `--max-age-hours` from your
  own edit rate. `48` is a guess.
- **`--now` defaults to this machine's clock.** The stamps have no time zone, so the clock and the
  stamps must use the same zone. Pass `--now` from the database server's clock when they differ.
- **A duplicate key in the source** is judged row by row. Each row is `APPLIED` when its key's
  newest applied stamp is the same or newer.
- **Keys are compared as trimmed text.** `P-1` and `p-1` are two keys, and so are `7` and `07`.
- **Only comma-separated UTF-8 CSV is read.** UTF-16, tab-delimited and semicolon-delimited files
  exit 64. A UTF-16 file gets its own message. A tab or semicolon file gets "columns not in the
  CSV", with a header line that shows the problem. Other bytes are decoded lossily without a
  warning, and two keys can collapse into one. A BOM at the start of a UTF-8 file is removed. A NUL
  byte inside a file gives exit 2 on Python 3.9 and 3.10, where the csv module refuses it, and is
  read as part of the text on Python 3.11 and later. A field over csv's default limit of 131072
  characters gives exit 2 on every version.
- **An empty file is a usage error.** A zero-byte `--source` or `--applied` exits 64 with "columns
  not in the CSV", and not 2.
- **A header-only source** has no rows to compare. It exits 4 and prints no `SAFE` line.
- **A wrong column name echoes the header.** The message lists the columns of the file, so do not
  point `--source` at a file whose first line is sensitive.
- **Control characters in a key are printed as they are.** A key that holds an escape sequence or a
  newline can change what the console shows.
- **Row numbers count CSV records.** A blank line or a quoted newline moves them away from the line
  numbers of an editor.
- **A key that starts with `=`, `+`, `-` or `@`** is written to `--out` as it came. Open the file as
  text, or quote it first. A spreadsheet may read it as a formula.
- **`--out` is written in place,** not through a temporary file. A failed write can leave a partial
  file, and the exit code is 2. The verdict is printed above the error.
- **Do not close the pipe early.** A closed stdout, as in `| head`, ends the run with exit 70 and
  no verdict. The exit status of the pipeline is not the tool's.
- **Both files are read into memory.** One run of 300,000 source rows and 297,000 applied rows took
  about 11 seconds (10.5 to 11.6 over four runs) on a Windows workstation with Python 3.13.2. The
  keys were `K0` to `K299999`, the stamps were 20 seconds apart, and 2,549 rows were stranded. Under
  `cProfile`, 24 of 28 seconds went to `strptime`. That is one machine, so yours will differ. A
  table of tens of millions of rows is not what this is for.
- **The self-test reads the clock.** The assertions that omit `--now` expect `2026-01-05` to be
  stale, so they fail when the system date is before 2026-01-07.
- **There is no `--fix`.** The tool names a safe mark. Writing it to the watermark table is an
  edit somebody signs for.
- **No line is unreachable.** Coverage is 100 percent of lines and branches. The self-test starts
  one child interpreter, to prove that a key the console cannot encode still prints a verdict.
  Coverage does not trace that child, and the in-process assertions cover the same lines.
- It opens no socket and imports no network module, so no credential can leak from it. The
  self-test reads its own source to assert that.

## Sources

- Esri, [Versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html):
  the adds table "stores all records inserted to or updated in the dataset", and the deletes table
  "records all deletions made in the dataset".
- Esri, [What is a versioned view?](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/what-is-a-versioned-view.htm):
  "The view that is created has the same name as the table or feature class with _evw appended to
  the end", and in it "all the records in the base table are selected and merged with records from
  the delta tables". After `set_current_version`, "you are referencing the state that the specified
  version was referencing when you set the version."
- Esri, [Administer a traditional version](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/recommended-version-administration-workflow.html):
  compress "compresses edits common to all versions back to the business table". That sentence does
  not mention delta tables. The page names them once, in its Analyze Datasets step: the tool updates
  statistics of "business tables, delta tables, and historical archive tables". The link between
  compress and the delta tables is my reading of the pages above.
- Esri, [Compress (Data Management)](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/compress.html):
  "Compresses an enterprise geodatabase by removing states not referenced by a version and
  redundant rows."
- Microsoft, [MAX (Transact-SQL)](https://learn.microsoft.com/en-us/sql/t-sql/functions/max-transact-sql):
  "MAX ignores any null values." and "MAX returns NULL when there's no row to select."
- Microsoft, [Comparison operators](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/comparison-operators-transact-sql):
  "When SET ANSI_NULLS is ON, an operator that has one or two NULL expressions returns UNKNOWN." A
  Boolean expression "filter[s] the rows that qualify" in a `WHERE` clause. That an UNKNOWN row does
  not qualify is my reading of the page.
- Microsoft, [datetime (Transact-SQL)](https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime-transact-sql):
  accuracy "Rounded to increments of .000, .003, or .007 seconds", and time zone offset aware: "No".
- dlt, [lag](https://dlthub.com/docs/general-usage/incremental/lag) and
  [cursor](https://dlthub.com/docs/general-usage/incremental/cursor): the `lag` and
  `on_cursor_value_missing` settings described above.
- dbt, [freshness](https://docs.getdbt.com/reference/resource-properties/freshness): `warn_after`,
  `error_after` and `loaded_at_field` as quoted above, and the note that `dbt source freshness` is
  a legacy command.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [evwlint](https://github.com/uhsear/evwlint) - find the views, procedures and job steps that read
  a versioned base table instead of its `_evw` view. It finds the read that goes stale. This tool
  checks the watermark that a stale read leaves behind.
- [compressfloor](https://github.com/uhsear/compressfloor) - name what holds a geodatabase's
  compress floor. It answers why the base table stopped changing.
- [replicakeep](https://github.com/uhsear/replicakeep) - plan and then repair an idle replica that
  holds the compress floor.
- [safe-republish](https://github.com/uhsear/safe-republish) - the same refusal to write when a
  plausibility check fails, for a truncate-and-append.
