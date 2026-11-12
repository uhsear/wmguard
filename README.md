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
PASS  a job declared to read the view, with a mark behind a frozen base MAX, selects from the view and loses nothing: a note  <-- pinned defect
...
PASS  a base MAX inside the age limit that the view is ahead of is the normal state between compresses: a note, exit 0  <-- pinned defect
...
PASS  an unapplied row inside the window is STRANDED  <-- pinned defect
...
PASS  a row in the window with a blank key is UNDATED, never guessed APPLIED  <-- pinned defect
...
PASS  an unapplied row above the new mark (PENDING) also caps the safe mark: it never rises past a row the next run still needs  <-- pinned defect
...
PASS  a mark past every source stamp is AHEAD even when nothing is stranded, and it exits 1  <-- pinned defect
...
PASS  a mark poisoned by one future-dated row reads FUTURE on the watermark, the base MAX and the view MAX, and never FRESH  <-- pinned defect
...
PASS  a source with no readable stamp compared nothing: exit 4, not clean  <-- pinned defect
...
PASS  rows all at or below the old mark are BEHIND, not compared: exit 4, no SAFE finding  <-- pinned defect
...
PASS  an unapplied row at or below the old mark is a finding, not a clean exit 0 over a row the tool could not judge  <-- pinned defect
...
PASS  an applied file with rows and none readable compares nothing, instead of reporting every row STRANDED  <-- pinned defect
...
PASS  without --apply nothing is written  <-- pinned defect
...
PASS  every key that starts with = + - @ comes out with a guard apostrophe in --out, and a plain key does not  <-- pinned defect
...
PASS  --out read back as --applied is refused with exit 64, because it lists the stranded rows with their source stamps and would read SAFE  <-- pinned defect
...
PASS  an applied export that a spreadsheet guarded with an apostrophe still matches the source key, and the rows it does not list stay STRANDED  <-- pinned defect
...
PASS  --out naming any other existing file is refused, not overwritten  <-- pinned defect
...
PASS  the same file as --source and --applied is refused with exit 64, not read as every row applied and SAFE  <-- pinned defect
...
PASS  a credential file given by mistake never reaches stderr or stdout  <-- pinned defect
...
PASS  a Windows-1252 export whose two keys would both decode to U+FFFD is refused and never reads SAFE  <-- pinned defect
...
PASS  a pipe that dies at the flush after a short report exits 70 and points stdout at the null device, so the exit flush cannot turn it into 120  <-- pinned defect
...
PASS  a source whose every row is at or below the old mark judged nothing: exit 4 and no SAFE line, not exit 0  <-- pinned defect
...
PASS  and a zero value counts as given: 0 is not False  <-- pinned defect
...
PASS  --ap, a prefix of a write flag, is refused and writes nothing  <-- pinned defect
...
PASS  the module imports no network library, so it has no connection to leak a credential from  <-- pinned defect
...
--------------------------------------------------------------------
245 assertions, 0 failed
```

The full run prints all 245 assertions. Each `...` line above is where this block is cut.

## What already exists

Data-loading libraries know the problem and handle it inside their own state. dlt's incremental
cursor has a `lag` setting, which, in the documentation's words, "ensures that a defined period of
data remains refreshed". Its
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
procedure. I found no library that guards that mark. The tool compares the stored mark with two things the
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

The self-test prints the same 245 assertions on Windows (Python 3.13.2 and 3.12.10)
and on Ubuntu (Python 3.9.25 and 3.12.3). The outputs are identical line for line, once the
carriage returns that Windows adds are removed. `coverage run --branch wmguard.py --self-test`
reports 100 percent of lines and branches (1037 statements, 176 branches).

```
git clone https://github.com/uhsear/wmguard.git
```

## Usage

Pick a mode first. Then name the numbers that mode needs.

```
python wmguard.py --self-test
python wmguard.py frozenmark --watermark W --base-max B --view-max V --job-reads view
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
| `--job-reads` | frozenmark | `view` or `base`: the table the job selects from. Needed to judge a mark above a stale base MAX (`READER` without it). |
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
| `--self-test` | none | Run the assertions and exit. Other valid flags are ignored. A flag the parser refuses still exits 64 and runs no assertion. |

A flag that belongs to the other mode is refused with exit 64, so a run never looks defended by a
flag it did not read. A zero value counts as given: `frozenmark --sample 0` and
`safemark --max-age-hours 0` are refused. A flag given an empty value, such as `--source ""`,
`--out ""` or `--key-field ""`, is refused with exit 64 too, and is never read as "not given".

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

$ python wmguard.py frozenmark --watermark "2099-01-01 00:00:00" --base-max "2099-01-01 00:00:00" --now "2026-03-01 09:00:00"
wmguard frozenmark: watermark 2099-01-01 00:00:00, base MAX 2099-01-01 00:00:00, view MAX not given, now 2026-03-01 09:00:00, limit 48.0 h
FUTURE    the watermark 2099-01-01 00:00:00 is 638487.0 h after now 2026-03-01 09:00:00, over the 12.0 h allowed for zone skew. Either a row carries a wrong date, and a mark taken from it skips every later row, or --now is wrong. The tool cannot tell which, so it does not call this fresh.
FUTURE    the base MAX 2099-01-01 00:00:00 is 638487.0 h after now 2026-03-01 09:00:00, over the 12.0 h allowed for zone skew. Either a row carries a wrong date, and a mark taken from it skips every later row, or --now is wrong. The tool cannot tell which, so it does not call this fresh.
VERDICT: 2 finding(s). Do not trust this watermark.
exit 1
```

Three more runs show the reader question. A mark above a frozen base MAX means opposite things
for the two kinds of job, and the tool cannot see which table the job reads. With no `--job-reads`
it says so and exits 1. The same numbers, with the answer given:

```
$ python wmguard.py frozenmark --watermark "2026-03-01 08:20:00" --base-max "2026-01-05 00:00:00" --view-max "2026-03-01 08:30:00" --now "2026-03-01 09:00:00"
wmguard frozenmark: watermark 2026-03-01 08:20:00, base MAX 2026-01-05 00:00:00, view MAX 2026-03-01 08:30:00, now 2026-03-01 09:00:00, limit 48.0 h
READER    the view MAX 2026-03-01 08:30:00 is 1328.5 h ahead of the base MAX 2026-01-05 00:00:00, and the base MAX is 1329.0 h old, over the limit of 48.0 h. The watermark is above the base MAX. A job that reads the view is fine, and a job that reads the base table selects nothing and skips rows after the next compress. The tool cannot see which table the job reads, so it does not call this clean. Pass --job-reads view or --job-reads base.
VERDICT: 1 finding(s). Do not trust this watermark.
exit 1
```

With `--job-reads view` the same numbers print a `BASE_LAGS` note and exit 0. With
`--job-reads base` they print `FROZEN`, because a job that reads the base table selects nothing
above a mark that sits above the base MAX, and exit 1.

When no `--view-max` is given and the base MAX is inside the age limit, the tool cannot look for
a freeze that began less than that long ago. It exits 0 and says what it did not do:

```
$ python wmguard.py frozenmark --watermark "2026-03-01 00:00:00" --base-max "2026-03-01 00:00:00" --now "2026-03-01 20:00:00"
wmguard frozenmark: watermark 2026-03-01 00:00:00, base MAX 2026-03-01 00:00:00, view MAX not given, now 2026-03-01 20:00:00, limit 48.0 h
FRESH     the base MAX is within 48.0 h of now and the watermark is not past it. Without --view-max a table that froze less than that long ago looks the same, so the tool could not look for a freeze.
VERDICT: the base MAX is inside the age limit and the mark is not past it. No --view-max was given, so a freeze younger than the limit was not looked for.
exit 0
```

The five runs above show the cases that matter. The first run has a view that is 1328.5 hours ahead of
the base, a base MAX over the age limit, and a mark equal to the base. The second run has an old
base that the view does not pass, so the table is quiet and not frozen. The third run has no view.
An old base MAX then looks the same whether the table is frozen or idle, so the tool says that and
exits 1. In the fourth run the view is ahead by 20 minutes and the base is recent. A versioned
table is in that state between compresses, so it is a note and the exit code is 0. In the fifth run
one row with a typo year (2099) has set the base MAX, and the job stored it as the mark. Every later
row is stamped below that mark, so none is ever selected. The age check cannot see this, because a
stamp in the future is never old. The tool compares each stamp with `--now` and exits 1.

| Code | Finding? | When |
|---|---|---|
| `FROZEN` | yes, exit 1 | The view MAX is ahead of the base MAX, the base MAX is older than `--max-age-hours`, and the mark is at or behind the base MAX, unless `--job-reads view` says the job reads the view. Also when the mark is above the base MAX and `--job-reads base` says the job reads the base table. |
| `READER` | yes, exit 1 | The view MAX is ahead, the base MAX is older than the limit, the mark is above the base MAX, and `--job-reads` was not given. The tool cannot see which table the job reads. |
| `STALE` | yes, exit 1 | No view was given, and the base MAX is older than `--max-age-hours`. |
| `FUTURE` | yes, exit 1 | The mark, the base MAX or the view MAX is more than 12 hours after `--now`. The tolerance is `FUTURE_TOLERANCE_HOURS` in the file; it allows for a database clock in another zone. One finding per stamp. |
| `AHEAD` | yes, exit 1 | The mark is past every stamp the source has. A row stamped at or below it that arrives later is never selected. |
| `BASE_LAGS` | note | The view MAX is ahead of the base MAX, but the base MAX is within the age limit, or the base MAX is stale and `--job-reads view` says the job reads the view, whether the mark is above the base MAX or at or behind it. |
| `IDLE` | note | The base MAX is old, but the view is not ahead of it. The table is quiet. |
| `FRESH` | note | The base MAX is recent, no stamp is more than 12 hours after `--now`, and the mark is not past the source. Without `--view-max` it also says a freeze younger than the limit was not looked for. |

`FROZEN` means that the base table lags its view now and has done so for longer than the limit,
and that the mark sits where a job reading the base table would put it. One snapshot cannot show
that a table stopped moving. A blocked compress that is younger than `--max-age-hours` shows up as
`BASE_LAGS` until it ages past the limit. A job that reads the view keeps a mark above the base
MAX, so `--job-reads view` keeps it from being flagged. A job that reads the view also selects the
rows above a mark that sits at or behind the base MAX, so `--job-reads view` makes that a note
too. Without `--job-reads`, a mark above the base MAX is `READER`, a finding, because a job that
reads the base table selects nothing above it.

The age limit is inclusive. A base MAX exactly at the limit is not stale, and one second over is.
A stamp more than 12 hours after `--now` is `FUTURE`, a finding, and never `FRESH`. A stamp up to 12 hours after `--now` (the limit is inclusive) is treated as zone skew. A view MAX behind the base MAX is not freeze
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

A key whose applied stamp is later than every source stamp of that key is `LATER`, a finding.
An applied copy stored five hours late, because the serving table keeps UTC or a load time,
would otherwise let an older, unapplied row of that key read `APPLIED`. The tool cannot tell
that from an applied file that was exported after a newer edit, so it exits 1 for both. Synthetic
files, one key shifted by 3.5 hours:

```
$ cat source.csv
KEY,STAMP
C,2026-03-01 02:30:00
E,2026-03-01 03:30:00
$ cat applied.csv
KEY,STAMP
C,2026-03-01 06:00:00
E,2026-03-01 03:30:00
$ python wmguard.py safemark --source source.csv --applied applied.csv --old-watermark "2026-03-01 00:00:00" --new-watermark "2026-03-01 03:00:00"
wmguard safemark: source.csv 2 row(s), applied.csv 2 row(s)
window: stamp after 2026-03-01 00:00:00, new watermark 2026-03-01 03:00:00
APPLIED 2, STRANDED 0, PENDING 0, BEHIND 0, UNDATED 0
LATER     1 key(s) read APPLIED only because the applied stamp is later than every source stamp of that key. A load time, or a stamp in another time zone, looks like this, and so does an applied file exported after a newer edit. The tool cannot tell, and an older copy of the row may be the one that was applied. Export the applied stamp as the source stamp.
safe watermark: 2026-03-01 03:30:00
VERDICT: 1 finding(s). Do not trust this watermark.
exit 1
```

The safe mark on that run is not trustworthy, because it counts the shifted key as applied.

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
OUTSIDE   1 unapplied row(s) have a stamp at or below the old watermark, so this run does not judge them. An earlier run may have skipped them, or they were never meant to be applied, and the tool cannot tell which. Pass an early --old-watermark, such as 1900-01-01, to judge the whole history.
STRANDED rows:
  row 2  key P-102  stamp 2026-03-01 01:40:00
  row 4  key P-104  stamp 2026-03-01 02:05:00
UNDATED rows:
  row 7  key P-107  stamp none
safe watermark: 2026-03-01 01:10:00
VERDICT: 3 finding(s). Do not trust this watermark.
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
  the applied file either, so the `OUTSIDE` finding counts it and the run exits 1. The tool cannot
  tell a row that an earlier run skipped from a row that was never meant to be applied, so it does
  not report a clean run over it.
- The verdict says "no skipped row found above the old watermark" only when it finds no finding,
  and no unapplied row sits below the old mark. A note alone, such as `REGRESSED`, does not stop
  it. It does not claim to have judged rows below the old mark.

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
OUTSIDE   1 unapplied row(s) have a stamp at or below the old watermark, so this run does not judge them. An earlier run may have skipped them, or they were never meant to be applied, and the tool cannot tell which. Pass an early --old-watermark, such as 1900-01-01, to judge the whole history.
UNDATED rows:
  row 7  key P-107  stamp none
safe watermark: 2026-03-01 01:10:00
VERDICT: 2 finding(s). Do not trust this watermark.
exit 1
```

The stranded rows are gone. `P-102` and `P-104` are now `PENDING`, so the next run selects them.
The `UNDATED` finding stays, because no mark fixes a row with no stamp. The `OUTSIDE` finding
stays too, because `P-090` is still below the old mark and still not applied. Either apply it, or
accept the loss and give the next run an old mark above it.

`--out rows.csv --apply` writes one line per source row. Without `--apply`, the same run prints
"Check only" and writes nothing. This is the file that the first run above writes when you add
`--out rows.csv --apply` to it (new mark `2026-03-01 06:00:00`, so `P-102` and `P-104` are
`STRANDED`):

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
| `OUTSIDE` | yes, exit 1 | At least one row at or below the old mark is not in the applied file. This run does not judge it, and it cannot tell a skipped row from one never meant to be applied. |
| `SAFE` | note | None of the findings or notes above, and at least one row was judged in the window (not `BEHIND`, not `UNDATED`). |

A row that was applied and then edited is `STRANDED` or `PENDING` at its new stamp, because the
applied file shows the older stamp. A key that appears twice in the applied file keeps its newest
stamp.

A blank key never matches. A source row above the old mark with a blank key is `UNDATED`. An applied
row with a blank key is counted as unreadable and left out. Without that rule, one blank-key applied
row would mark every blank-key source row as applied, and the run would exit 0.

A spreadsheet runs a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return as a
formula. OWASP lists those six characters, a line feed, and the full-width forms of `=`, `+`, `-`
and `@`, and recommends to "Prepend each cell field with a single quote"
([CSV Injection](https://community.owasp.org/attacks/CSV_Injection)). The `key` column of
`--out` comes from the source file, so `wmguard` puts one apostrophe in front of a key that starts
with `=`, `+`, `-` or `@`. A tab or CR at the start of a key is trimmed away on read, so it never
reaches the guard through the CLI (see Limits). It does not guard the line feed or the full-width forms (see Limits). OWASP
also warns that these techniques "are not reliable in Microsoft Excel after saving and
re-opening the CSV file", so this is a guard and not a proof.
`=HYPERLINK("http://x.invalid","y")` is written as `'=HYPERLINK(...)`. Row numbers stay numbers.
The reader removes one guard apostrophe from the key column of `--source` and `--applied`, so an
applied export that a spreadsheet saved with the apostrophe still matches the source key. The
tool refuses its own `--out` file as `--source` or `--applied`, with exit 64. The file lists every
source row with its source stamp, including the `STRANDED` and `PENDING` rows that were never
applied. Read as an applied file, each of those rows looked applied, the safe mark jumped to the
newest stamp, and the run exited 0. The refusal tests the header: a file whose columns are exactly
`row`, `key`, `stamp` and `class`, in that order, is the tool's own output. The self-test pins both
sides: the spreadsheet-guarded applied file keeps the unlisted rows `STRANDED`, and the `--out`
file is refused as `--applied` and as `--source`.

If the applied file has rows and none has a readable key and stamp, the tool compares nothing and
exits 4. A date format it does not read, such as `3/1/2026 1:10:00 AM`, would otherwise make every
source row look unapplied, and a scheduled job could not tell that from a real finding.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing found. Notes may still print. |
| 1 | At least one finding. |
| 2 | A file could not be read or is not UTF-8 text, or `--out` could not be written. A UTF-16 file is the exception and exits 64; see Limits. |
| 4 | Nothing was compared: no source row could be judged (each is `BEHIND`, `UNDATED` or absent), or no applied row was readable. |
| 64 | Usage error: a bad flag, a bad stamp, a wrong-mode flag, an empty flag value, an existing `--out`, `--source` and `--applied` naming one file, or a column the file lacks. |
| 70 | An unexpected crash in the tool. It is never a finding about the data. |

Exit 4 is not exit 0 on purpose. A source that was exported with the wrong column, with every
stamp blank, or with every row at or below the old mark, would otherwise report a clean run. A scheduled job reads 0 as "safe".

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
  the `OUTSIDE` finding only counts the ones that are missing from the applied file.
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
  a load time earlier than the source stamp reads as `STRANDED`. A stamp later than every source
  stamp of its key, from a load time or a shifted time zone, is the `LATER` finding. The tool
  cannot see an offset that keeps the applied stamp at or below the newest source stamp of the
  key, so a shift smaller than the gap between two edits of one key can still read `APPLIED`.
  Copy the source stamp into the serving row when you apply it.
- **A view MAX can be stale too.** A session that set a version keeps reading that version's state
  (see Sources). Take the view MAX from a session that set none.
- **`STALE` cannot tell frozen from idle.** Without `--view-max`, a quiet table over a long
  weekend looks the same as a frozen one. Give `--view-max`, or set `--max-age-hours` from your
  own edit rate. `48` is a guess.
- **`--now` defaults to this machine's clock.** The stamps have no time zone, so the clock and the
  stamps must use the same zone. Pass `--now` from the database server's clock when they differ.
  A stamp up to 12 hours after `--now` is allowed as zone skew, so a typo date that is less than
  that far ahead is not caught as `FUTURE`. The 12 hours is a guess, set in the file.
- **A duplicate key in the source** is judged row by row. Each row is `APPLIED` when its key's
  newest applied stamp is the same or newer.
- **Keys are compared as trimmed text, case-sensitively.** `P-1` and `p-1` are two keys, and so
  are `7` and `07`. SQL Server default collations are not case-sensitive, and a spreadsheet can
  drop leading zeros (`00123` to `123`) or change case. The result is a false `STRANDED`, which
  exits 1, the safe direction.
- **Only comma-separated UTF-8 CSV is read.** UTF-16, tab-delimited and semicolon-delimited files
  exit 64. A UTF-16 file gets its own message. A tab or semicolon file gets "columns not in the
  CSV" and a count of one column, which shows the problem. A file with a byte that is not UTF-8,
  such as a Windows-1252 export with accented names, exits 2 and names the file, the byte and its
  offset. Nothing is decoded lossily, so a decoding step never merges two keys. Trimming and the
  guard apostrophe can (see below). A BOM at the start of a UTF-8 file is removed. A NUL
  byte inside a file gives exit 2 on Python 3.9 and 3.10, where the csv module refuses it, and is
  read as part of the text on Python 3.11 and later. That was measured on 3.9, 3.10, 3.11 and 3.12,
  and the cause in the csv module was not checked against the Python changelog. A field over csv's default limit of 131072
  characters gives exit 2 on every version. Its message does not name the file, so with two
  inputs you cannot tell from the message which one it was.
- **An empty file is a usage error.** A zero-byte `--source` or `--applied` exits 64 with "columns
  not in the CSV", and not 2.
- **A header-only source** has no rows to compare. It exits 4 and prints no `SAFE` line.
- **A wrong column name never echoes the header.** The message names the columns you asked for and
  counts the columns the file has, so a file given by mistake, such as one that holds a token in
  its first line, does not reach the log. A count of 1 usually means the file is not
  comma-separated. The self-test pins that a credential-shaped first line is not printed.
- **Control characters in a key are printed as they are.** A key that holds an escape sequence or a
  newline can change what the console shows.
- **Row numbers count CSV records.** A blank line or a quoted newline moves them away from the line
  numbers of an editor.
- **A guarded key is read back as the key.** `--out` writes a key that starts with `=`, `+`, `-`,
  `@`, a tab or a CR with one apostrophe in front, and the reader removes exactly one such
  apostrophe from the key column of `--source` and `--applied`. A source key that is already
  written as an apostrophe followed by one of those characters, such as `'=x`, is therefore read
  as `=x` and written as `'=x`. That is the one key the file does not copy byte for byte, so the
  guard is lossless for every key except one that already starts with an apostrophe followed by
  one of those characters. Keys are
  trimmed before they are written, so a tab or CR at the start of a key never reaches the guard
  through the CLI. The guard still covers both, and the self-test calls it with both. The guard covers
  these six characters only, and only after the key is trimmed. Two source
  keys that differ only by that apostrophe, such as `=x` and `'=x`, are read as one key and share
  one verdict. The other
  three columns are not guarded: the row number is an integer, the stamp comes from a parsed
  timestamp, and the class is one of five fixed words. Stdout is not a CSV and is not guarded.
- **Keys that differ only by leading whitespace or one guard apostrophe are read as one key.** A
  source key written as tab, CR or line feed followed by `=x`, or as `'=x`, reduces to `=x` and
  shares one verdict with the key `=x`. A source key that starts with an apostrophe before `=`, `+`,
  `-`, `@`, a tab or a CR can therefore match an applied key that differs only by that apostrophe.
- **A key that starts with a tab or a CR is trimmed before it is written,** so the tab and CR guard
  is exercised only by calling the function directly.
- **An applied row whose stamp cannot be read is counted as not applied.** If its source row sits
  above the new mark, the run can exit 0 with a `SAFE` line and a note that names the count. A
  row at or below the new mark that is not applied becomes `STRANDED`, which exits 1.
- **A two-column file given by mistake can print a value.** A `STRANDED` sample line prints the
  key column, so a file whose key column holds a token puts that token on stdout. The self-test
  pins only the case where the first line is the wrong header.
- **A dry run does not test the `--out` directory.** A missing or read-only directory fails only
  with `--apply`, as exit 2. A disk that fills during `--apply` leaves a partial `--out` file;
  delete it before you run again.
- **A readable file with the wrong columns exits 64, not 2.** A scheduler cannot tell a wrong flag
  from a wrong-format file by the exit code alone.
- **Some lead characters are not guarded.** A line feed is trimmed away before it can reach the
  guard. The full-width forms of `=`, `+`, `-` and `@` are written as they are. A zero-width space or
  a byte-order mark in front of `=` hides the formula character from the guard, so that key is
  written unguarded. A leading no-break space is removed by the trim, so that key is guarded and
  written without the space, and not byte for byte.
- **A safemark run with an empty window exits 4.** If every source row is at or below
  `--old-watermark`, the run judged no row. It prints `BEHIND n` and "nothing was compared", never
  a `SAFE` line. A sync that really had nothing new to apply therefore exits 4 too. The tool
  cannot tell an idle sync from a wrong old mark. A scheduler should treat 4 as "check", not as
  failure of the job.
- **`LATER` is a guess in the safe direction.** An applied file that was exported after a newer
  edit of the key has an applied stamp later than the source stamp and exits 1 too. Export the
  source after the sync, so the two files come from one moment.
- **The safe mark cannot account for a row with an unreadable stamp.** Such a row has no place on
  the time line, so the printed safe mark ignores it. The `UNDATED` finding still exits 1. A
  blank-key row above the old mark does hold the safe mark below its own stamp.
- **Keys and paths are printed as they are.** A key can hold a terminal escape sequence. The
  traceback printed on exit 70 names the path of the script and prints the source line of each
  frame. It names a file given on the command line only when the exception message does.
- **The GitHub search above cannot be repeated from this repository.** It is a dated claim.
- **`--out` is written in place,** not through a temporary file. A failed write can leave a partial
  file, and the exit code is 2. The verdict is printed above the error. Delete the partial file
  before you run again, because `--out` refuses a path that exists.
- **Do not close the pipe early.** On Windows 11 with Python 3.13.2 under Git Bash, `safemark ... --sample 3000 | head -1`
  printed the first line and the run exited 70. A closed stdout, as in `| head`, ends the run with exit 70 and
  the verdict may not be delivered. The tool points stdout at the null device after the failure, so
  Python cannot fail again at exit and replace the 70 with its own 120. When the reader took the
  whole short report before it closed, the run keeps its normal code. The exit status of a
  pipeline is the status of its last command, unless `pipefail` is set.
- **A job that runs with stdout closed** still returns the right exit code, but nothing is printed.
  Read the exit code and not the log.
- **Ctrl+C is not handled.** An interrupt ends the run with Python's own status and a traceback,
  and not with a code from the table above. That is a stop by the operator and not a finding.
- **Both files are read into memory.** One run of 300,000 source rows and 297,000 applied rows took
  about 11 seconds (10.5 to 11.6 over four runs) on a Windows workstation with Python 3.13.2.
  The files come from this generator, which you can run:

  ```python
  import datetime
  t0 = datetime.datetime(2026, 1, 1)
  with open("bench-src.csv", "w", newline="") as src, \
          open("bench-app.csv", "w", newline="") as app:
      src.write("KEY,STAMP\r\n")
      app.write("KEY,STAMP\r\n")
      for i in range(300000):
          line = "K%d,%s\r\n" % (i, (t0 + datetime.timedelta(seconds=20 * i))
                                  .strftime("%Y-%m-%d %H:%M:%S"))
          src.write(line)
          if i % 100 != 7:       # every 100th row is left unapplied
              app.write(line)
  ```

  `python wmguard.py safemark --source bench-src.csv --applied bench-app.csv --old-watermark
  1900-01-01 --new-watermark "2026-03-01 00:00:00"` then prints `APPLIED 297000, STRANDED 2549,
  PENDING 451` and exits 1. Under `cProfile`, 24 of 28 seconds went to `strptime`. That is one
  machine, so yours will differ. A table of tens of millions of rows is not what this is for.
- **The self-test reads the clock.** The assertions that omit `--now` expect `2026-01-05` to be
  stale, so they fail when the system date is before 2026-01-07.
- **Keys that differ only by one leading guard apostrophe can give a wrong verdict.** `=x` and
  `'=x` are read as one key. An unapplied source row `=x` therefore reads `APPLIED` when only `'=x`
  was applied with the same stamp, and the run can exit 0 with a `SAFE` line. If the applied stamp
  is later than the source stamp, the `LATER` finding fires and the run exits 1. Keys of that
  shape are rare.
- **No path writes a file and reads it back.** `wmguard` refuses its own `--out` file with exit 64,
  so the strip-on-read of the guard apostrophe is pinned through `uncell(cell(v)) == v` and
  through an applied export that a spreadsheet guarded, and not through a file written then read.
- **Stamps without seconds and `datetime2(7)` values are `UNDATED`.** `2026-03-01 01:00` and a
  value with seven fraction digits are not read. A file of only such stamps exits 4 with "nothing
  was compared". The stamp tick of 3 ms for `datetime` is an approximation: it rounds to .000,
  .003 and .007 seconds, so the spacing is not even.
- **`IDLE` assumes every edit is stamped above the base MAX.** A clock that steps back, or a
  back-dated bulk load, can freeze a table while the view MAX is not ahead of the base MAX. The
  run then exits 0.
- **Name two different files for `--source` and `--applied`.** The same file given twice, also as a
  hard link or under another spelling of the path, exits 64 with "name the same file". A copy of
  the source under another name is a different file. The tool reads it as an applied export, marks
  every row `APPLIED` and exits 0.
- **A sentinel stamp such as `9999-12-31` on a source row is read as a real stamp.** The row stays
  `PENDING` and `safemark` raises no `FUTURE` finding. `FUTURE` is judged only in `frozenmark`.
- **A zero-byte `--applied` file exits 64, not a verdict.** A sync that applied nothing needs a
  header-only applied file to get a verdict.
- **A file that does not name itself in a csv error.** A NUL byte or a BOM-less UTF-16 file on
  Python 3.9 and 3.10, and a field over csv's limit on every version, exit 2 with a message that
  does not name the file.
- **A closed pipe is blamed on the tool.** In `safemark ... | head -1`, the cause is the reader.
  The exit 70 is accurate and the printed "fault in the tool" text misattributes it.
- **There is no `--fix`.** The tool names a safe mark. Writing it to the watermark table is an
  edit somebody signs for.
- **No line is unreachable.** Coverage is 100 percent of lines and branches. The self-test starts
  two child interpreters. One proves that a key the console cannot encode still prints a verdict.
  The other proves that a closed stdout pipe exits 70. Coverage does not trace the children, and
  the in-process assertions cover the same lines.
- **A UTF-16 file without a byte-order mark** exits 2 on Python 3.9 and 3.10, where the csv module
  refuses its NUL bytes, and exits 64 on 3.11 and later. A UTF-16 file with a mark exits 64 on
  every version.
- **The printed safe mark can be later than `--new-watermark`.** It is the newest applied stamp
  below the oldest unapplied row, and it does not look at the mark you wrote. With an applied row at
  05:00, a new mark of 03:00 and nothing stranded, the line reads `safe watermark: 05:00` and the
  verdict is `SAFE`. The verdict is right: any mark at or below the safe mark skips nothing.
- **Terminal output is lossy.** Stdout and stderr replace a character the console cannot encode
  with `?`. Two keys that differ only in such characters print the same in the log. The CSV and the
  verdict are not affected.
- **The `--out` file has the default permissions** of the process (the umask, or the ACL the folder
  passes down). It holds source keys, so write it to a folder only you can read.
- **Another spreadsheet may show the apostrophe.** The guard is the OWASP single-quote advice, which
  OWASP aims at Excel. A different program may display the apostrophe as part of the key.
- **Only the exact header of `--out` is recognised as the tool's own file.** If you rename a column
  or add one, the file is read as ordinary rows, and the hidden-stranded-rows problem above returns.
- **A source or applied export whose columns are exactly `row`, `key`, `stamp` and `class` is taken
  for wmguard's own `--out` file** and refused with exit 64; rename a column.
- **A mark taken from the `_evw` view needs `--view-max`.** Without it, `frozenmark` compares the
  mark with the base MAX only, and a mark that is ahead of the base table reads as `AHEAD`
  (exit 1). Give `--view-max` and `--job-reads view` for a job that reads the view.
- **`--job-reads` is ignored unless it is needed.** Given with a case that is not stale, or without `--view-max`, it changes nothing and prints no warning.
- **`FROZEN` can be a conservative false alarm without `--job-reads`.** A job that reads the view, with a mark at or behind a frozen base MAX while the view is ahead, is flagged with exit 1, because the tool cannot see which table the job reads. Give `--job-reads view` and the same numbers are a `BASE_LAGS` note with exit 0.
- **The `frozenmark` error for a stamp with seven fraction digits** says "the fraction is optional" and does not say that at most six digits are read.
- **The column name `DateUpdate` in the opening story and the SQL block** is a generic English name for a stamp column. Rename it to `LastEdited` in your own queries if a shorter, unique name suits you better.
- **Keys that begin with a minus sign,** including negative integers such as `-3`, are written to
  `--out` with a guard apostrophe. The reader removes it from `--source` and `--applied`, and a
  spreadsheet shows the apostrophe.
- **`--out` is written with CRLF line endings,** the default of Python's csv module, on every
  platform.
- **A red self-test exits 1,** the same code as a finding. Read the `0 failed` footer, not only the
  exit code, to tell them apart.
- **A `--out` path that names a Windows device.** On Windows 11 with Python 3.13, `--out aux.csv
  --apply` wrote an ordinary file named `aux.csv`, and `--out nul` was refused as an existing path
  (exit 64). Other Windows versions were not probed. Microsoft's
  [Naming Files, Paths, and Namespaces](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file)
  lists `CON`, `PRN`, `AUX` and `NUL` as reserved names and says to avoid them "followed immediately
  by an extension", so `aux.csv` is a name that page tells you not to use, even though it was
  written here. Do not rely on either result.
- **The seven-digit precision of `datetime2(7)`** comes from the Microsoft page in Sources, which
  lists "0 to 7 digits" and says the type is not time zone offset aware.
- **Excel may drop the apostrophe** when it saves the file again. OWASP also describes a tab prefix
  inside the quoted field as an alternative. The tool does not use it, because it would
  change the key data.
- **The guard covers `=`, `+`, `-`, `@`, tab and CR only.** A key that starts with a percent sign,
  a pipe or a zero-width character is written as it is.
- **A repeated column name in a header** is not refused. The reader keeps the value of the last
  column with that name, so `KEY,KEY,STAMP` reads the second column as the key.
- **A wrong stamp flag is echoed.** The message for a bad `--old-watermark` and the other stamp
  flags repeats the text you typed, so do not type a secret into one.
- **Keys are printed to the log.** The `STRANDED` and `UNDATED` sample lines print source keys. Point
  `--source` and `--applied` only at data you accept to see in the log. A byte that is not UTF-8 is
  named with its offset in the exit 2 message, so one byte of a wrongly supplied file can reach
  stderr.
- **The self-test writes files** under a fresh temporary directory and starts two child
  interpreters. A finally block removes the files. **A killed self-test can leave a directory**
  named `wmguard-selftest-*` in the temp folder. Delete any that remain.
- **The compress-to-delta-table behaviour in the opening** is an inference from the Esri pages in
  Sources, not a quoted statement.
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
- Microsoft, [datetime2 (Transact-SQL)](https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime2-transact-sql):
  precision "0 to 7 digits, with an accuracy of 100 nanoseconds (100 ns)", and "Time zone offset
  aware and preservation": "No".
- Microsoft, [datetime (Transact-SQL)](https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime-transact-sql):
  accuracy "Rounded to increments of .000, .003, or .007 seconds", and time zone offset aware: "No".
- OWASP, [CSV Injection](https://community.owasp.org/attacks/CSV_Injection): the characters that
  start a dangerous cell (`=`, `+`, `-`, `@`, tab, carriage return, line feed, and the full-width
  forms of `=`, `+`, `-` and `@`), the recommendation to prepend a single quote, and the note that
  Excel "may remove quotes or escape characters from CSV cells when a file is saved and re-opened".
  The tool does not open a spreadsheet, so the effect of the apostrophe in Excel was not run here.
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
