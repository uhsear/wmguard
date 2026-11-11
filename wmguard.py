#!/usr/bin/env python
"""Flag a sync watermark taken from a frozen table, or advanced past rows nobody applied.

A nightly sync keeps a high-water mark and selects the source rows stamped after
it. Two things break that, and neither raises an error.

frozenmark: the mark is read from MAX(stamp) of a table that stopped moving. On a
traditionally versioned table the base table changes only when a compress moves
the delta rows into it, so a blocked compress freezes MAX(stamp) while the _evw
view keeps growing. The mark freezes with it, the job selects nothing, and "zero
pending" is true of a table that has stopped telling the truth. A base MAX that
lags its view by a little is normal between compresses, so the tool flags
FROZEN only when the base MAX is also older than --max-age-hours.

safemark: the mark is advanced to a value that is not safe. Selection is strictly
stamp > mark, so every unapplied row stamped at or below the new mark is gone for
good, and a row with no stamp was never selectable at all.

    python wmguard.py --self-test
    python wmguard.py frozenmark --watermark "2026-01-05 00:00:00" --base-max "2026-01-05 00:00:00" --view-max "2026-03-01 08:30:00"
    python wmguard.py frozenmark --watermark W --base-max B --now N --max-age-hours 48
    python wmguard.py safemark --source source.csv --applied applied.csv --old-watermark W --new-watermark W2
    python wmguard.py safemark --source s.csv --applied a.csv --old-watermark W --new-watermark W2 --out rows.csv --apply

Reads timestamps and CSV files. Opens no connection to anything. The only thing
it writes is the --out CSV, and only with --apply.

Exit codes: 0 nothing found, 1 at least one finding, 2 a file could not be read
or written, 4 nothing was compared (no source row could be judged, or no applied
row was readable), 64 usage error, 70 an unexpected crash, which is never a
finding.
"""

from __future__ import print_function

import argparse
import ast
import collections
import contextlib
import csv
import datetime
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# A source whose newest row is older than this is suspect. 48 covers a table
# that is quiet over a weekend. It is a guess about how often your table is
# edited, not a statement about any table, so set --max-age-hours from yours.
DEFAULT_MAX_AGE_HOURS = 48.0

# Upper bound for --max-age-hours: about 114 years, far below what
# datetime.timedelta can hold, so an absurd value is a usage error and not a crash.
MAX_AGE_LIMIT_HOURS = 1000000.0

DEFAULT_SAMPLE = 10

DEFAULT_KEY_FIELD = "KEY"
DEFAULT_STAMP_FIELD = "STAMP"

# Tried in order. No format carries a time zone, because SQL Server datetime
# does not (see the README).
STAMP_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f",
                 "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")

MODES = ("frozenmark", "safemark")

# Row classes of safemark.
APPLIED = "APPLIED"
STRANDED = "STRANDED"
PENDING = "PENDING"
BEHIND = "BEHIND"
UNDATED = "UNDATED"
CLASSES = (APPLIED, STRANDED, PENDING, BEHIND, UNDATED)

Finding = collections.namedtuple("Finding", "code is_finding message")
SourceRow = collections.namedtuple("SourceRow", "number key stamp")

# The flags that belong to one mode. A flag given to the other mode is refused,
# because a run must not look defended by a flag it never read.
FROZEN_ONLY = ("watermark", "base_max", "view_max", "now", "max_age_hours")
SAFE_ONLY = ("source", "applied", "old_watermark", "new_watermark",
             "key_field", "stamp_field", "applied_key_field",
             "applied_stamp_field", "sample", "out", "apply")
FROZEN_REQUIRED = ("watermark", "base_max")
SAFE_REQUIRED = ("source", "applied", "old_watermark", "new_watermark")


# ----------------------------------------------------------------- decision core

def parse_stamp(text):
    """A naive datetime from text. Raises ValueError for anything else, a time
    zone suffix included: a mark compared across zones is wrong by hours."""
    text = (text or "").strip()
    for fmt in STAMP_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError("%r is not a timestamp. Use YYYY-MM-DD HH:MM:SS with no "
                     "time zone; the fraction is optional." % text)


def fmt_stamp(stamp):
    return stamp.isoformat(" ")


def hours(delta):
    return delta.total_seconds() / 3600.0


def judge_frozen(watermark, base_max, view_max, now, max_age):
    """Findings for a watermark read from a table whose MAX may be frozen.

    base_max is MAX(stamp) of the table the job reads. view_max is the same
    MAX read through the current-state view, or None when it was not given.
    max_age is a timedelta. Pure: no clock, no I/O.

    One snapshot cannot show that a table stopped moving. A versioned base
    table lags its view between compresses, so FROZEN needs three things at
    once: the view is ahead, the base MAX is older than max_age, and the mark
    sits at or behind the base MAX. A mark above the base MAX did not come
    from the base table, so a job that reads the view is not flagged.
    """
    out = []
    stale = now - base_max > max_age
    if view_max is not None and view_max > base_max:
        gap = hours(view_max - base_max)
        ahead = ("the view MAX %s is %.1f h ahead of the base MAX %s"
                 % (fmt_stamp(view_max), gap, fmt_stamp(base_max)))
        if stale and watermark <= base_max:
            if watermark == base_max:
                how = ("The watermark equals the base MAX, so a job that "
                       "reads the base table selects nothing")
            else:
                how = ("The watermark is behind the base MAX, and the base "
                       "table does not hold the rows between its MAX and "
                       "the view MAX")
            out.append(Finding(
                "FROZEN", True,
                "%s, and the base MAX is %.1f h old, over the limit of "
                "%.1f h. %s." % (ahead, hours(now - base_max),
                                 hours(max_age), how)))
        elif stale:
            out.append(Finding(
                "BASE_LAGS", False,
                "%s, and the base MAX is %.1f h old. The watermark is above "
                "the base MAX, so it did not come from the base table. "
                "Anything else that reads the base table is stale."
                % (ahead, hours(now - base_max))))
        else:
            out.append(Finding(
                "BASE_LAGS", False,
                "%s, and the base MAX is within the limit of %.1f h. A "
                "versioned base table lags its view until the next "
                "compress, so this alone is not a freeze."
                % (ahead, hours(max_age))))
    elif stale:
        age = hours(now - base_max)
        if view_max is None:
            out.append(Finding(
                "STALE", True,
                "the base MAX %s is %.1f h old, over the limit of %.1f h. "
                "Without --view-max a frozen table and an idle one look the "
                "same." % (fmt_stamp(base_max), age, hours(max_age))))
        else:
            out.append(Finding(
                "IDLE", False,
                "the base MAX is %.1f h old, and the view MAX is not ahead "
                "of it. The table is quiet, not frozen." % age))
    ceiling = base_max if view_max is None else max(base_max, view_max)
    if watermark > ceiling:
        out.append(Finding(
            "AHEAD", True,
            "the watermark %s is past every stamp in the source (newest "
            "%s). A row that arrives stamped at or below it is never "
            "selected." % (fmt_stamp(watermark), fmt_stamp(ceiling))))
    if not out:
        out.append(Finding(
            "FRESH", False,
            "the base MAX is within %.1f h of now and the watermark is not "
            "past it." % hours(max_age)))
    return out


FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
GUARD_MARK = "'"


def _guarded(value):
    """True when value is a formula lead after any run of guard marks."""
    return value.lstrip(GUARD_MARK)[:1] in FORMULA_LEAD


def cell(value):
    """A text cell for the CSV that no longer starts with a formula character.

    A cell that starts with = + - @ tab or CR gets one apostrophe in front.
    That is the OWASP single-quote advice. Excel may drop the apostrophe when
    the file is saved and opened again, so this is a guard and not a proof.
    A value that already starts with an apostrophe before such a character
    gets one more, so uncell() gives back exactly what went in."""
    if _guarded(value):
        return GUARD_MARK + value
    return value


def uncell(value):
    """The inverse of cell(): removes exactly one guard apostrophe."""
    if value.startswith(GUARD_MARK) and _guarded(value):
        return value[1:]
    return value


def build_applied(records, key_field, stamp_field):
    """(newest applied stamp by key, count of rows with no readable stamp).

    A row with no readable stamp or no key proves nothing was applied, so it is
    counted and left out. A blank key must never match: it would mark every
    blank-key source row applied. A key applied twice keeps its newest stamp.
    """
    applied = {}
    unreadable = 0
    for rec in records:
        key = uncell((rec.get(key_field) or "").strip())
        try:
            stamp = parse_stamp(rec.get(stamp_field))
        except ValueError:
            unreadable += 1
            continue
        if not key:
            unreadable += 1
            continue
        previous = applied.get(key)
        if previous is None or stamp > previous:
            applied[key] = stamp
    return applied, unreadable


def build_source(records, key_field, stamp_field):
    """SourceRow list. A stamp that is blank or unreadable becomes None."""
    rows = []
    for number, rec in enumerate(records, 1):
        key = uncell((rec.get(key_field) or "").strip())
        try:
            stamp = parse_stamp(rec.get(stamp_field))
        except ValueError:
            stamp = None
        rows.append(SourceRow(number, key, stamp))
    return rows


def is_applied(row, applied):
    """True when the key was applied at the row's stamp or a newer one."""
    done = applied.get(row.key)
    return done is not None and done >= row.stamp


def classify_row(row, applied, old, new):
    """One class per source row. The job selects strictly stamp > old, so a row
    is in play only above old, and it is lost when it is unapplied at or below
    new. A row in play with a blank key cannot be matched to an applied row,
    so it is UNDATED and never guessed APPLIED."""
    if row.stamp is None:
        return UNDATED
    if row.stamp <= old:
        return BEHIND
    if not row.key:
        return UNDATED
    if is_applied(row, applied):
        return APPLIED
    if row.stamp <= new:
        return STRANDED
    return PENDING


def safe_watermark(rows, verdicts, old):
    """A mark that skips no unapplied row.

    Selection is strictly greater than the mark, so the mark must sit below the
    oldest unapplied stamp. A row above the old mark with a blank key counts as
    unapplied here, because it cannot be matched. A row with no readable stamp
    has no place on the time line, so no mark accounts for it. The answer is the newest applied stamp under that,
    or old when there is none. Any lower mark is safe too, only slower.
    An applied row that shares the oldest unapplied stamp is selected again
    next run, so the apply step has to be safe to repeat.
    """
    unapplied = [r.stamp for r, v in zip(rows, verdicts)
                 if v in (STRANDED, PENDING)
                 or (v == UNDATED and r.stamp is not None)]
    done = [r.stamp for r, v in zip(rows, verdicts) if v == APPLIED]
    if unapplied:
        floor = min(unapplied)
        done = [s for s in done if s < floor]
    return max(done + [old])


class SafeReport(object):
    """The result of judging one watermark advance.

    why is None, or the reason nothing was compared. applied_unusable is True
    when the applied file had rows and none could be read: every source row
    would look unapplied, and that is a format fault, not a finding.
    """

    def __init__(self, rows, verdicts, old, new, safe, unreadable, findings,
                 applied_unusable=False):
        self.rows = rows
        self.verdicts = verdicts
        self.old = old
        self.new = new
        self.safe = safe
        self.unreadable = unreadable
        self.findings = findings
        self.applied_unusable = applied_unusable
        self.counts = dict((c, verdicts.count(c)) for c in CLASSES)
        self.compared = len(verdicts) - self.counts[UNDATED]
        self.why = None
        if applied_unusable:
            self.compared = 0
            self.why = ("No applied row has a readable key and stamp, so "
                        "every source row would look unapplied. Check the "
                        "date format and the column names of the applied "
                        "file.")
        elif self.compared == 0:
            self.why = ("No source row can be judged: each has a blank key "
                        "or a stamp the tool cannot read, or the source has "
                        "no row.")


def judge_safe(rows, applied, unreadable, old, new):
    """Judge the move of a watermark from old to new. Pure: no clock, no I/O.

    Only rows stamped above old are judged. unreadable is the count of applied
    rows with no readable key and stamp."""
    verdicts = [classify_row(r, applied, old, new) for r in rows]
    safe = safe_watermark(rows, verdicts, old)
    if unreadable and not applied:
        return SafeReport(rows, verdicts, old, new, safe, unreadable, [],
                          True)
    findings = []
    lost = verdicts.count(STRANDED)
    if lost:
        findings.append(Finding(
            "STRANDED", True,
            "the new watermark skips %d unapplied row(s). A job that selects "
            "stamp > watermark never sees them again. A safe mark is %s."
            % (lost, fmt_stamp(safe))))
    undated = verdicts.count(UNDATED)
    if undated:
        findings.append(Finding(
            "UNDATED", True,
            "%d source row(s) have a blank key or a stamp the tool cannot "
            "read, so they are not judged. A NULL stamp is never greater "
            "than a mark, and a stamp in another format is not read."
            % undated))
    dated = [r.stamp for r in rows if r.stamp is not None]
    if dated and new > max(dated):
        findings.append(Finding(
            "AHEAD", True,
            "the new watermark %s is past the newest source stamp %s. A row "
            "that arrives stamped at or below it is never selected."
            % (fmt_stamp(new), fmt_stamp(max(dated)))))
    if new < old:
        findings.append(Finding(
            "REGRESSED", False,
            "the new watermark is behind the old one, so rows between them "
            "are selected again. Safe only if applying a row twice is safe."))
    outside = sum(1 for r, v in zip(rows, verdicts)
                  if v == BEHIND and not is_applied(r, applied))
    if outside:
        findings.append(Finding(
            "OUTSIDE", True,
            "%d unapplied row(s) have a stamp at or below the old "
            "watermark, so this run does not judge them. An earlier run may "
            "have skipped them, or they were never meant to be applied, and "
            "the tool cannot tell which. Pass an early --old-watermark, such "
            "as 1900-01-01, to judge the whole history." % outside))
    compared = len(verdicts) - undated
    if not findings and compared:
        findings.append(Finding(
            "SAFE", False, "the new watermark skips no unapplied row above "
            "the old watermark."))
    return SafeReport(rows, verdicts, old, new, safe, unreadable, findings)


def gate(findings, compared):
    """The exit status. 4 outranks 1: a run that compared nothing is not a
    clean run and not a finding either."""
    if compared == 0:
        return 4
    if any(f.is_finding for f in findings):
        return 1
    return 0


# ------------------------------------------------------------------- reporting

def verdict_line(findings, why, clean):
    """why is the reason nothing was compared, or None. clean is the sentence
    for a run with no finding, which differs by mode."""
    count = sum(1 for f in findings if f.is_finding)
    if why:
        return ("VERDICT: nothing was compared. %s This is not a clean run."
                % why)
    if count:
        return "VERDICT: %d finding(s). Do not trust this watermark." % count
    return clean


CLEAN_FROZEN = "VERDICT: no frozen MAX and no mark past the source found."
CLEAN_SAFE = "VERDICT: no skipped row found above the old watermark."


def describe_frozen(watermark, base_max, view_max, now, max_age):
    findings = judge_frozen(watermark, base_max, view_max, now, max_age)
    lines = ["wmguard frozenmark: watermark %s, base MAX %s, view MAX %s, now "
             "%s, limit %.1f h"
             % (fmt_stamp(watermark), fmt_stamp(base_max),
                "not given" if view_max is None else fmt_stamp(view_max),
                fmt_stamp(now), hours(max_age))]
    for f in findings:
        lines.append("%-9s %s" % (f.code, f.message))
    lines.append(verdict_line(findings, None, CLEAN_FROZEN))
    return lines, gate(findings, 1)


def _sample_lines(report, verdict, sample):
    picked = [r for r, v in zip(report.rows, report.verdicts) if v == verdict]
    lines = []
    if picked and sample:
        lines.append("%s rows:" % verdict)
        for r in picked[:sample]:
            lines.append("  row %d  key %s  stamp %s"
                         % (r.number, r.key,
                            "none" if r.stamp is None else fmt_stamp(r.stamp)))
        if len(picked) > sample:
            lines.append("  ...and %d more" % (len(picked) - sample))
    return lines


def describe_safe(report, sample):
    lines = ["window: stamp after %s, new watermark %s"
             % (fmt_stamp(report.old), fmt_stamp(report.new))]
    if report.applied_unusable:
        lines.append("applied file: %d row(s), none with a readable key and "
                     "stamp" % report.unreadable)
        lines.append(verdict_line(report.findings, report.why, CLEAN_SAFE))
        return lines
    lines.append(", ".join("%s %d" % (c, report.counts[c]) for c in CLASSES))
    if report.unreadable:
        lines.append("applied file: %d row(s) with no readable key or stamp "
                     "were counted as not applied" % report.unreadable)
    for f in report.findings:
        lines.append("%-9s %s" % (f.code, f.message))
    lines.extend(_sample_lines(report, STRANDED, sample))
    lines.extend(_sample_lines(report, UNDATED, sample))
    lines.append("safe watermark: %s" % fmt_stamp(report.safe))
    lines.append(verdict_line(report.findings, report.why, CLEAN_SAFE))
    return lines


def classified_rows(report):
    return [{"row": r.number, "key": cell(r.key),
             "stamp": "" if r.stamp is None else fmt_stamp(r.stamp),
             "class": v}
            for r, v in zip(report.rows, report.verdicts)]


def write_classified(path, report):
    # "x", not "w": the file must not exist, so a path that appeared after
    # the check in _run_safe still cannot be overwritten.
    with open(path, "x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["row", "key", "stamp", "class"])
        writer.writeheader()
        writer.writerows(classified_rows(report))


# ------------------------------------------------------------------------- I/O

class UnreadableText(Exception):
    """The file is not UTF-8 text. Exit 2, like any file that cannot be read."""


def read_csv(path, fields):
    """Rows of a CSV as dicts. Raises ValueError when a column is missing."""
    with open(path, "rb") as fh:
        data = fh.read()
    # A UTF-16 file has NUL bytes, which the csv module refuses on Python 3.9
    # and 3.10 and reads as text on 3.11 and later. Say so before that matters.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ValueError("%s: this looks like a UTF-16 file. Export "
                         "comma-separated UTF-8 text." % path)
    # Strict UTF-8: a byte that is not UTF-8 would otherwise become U+FFFD,
    # and two different keys could collapse into one and hide a stranded row.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnreadableText(
            "%s: byte 0x%02x at offset %d is not UTF-8. Export the file as "
            "UTF-8 text." % (path, data[exc.start], exc.start))
    # A BOM would stay in the first header and break its match.
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    absent = [f for f in fields if f not in header]
    if absent:
        raise ValueError(
            "%s: columns not in the CSV: %s. It has: %s"
            % (path, ", ".join(absent),
               ", ".join(header) if header else "(no columns at all)"))
    return list(reader)


# ------------------------------------------------------------------------- CLI

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="wmguard.py", allow_abbrev=False,
        description="Flag a sync watermark taken from a frozen table or "
                    "advanced past unapplied rows. Writes nothing without "
                    "--apply.")
    ap.add_argument("mode", nargs="?", choices=MODES,
                    help="frozenmark: is the source MAX frozen. safemark: "
                         "did the watermark skip unapplied rows.")
    ap.add_argument("--watermark", help="frozenmark: the stored watermark")
    ap.add_argument("--base-max", dest="base_max",
                    help="frozenmark: MAX(stamp) of the table the job reads")
    ap.add_argument("--view-max", dest="view_max",
                    help="frozenmark: MAX(stamp) through the _evw view")
    ap.add_argument("--now", help="frozenmark: the time to measure age from "
                                  "(default: this machine's clock)")
    ap.add_argument("--max-age-hours", dest="max_age_hours", type=float,
                    help="frozenmark: a base MAX older than this is stale "
                         "(default %.0f)" % DEFAULT_MAX_AGE_HOURS)
    ap.add_argument("--source", help="safemark: CSV of the source rows")
    ap.add_argument("--applied", help="safemark: CSV of the rows applied")
    ap.add_argument("--old-watermark", dest="old_watermark",
                    help="safemark: the watermark before the sync")
    ap.add_argument("--new-watermark", dest="new_watermark",
                    help="safemark: the watermark the sync wrote")
    ap.add_argument("--key-field", dest="key_field",
                    help="safemark: the key column (default %s)"
                         % DEFAULT_KEY_FIELD)
    ap.add_argument("--stamp-field", dest="stamp_field",
                    help="safemark: the stamp column (default %s)"
                         % DEFAULT_STAMP_FIELD)
    ap.add_argument("--applied-key-field", dest="applied_key_field",
                    help="safemark: the key column of --applied")
    ap.add_argument("--applied-stamp-field", dest="applied_stamp_field",
                    help="safemark: the stamp column of --applied")
    ap.add_argument("--sample", type=int,
                    help="safemark: rows to list under a heading "
                         "(default %d)" % DEFAULT_SAMPLE)
    ap.add_argument("--out", help="safemark: path for the classification CSV")
    ap.add_argument("--apply", action="store_true",
                    help="safemark: write --out. Without this nothing is "
                         "written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _given(args, names):
    """Flags the operator typed. Identity tests, not ==, because 0 == False
    and a zero value must count as given."""
    return [n for n in names
            if getattr(args, n) is not None and getattr(args, n) is not False]


def _flag(name):
    return "--" + name.replace("_", "-")


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode.

    A key that holds a character a cp1252 pipe cannot write would raise a
    UnicodeEncodeError, exit 1 and never print the verdict. A character that cannot be shown prints as ? instead.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a finding'."""
    _tolerate_odd_characters()
    try:
        code = _run(argv)
        # Flush here, inside the guard. A pipe that closed early fails at this
        # flush when the output is small, and Python would otherwise fail
        # again at exit and turn the exit status into 120.
        if sys.stdout is not None:
            sys.stdout.flush()
        return code
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the data." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        _discard_stdout()
        return 70


def _discard_stdout():
    """Point stdout at the null device so the flush at exit cannot fail.
    This is the recipe in the Python signal module docs for a closed pipe."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        # A stdout with no file descriptor (or none at all) has no exit flush
        # to fail.
        pass


def _stamp_flag(name, text):
    try:
        return parse_stamp(text)
    except ValueError as exc:
        raise ValueError("%s: %s" % (_flag(name), exc))


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file could
        # not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()
    if args.mode is None:
        print("error: name a mode, frozenmark or safemark. Use --self-test to "
              "verify the tool without one.", file=sys.stderr)
        return 64
    frozen = args.mode == "frozenmark"
    wrong = _given(args, SAFE_ONLY if frozen else FROZEN_ONLY)
    if wrong:
        print("error: %s cannot be used with %s: nothing would read it."
              % (", ".join(_flag(n) for n in wrong), args.mode),
              file=sys.stderr)
        return 64
    missing = [n for n in (FROZEN_REQUIRED if frozen else SAFE_REQUIRED)
               if getattr(args, n) is None]
    if missing:
        print("error: %s needs %s." % (args.mode,
              ", ".join(_flag(n) for n in missing)), file=sys.stderr)
        return 64

    try:
        if frozen:
            return _run_frozen(args)
        return _run_safe(args)
    except ValueError as exc:
        # A stamp or column the operator got wrong is a usage error, and a
        # file that could not be read is not. Only the exit code tells them
        # apart.
        print("error: %s" % exc, file=sys.stderr)
        return 64


def _run_frozen(args):
    watermark = _stamp_flag("watermark", args.watermark)
    base_max = _stamp_flag("base_max", args.base_max)
    view_max = None
    if args.view_max is not None:
        view_max = _stamp_flag("view_max", args.view_max)
    now = (_stamp_flag("now", args.now) if args.now is not None
           else datetime.datetime.now())
    limit = (DEFAULT_MAX_AGE_HOURS if args.max_age_hours is None
             else args.max_age_hours)
    if not 0 <= limit <= MAX_AGE_LIMIT_HOURS:
        raise ValueError("--max-age-hours must be between 0 and %d."
                         % MAX_AGE_LIMIT_HOURS)
    lines, code = describe_frozen(watermark, base_max, view_max, now,
                                  datetime.timedelta(hours=limit))
    for line in lines:
        print(line)
    return code


def _run_safe(args):
    old = _stamp_flag("old_watermark", args.old_watermark)
    new = _stamp_flag("new_watermark", args.new_watermark)
    sample = DEFAULT_SAMPLE if args.sample is None else args.sample
    if sample < 0:
        raise ValueError("--sample cannot be negative.")
    for name in ("key_field", "stamp_field", "applied_key_field",
                 "applied_stamp_field", "out"):
        if getattr(args, name) == "":
            raise ValueError("%s cannot be empty." % _flag(name))
    if args.apply and not args.out:
        raise ValueError("--apply needs --out.")
    if args.out is not None and os.path.lexists(args.out):
        # Checked on a dry run too: the run that would clobber a file should
        # say so before --apply is added, not after. This covers the input
        # files, hard links to them and every other existing path.
        raise ValueError("--out %s already exists. wmguard never overwrites; "
                         "name a new file." % args.out)
    key = DEFAULT_KEY_FIELD if args.key_field is None else args.key_field
    stamp = (DEFAULT_STAMP_FIELD if args.stamp_field is None
             else args.stamp_field)
    akey = key if args.applied_key_field is None else args.applied_key_field
    astamp = (stamp if args.applied_stamp_field is None
              else args.applied_stamp_field)
    try:
        source = read_csv(args.source, (key, stamp))
        applied_rows = read_csv(args.applied, (akey, astamp))
    except (IOError, OSError, csv.Error, UnreadableText) as exc:
        # csv.Error too. A NUL in the file, or a field over csv's limit, is an
        # unreadable input and not a watermark that failed.
        print("error: %s" % exc, file=sys.stderr)
        return 2
    rows = build_source(source, key, stamp)
    applied, unreadable = build_applied(applied_rows, akey, astamp)
    report = judge_safe(rows, applied, unreadable, old, new)
    print("wmguard safemark: %s %d row(s), %s %d row(s)"
          % (args.source, len(rows), args.applied, len(applied_rows)))
    for line in describe_safe(report, sample):
        print(line)
    code = gate(report.findings, report.compared)
    if args.out is not None:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
        elif report.compared == 0:
            print("\nRefused. %s was not written, because nothing was "
                  "compared." % args.out)
        else:
            try:
                write_classified(args.out, report)
            except (IOError, OSError, csv.Error) as exc:
                # A traceback here would exit 1, the code for a finding.
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)
    return code


# ------------------------------------------------------------------ self-test

def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises, so a crash never leaves wmguard-selftest-* behind."""
    tmp = tempfile.mkdtemp(prefix="wmguard-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core and the CLI. No network, no
    credentials. The only files are the ones written under tmp."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def tmpfile(name, text):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_cli(argv):
        """main() with its output captured, so the self-test stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def T(text):
        return parse_stamp(text)

    def codes(findings):
        return [f.code for f in findings]

    def frozen(watermark, base, view, now, age_hours=48.0):
        return judge_frozen(T(watermark), T(base), None if view is None
                            else T(view), T(now),
                            datetime.timedelta(hours=age_hours))

    print("wmguard self-test: temporary files only, no network, no "
          "credentials")
    print("-" * 68)

    # ---- timestamps
    check(T("2026-03-01 08:30:00") == datetime.datetime(2026, 3, 1, 8, 30),
          "a space-separated timestamp parses")
    check(T("2026-03-01T08:30:00") == T("2026-03-01 08:30:00"),
          "the ISO T form is the same instant")
    check(T("2026-03-01 08:30:00.007").microsecond == 7000,
          "a three-digit fraction parses, the grain of SQL Server datetime")
    check(T("2026-03-01") == datetime.datetime(2026, 3, 1),
          "a bare date is midnight")
    check(T("  2026-03-01 08:30:00  ") == T("2026-03-01 08:30:00"),
          "surrounding spaces are trimmed")
    raises(lambda: T("2026-03-01 08:30:00Z"),
           "a time zone suffix is refused, not dropped  <-- pinned defect")
    raises(lambda: T("2026-03-01 08:30:00+05:00"),
           "a numeric offset is refused too")
    raises(lambda: T(""), "an empty stamp is refused")
    raises(lambda: T(None), "a missing stamp is refused")
    check(T("2026-3-1 8:30:00") == T("2026-03-01 08:30:00"),
          "strptime also accepts a one-digit month, day and hour")
    raises(lambda: T("03/01/2026"),
           "a US date is refused, because its month and day are ambiguous")
    raises(lambda: T("2026-03-01 08:30:00.1234567"),
           "a seven-digit fraction (datetime2(7)) is refused, not rounded")
    check(fmt_stamp(T("2026-03-01T08:30:00")) == "2026-03-01 08:30:00",
          "stamps print in one form")
    check(hours(datetime.timedelta(minutes=90)) == 1.5,
          "hours() converts a delta")

    # ---- frozenmark: the base MAX froze, the view kept moving
    f = frozen("2026-01-05 00:00:00", "2026-01-05 00:00:00",
               "2026-03-01 08:30:00", "2026-03-01 09:00:00")
    check(codes(f) == ["FROZEN"] and f[0].is_finding,
          "a watermark equal to a frozen base MAX, with the view ahead, is "
          "FROZEN  <-- pinned defect")
    check("equals the base MAX" in f[0].message
          and "selects nothing" in f[0].message,
          "and the finding says the watermark came from the base MAX")
    check("is 1328.5 h ahead" in f[0].message,
          "the gap between base and view is measured, 55 days 8.5 h")
    f = frozen("2026-01-04 00:00:00", "2026-01-05 00:00:00",
               "2026-03-01 08:30:00", "2026-03-01 09:00:00")
    check(codes(f) == ["FROZEN"] and "behind the base MAX" in f[0].message
          and "cannot reach" not in f[0].message,
          "a watermark behind a frozen base MAX is FROZEN too, a different "
          "cause")
    f = frozen("2026-03-01 00:00:00", "2026-03-01 08:00:00",
               "2026-03-01 08:00:01", "2026-03-04 00:00:00")
    check(codes(f) == ["FROZEN"] and "does not hold the rows between"
          in f[0].message and "selects nothing" not in f[0].message,
          "and its wording names only the part of the gap the base cannot "
          "hold, not the rows the job still reads")
    f = frozen("2026-02-01 00:00:00", "2026-01-05 00:00:00",
               "2026-03-01 08:30:00", "2026-03-01 09:00:00")
    check(codes(f) == ["BASE_LAGS"] and not f[0].is_finding
          and "did not come from the base table" in f[0].message,
          "a watermark above a stale base MAX did not come from the base: a "
          "note, not a finding  <-- pinned defect")
    f = frozen("2026-03-01 02:00:00", "2026-02-20 00:00:00",
               "2026-03-01 08:30:00", "2026-03-01 09:00:00")
    check(codes(f) == ["BASE_LAGS"] and not f[0].is_finding,
          "a job that reads the view, whose mark trails the view MAX, is not "
          "FROZEN  <-- pinned defect")
    f = frozen("2026-03-01 08:00:00", "2026-03-01 08:00:00",
               "2026-03-01 08:20:00", "2026-03-01 08:30:00")
    check(codes(f) == ["BASE_LAGS"] and not f[0].is_finding
          and "not a freeze" in f[0].message
          and gate(f, 1) == 0,
          "a base MAX inside the age limit that the view is ahead of is the "
          "normal state between compresses: a note, exit 0  "
          "<-- pinned defect")
    f = frozen("2026-02-28 00:00:00", "2026-03-01 00:00:00",
               "2026-03-01 00:00:01", "2026-03-01 00:10:00")
    check(codes(f) == ["BASE_LAGS"],
          "a mark 24 h behind a recent base MAX, with the view 1 s ahead, is "
          "not FROZEN  <-- pinned defect")
    f = frozen("2026-03-01 00:00:00", "2026-03-01 00:00:00",
               "2026-03-01 00:05:00", "2026-03-03 00:00:00")
    check(codes(f) == ["BASE_LAGS"],
          "a base MAX exactly at the age limit is not frozen, the limit is "
          "inclusive")
    f = frozen("2026-03-01 00:00:00", "2026-03-01 00:00:00",
               "2026-03-01 00:05:00", "2026-03-03 00:00:01")
    check(codes(f) == ["FROZEN"], "and one second over it is FROZEN")
    f = frozen("2026-03-01 08:30:00", "2026-01-05 00:00:00",
               "2026-03-01 08:30:00", "2026-03-01 09:00:00")
    check(codes(f) == ["BASE_LAGS"] and not f[0].is_finding,
          "a watermark taken from the view is fine though the base lags: a "
          "note, not a finding")
    check("stale" in f[0].message,
          "and the note says other readers of the base are stale")
    f = frozen("2026-03-01 08:30:00", "2026-03-01 08:30:00",
               "2026-03-01 08:30:00", "2026-03-01 09:00:00")
    check(codes(f) == ["FRESH"],
          "base and view agree and are recent: FRESH")

    # ---- frozenmark: no view to compare with
    f = frozen("2026-01-05 00:00:00", "2026-01-05 00:00:00", None,
               "2026-03-01 09:00:00")
    check(codes(f) == ["STALE"] and f[0].is_finding,
          "an old base MAX with no view is STALE  <-- pinned defect")
    check("look the same" in f[0].message,
          "and the finding says it cannot tell frozen from idle")
    f = frozen("2026-03-01 08:30:00", "2026-03-01 08:30:00", None,
               "2026-03-01 09:00:00")
    check(codes(f) == ["FRESH"], "a recent base MAX with no view is FRESH")
    f = frozen("2026-03-01 00:00:00", "2026-03-01 00:00:00", None,
               "2026-03-03 00:00:00")
    check(codes(f) == ["FRESH"],
          "a base MAX exactly at the age limit is not stale, the limit is "
          "inclusive  <-- pinned defect")
    f = frozen("2026-03-01 00:00:00", "2026-03-01 00:00:00", None,
               "2026-03-03 00:00:01")
    check(codes(f) == ["STALE"], "and one second over it is stale")
    f = frozen("2026-03-01 00:00:00", "2026-03-01 00:00:00", None,
               "2026-03-01 06:00:01", age_hours=6.0)
    check(codes(f) == ["STALE"], "the limit is the operator's, not fixed")
    f = frozen("2026-03-01 00:00:00", "2026-03-01 00:00:00", None,
               "2026-02-01 00:00:00")
    check(codes(f) == ["FRESH"],
          "a base MAX in the future of now (clock skew) is not stale")

    # ---- frozenmark: a quiet table is not a frozen one
    f = frozen("2026-01-05 00:00:00", "2026-01-05 00:00:00",
               "2026-01-05 00:00:00", "2026-03-01 09:00:00")
    check(codes(f) == ["IDLE"] and not f[0].is_finding
          and "not ahead of it" in f[0].message,
          "an old base MAX that the view does not pass is IDLE, a note  "
          "<-- pinned defect")
    f = frozen("2026-01-05 00:00:00", "2026-01-05 00:00:00",
               "2026-01-04 00:00:00", "2026-03-01 09:00:00")
    check(codes(f) == ["IDLE"],
          "a view MAX behind the base MAX (the newest row was deleted) is "
          "not freeze evidence")

    # ---- frozenmark: a watermark past everything
    f = frozen("2026-03-02 00:00:00", "2026-03-01 08:30:00", None,
               "2026-03-02 01:00:00")
    check(codes(f) == ["AHEAD"] and f[0].is_finding,
          "a watermark past the newest source stamp is AHEAD")
    f = frozen("2026-03-02 00:00:00", "2026-03-01 08:30:00",
               "2026-03-01 08:30:00", "2026-03-02 01:00:00")
    check(codes(f) == ["AHEAD"], "an AHEAD verdict is judged against the view as well")
    f = frozen("2026-03-02 00:00:00", "2026-01-05 00:00:00", None,
               "2026-03-02 01:00:00")
    check(codes(f) == ["STALE", "AHEAD"],
          "a stale base with a watermark past it reports both")
    f = frozen("2026-03-01 08:30:00", "2026-01-05 00:00:00",
               "2026-03-01 08:30:00", "2026-03-01 09:00:00")
    check("AHEAD" not in codes(f),
          "a watermark equal to the view MAX is not ahead of it")

    # ---- safemark: classify
    old = T("2026-03-01 00:00:00")
    new = T("2026-03-01 06:00:00")
    applied = {"A": T("2026-03-01 01:00:00")}

    def row(number, key, stamp):
        return SourceRow(number, key, None if stamp is None else T(stamp))

    def cls(key, stamp, applied_map=applied, o=old, n=new):
        return classify_row(row(1, key, stamp), applied_map, o, n)

    check(cls("A", "2026-03-01 01:00:00") == APPLIED,
          "a row applied at its own stamp is APPLIED")
    check(cls("B", "2026-03-01 02:00:00") == STRANDED,
          "an unapplied row inside the window is STRANDED  "
          "<-- pinned defect")
    check(cls("B", "2026-03-01 07:00:00") == PENDING,
          "an unapplied row above the new mark is PENDING, still reachable")
    check(cls("B", "2026-02-28 23:00:00") == BEHIND,
          "a row at or under the old mark is outside this run: BEHIND")
    check(cls("B", "2026-03-01 00:00:00") == BEHIND,
          "a row exactly on the old mark is BEHIND, selection is strict")
    check(cls("B", "2026-03-01 06:00:00") == STRANDED,
          "a row exactly on the new mark is STRANDED, the mark is "
          "inclusive  <-- pinned defect")
    check(cls("B", "2026-03-01 06:00:00.001") == PENDING,
          "and one millisecond above it is PENDING")
    check(cls("B", None) == UNDATED, "a row with no stamp is UNDATED")
    check(cls("A", "2026-03-01 02:00:00") == STRANDED,
          "a row edited after it was applied is unapplied at its new stamp  "
          "<-- pinned defect")
    check(cls("A", "2026-03-01 00:30:00") == APPLIED,
          "a row applied at a newer stamp than the source shows is APPLIED")
    check(cls("", "2026-03-01 02:00:00") == UNDATED,
          "a row in the window with a blank key is UNDATED, never guessed "
          "APPLIED  <-- pinned defect")
    check(cls("", "2026-03-01 02:00:00", {"": T("2026-03-01 02:00:00")})
          == UNDATED,
          "and a blank key does not match a blank key in the applied map")
    check(cls("", "2026-02-28 00:00:00") == BEHIND,
          "a blank key below the old mark is outside the run: BEHIND")

    # ---- safemark: the safe watermark
    def judge(source, applied_map, o=old, n=new, unreadable=0):
        rows = [row(i + 1, k, s) for i, (k, s) in enumerate(source)]
        return judge_safe(rows, applied_map, unreadable, o, n)

    r = judge([("A", "2026-03-01 01:00:00"), ("B", "2026-03-01 02:00:00"),
               ("C", "2026-03-01 03:00:00")],
              {"A": T("2026-03-01 01:00:00"), "C": T("2026-03-01 03:00:00")},
              n=T("2026-03-01 03:00:00"))
    check(codes(r.findings) == ["STRANDED"] and r.counts[STRANDED] == 1,
          "one skipped row among applied ones is one STRANDED finding")
    check(r.safe == T("2026-03-01 01:00:00"),
          "the safe mark is the newest applied stamp below the oldest "
          "unapplied one  <-- pinned defect")
    check("2026-03-01 01:00:00" in r.findings[0].message,
          "and the finding names it")
    r = judge([("A", "2026-03-01 02:00:00"), ("B", "2026-03-01 02:00:00")],
              {"A": T("2026-03-01 02:00:00")})
    check(r.safe == old,
          "an applied row that shares the oldest unapplied stamp does not "
          "lift the mark: it stays below the tie  <-- pinned defect")
    r = judge([("A", "2026-03-01 01:00:00"), ("B", "2026-03-01 02:00:00")],
              {"A": T("2026-03-01 01:00:00"), "B": T("2026-03-01 02:00:00")},
              n=T("2026-03-01 02:00:00"))
    check(codes(r.findings) == ["SAFE"] and r.safe == T("2026-03-01 02:00:00"),
          "everything applied: SAFE, and the safe mark is the newest applied "
          "stamp")
    check(gate(r.findings, r.compared) == 0, "a SAFE verdict exits 0")
    r = judge([("A", "2026-03-01 01:00:00"), ("B", "2026-03-01 07:00:00")],
              {"A": T("2026-03-01 01:00:00")})
    check(codes(r.findings) == ["SAFE"] and r.safe == T("2026-03-01 01:00:00"),
          "an unapplied row above the new mark is safe: PENDING keeps the "
          "next run honest")
    r = judge([("B", "2026-03-01 02:00:00")], {})
    check(r.safe == old, "with nothing applied the safe mark is the old one")
    r = judge([("A", "2026-03-01 01:00:00"), ("P", "2026-03-01 02:00:00"),
               ("B", "2026-03-01 03:00:00")],
              {"A": T("2026-03-01 01:00:00"), "B": T("2026-03-01 03:00:00")},
              n=T("2026-03-01 01:30:00"))
    check(r.verdicts == [APPLIED, PENDING, APPLIED]
          and r.safe == T("2026-03-01 01:00:00"),
          "an unapplied row above the new mark (PENDING) also caps the safe "
          "mark: it never rises past a row the next run still needs  "
          "<-- pinned defect")
    r = judge([("A", "2026-03-01 01:00:00"), ("B", "2026-03-01 02:00:00")],
              {"A": T("2026-03-01 01:00:00"), "B": T("2026-03-01 02:00:00")},
              o=T("2026-03-01 03:00:00"), n=T("2026-03-01 03:00:00"))
    check(r.safe == T("2026-03-01 03:00:00"),
          "the safe mark never falls below the old mark")

    # ---- safemark: the other ways a mark is wrong
    r = judge([("A", "2026-03-01 01:00:00")],
              {"A": T("2026-03-01 01:00:00")},
              n=T("2026-03-01 09:00:00"))
    check(codes(r.findings) == ["AHEAD"] and r.findings[0].is_finding
          and gate(r.findings, r.compared) == 1,
          "a mark past every source stamp is AHEAD even when nothing is "
          "stranded, and it exits 1  <-- pinned defect")
    r = judge([("A", "2026-03-01 01:00:00"), ("B", None)],
              {"A": T("2026-03-01 01:00:00")},
              n=T("2026-03-01 01:00:00"))
    check(codes(r.findings) == ["UNDATED"] and r.findings[0].is_finding
          and gate(r.findings, r.compared) == 1,
          "a row with no stamp is a finding: stamp > mark is never true for "
          "NULL, and it exits 1  <-- pinned defect")
    check(r.compared == 1 and r.counts[UNDATED] == 1,
          "and it is not counted as compared")
    r = judge([("A", "2026-03-01 05:00:00")],
              {"A": T("2026-03-01 05:00:00")},
              o=T("2026-03-01 04:00:00"), n=T("2026-03-01 02:00:00"))
    check(codes(r.findings) == ["REGRESSED"] and not r.findings[0].is_finding,
          "a mark moved backwards is a note: rows are selected twice, none "
          "lost")
    check(gate(r.findings, r.compared) == 0, "a regression alone exits 0")
    r = judge([("A", "2026-03-01 05:00:00")],
              {"A": T("2026-03-01 05:00:00")},
              o=T("2026-03-01 04:00:00"), n=T("2026-03-01 04:00:00"))
    check("REGRESSED" not in codes(r.findings),
          "a new mark equal to the old one is not a regression")
    r = judge([("A", None)], {})
    check(r.compared == 0 and gate(r.findings, r.compared) == 4,
          "a source with no readable stamp compared nothing: exit 4, not "
          "clean  <-- pinned defect")
    r = judge([], {})
    check(r.compared == 0 and gate(r.findings, r.compared) == 4
          and codes(r.findings) == [] and r.why is not None,
          "an empty source is exit 4 as well, and prints no SAFE line "
          "<-- pinned defect")
    check(gate([Finding("X", True, "")], 3) == 1
          and gate([Finding("X", False, "")], 3) == 0,
          "gate: a finding is 1, a note is 0")
    r = judge([("A", "2026-03-01 01:00:00"), ("B", "2026-03-01 02:00:00"),
               ("C", "2026-03-01 07:00:00"), ("D", "2026-02-01 00:00:00"),
               ("E", None)], {"A": T("2026-03-01 01:00:00")})
    check(sum(r.counts.values()) == 5
          and (r.counts[APPLIED], r.counts[STRANDED], r.counts[PENDING],
               r.counts[BEHIND], r.counts[UNDATED]) == (1, 1, 1, 1, 1),
          "every source row lands in exactly one class and the counts add "
          "up")

    # ---- rows at or below the old mark that were never applied
    r = judge([("A", "2026-02-28 01:00:00"), ("B", "2026-02-28 02:00:00"),
               ("C", "2026-03-01 01:00:00")],
              {"B": T("2026-02-28 02:00:00"), "C": T("2026-03-01 01:00:00")},
              n=T("2026-03-01 01:00:00"))
    check(codes(r.findings) == ["OUTSIDE"]
          and r.findings[0].is_finding
          and r.findings[0].message.startswith("1 unapplied row(s)")
          and "1900-01-01" in r.findings[0].message
          and gate(r.findings, r.compared) == 1,
          "an unapplied row at or below the old mark is a finding, not a "
          "clean exit 0 over a row the tool could not judge  <-- pinned "
          "defect")
    r = judge([("A", "2026-02-28 01:00:00")], {"A": T("2026-02-28 01:00:00")})
    check("OUTSIDE" not in codes(r.findings),
          "and an applied row below the old mark is not reported")
    r = judge([("A", "2026-03-01 01:00:00"), ("", "2026-03-01 02:00:00"),
               ("", "2026-03-01 03:00:00")],
              {"A": T("2026-03-01 01:00:00")}, n=T("2026-03-01 03:00:00"))
    check(r.verdicts == [APPLIED, UNDATED, UNDATED]
          and gate(r.findings, r.compared) == 1,
          "rows with a blank key are UNDATED and the run exits 1, not 0  "
          "<-- pinned defect")
    r = judge([("A", "2026-03-01 01:00:00"), ("", "2026-03-01 02:00:00"),
               ("C", "2026-03-01 03:00:00")],
              {"A": T("2026-03-01 01:00:00"), "C": T("2026-03-01 03:00:00")},
              n=T("2026-03-01 06:00:00"))
    check(r.safe == T("2026-03-01 01:00:00"),
          "a blank-key row in the window holds the safe mark below its own "
          "stamp, because it cannot be shown applied  <-- pinned defect")
    r = judge([("A", "2026-03-01 01:00:00"), ("B", None),
               ("C", "2026-03-01 03:00:00")],
              {"A": T("2026-03-01 01:00:00"), "C": T("2026-03-01 03:00:00")},
              n=T("2026-03-01 06:00:00"))
    check(r.verdicts == [APPLIED, UNDATED, APPLIED]
          and r.safe == T("2026-03-01 03:00:00")
          and gate(r.findings, r.compared) == 1,
          "a row with an unreadable stamp has no place on the time line, so "
          "it does not move the safe mark, and it still exits 1")
    r = judge([("A", "2026-03-01 01:00:00")], {}, unreadable=2)
    check(r.applied_unusable and r.compared == 0 and r.findings == []
          and gate(r.findings, r.compared) == 4 and "date format" in r.why,
          "an applied file with rows and none readable compares nothing, "
          "instead of reporting every row STRANDED  <-- pinned defect")
    r = judge([("A", "2026-03-01 01:00:00")],
              {"B": T("2026-03-01 01:00:00")}, unreadable=2)
    check(not r.applied_unusable and r.counts[STRANDED] == 1,
          "an applied file with some readable rows is judged as usual")

    # ---- the applied side
    recs = [{"K": "A", "S": "2026-03-01 01:00:00"},
            {"K": "A", "S": "2026-03-01 03:00:00"},
            {"K": "A", "S": "2026-03-01 02:00:00"},
            {"K": "B", "S": ""},
            {"K": "C", "S": "garbage"},
            {"K": " D ", "S": "2026-03-01 04:00:00"}]
    got, bad = build_applied(recs, "K", "S")
    check(got == {"A": T("2026-03-01 03:00:00"),
                  "D": T("2026-03-01 04:00:00")} and bad == 2,
          "a key applied twice keeps its newest stamp, an unreadable stamp "
          "is counted and left out, and keys are trimmed")
    got, bad = build_applied([{"K": "  ", "S": "2026-03-01 01:00:00"},
                              {"K": "", "S": "2026-03-01 02:00:00"}],
                             "K", "S")
    check(got == {} and bad == 2,
          "an applied row with a blank key is counted and left out, so it "
          "can never match  <-- pinned defect")
    check(build_applied([{"K": None, "S": None}], "K", "S") == ({}, 1),
          "a short row (None cells) is an unreadable applied row")
    src = build_source([{"K": "A", "S": "2026-03-01 01:00:00"},
                        {"K": None, "S": "x"}], "K", "S")
    check(build_source([{"K": " A ", "S": "2026-03-01"}], "K", "S")[0].key
          == "A", "source keys are trimmed too")
    check(src[0] == SourceRow(1, "A", T("2026-03-01 01:00:00"))
          and src[1] == SourceRow(2, "", None),
          "source rows are numbered from 1 and a bad stamp becomes None")

    # ---- reading CSV
    bom = tmpfile("bom.csv", chr(0xFEFF) + "KEY,STAMP\nA,2026-03-01 01:00:00\n")
    check(read_csv(bom, ("KEY", "STAMP"))[0]["KEY"] == "A",
          "a UTF-8 BOM does not hide the first column  <-- pinned defect")
    padded = tmpfile("padded.csv", "KEY , STAMP\nA,2026-03-01\n")
    check(read_csv(padded, ("KEY", "STAMP"))[0]["STAMP"] == "2026-03-01",
          "spaces around a header name are trimmed")
    raises(lambda: read_csv(bom, ("KEY", "NOPE")),
           "a missing column is a ValueError that names it")
    try:
        read_csv(bom, ("NOPE",))
    except ValueError as exc:
        check("NOPE" in str(exc) and "KEY, STAMP" in str(exc),
              "and lists the columns the file does have")
    empty = tmpfile("empty.csv", "")
    try:
        read_csv(empty, ("KEY",))
    except ValueError as exc:
        check("(no columns at all)" in str(exc),
              "an empty file says it has no columns at all")

    # ---- the CLI: frozenmark
    fz = ["frozenmark", "--watermark", "2026-01-05 00:00:00",
          "--base-max", "2026-01-05 00:00:00", "--now", "2026-03-01 09:00:00"]
    code, out, err = run_cli(fz + ["--view-max", "2026-03-01 08:30:00"])
    check(code == 1 and "FROZEN" in out and "VERDICT: 1 finding(s)" in out
          and err == "",
          "frozenmark exits 1 on a frozen base MAX")
    code, out, err = run_cli(fz)
    check(code == 1 and "STALE" in out and "view MAX not given" in out,
          "frozenmark with no view says so and exits 1 on a stale base")
    code, out, err = run_cli(fz + ["--view-max", "2026-01-05 00:00:00"])
    check(code == 0 and "IDLE" in out
          and "VERDICT: no frozen MAX and no mark past the source found." in out,
          "an idle table exits 0")
    code, out, err = run_cli(fz + ["--max-age-hours", "100000"])
    check(code == 0 and "FRESH" in out and "100000.0 h" in out,
          "a wide --max-age-hours is honoured")
    code, out, err = run_cli(
        ["frozenmark", "--watermark", "2026-01-05 00:00:00", "--base-max",
         "2026-01-05 00:00:00"])
    check(code == 1 and "STALE" in out,
          "with no --now the machine's clock is used, and 2026-01-05 is old "
          "by then")
    code, out, err = run_cli(
        ["frozenmark", "--watermark", "9998-01-01 00:00:00", "--base-max",
         "9998-01-01 00:00:00"])
    check(code == 0 and "FRESH" in out,
          "and a base MAX in the far future of the clock is fresh")
    code, out, err = run_cli(fz + ["--max-age-hours", "-1"])
    check(code == 64 and "between 0 and" in err and out == "",
          "a negative age limit is a usage error that prints nothing to "
          "stdout")
    check(run_cli(fz + ["--max-age-hours", "nan"])[0] == 64
          and run_cli(fz + ["--max-age-hours", "inf"])[0] == 64
          and run_cli(fz + ["--max-age-hours", "1e30"])[0] == 64,
          "nan, inf and an absurd age limit are usage errors, not crashes  "
          "<-- pinned defect")
    check(run_cli(fz + ["--max-age-hours", "abc"])[0] == 64,
          "a non-number age limit is a usage error")
    check(run_cli(fz + ["--max-age-hours", "0"])[0] == 1
          and run_cli(fz + ["--max-age-hours", "1000000"])[0] == 0
          and run_cli(fz + ["--max-age-hours", "1000000.5"])[0] == 64,
          "the age limit 0 and the upper limit are accepted, and one step "
          "past the upper limit is refused")
    code, out, err = run_cli(
        ["frozenmark", "--watermark", "yesterday", "--base-max",
         "2026-01-05"])
    check(code == 64 and "--watermark" in err and "not a timestamp" in err,
          "a bad stamp names the flag it came from")
    check(run_cli(["frozenmark", "--watermark", "2026-01-05", "--base-max",
                   "2026-01-05", "--view-max", "soon"])[0] == 64
          and run_cli(["frozenmark", "--watermark", "2026-01-05",
                       "--base-max", "2026-01-05", "--now", "later"])[0]
          == 64,
          "a bad --view-max or --now is a usage error")
    code, out, err = run_cli(["frozenmark", "--watermark", "2026-01-05"])
    check(code == 64 and "needs --base-max" in err,
          "a missing required flag is named")

    # ---- the CLI: safemark
    src_csv = tmpfile("source.csv",
                      "KEY,STAMP,NOTE\n"
                      "A,2026-03-01 01:00:00,x\n"
                      "B,2026-03-01 02:00:00,y\n"
                      "C,2026-03-01 03:00:00,z\n"
                      "D,2026-03-01 07:00:00,w\n"
                      "E,2026-02-01 00:00:00,v\n")
    app_csv = tmpfile("applied.csv",
                      "KEY,STAMP\nA,2026-03-01 01:00:00\n"
                      "C,2026-03-01 03:00:00\nZ,\n")
    sw = ["safemark", "--source", src_csv, "--applied", app_csv,
          "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
          "2026-03-01 06:00:00"]
    code, out, err = run_cli(sw)
    check(code == 1 and "STRANDED 1" in out and "row 2  key B" in out
          and "safe watermark: 2026-03-01 01:00:00" in out
          and "VERDICT: 2 finding(s)" in out and "OUTSIDE" in out,
          "safemark names the skipped row, the safe mark and exits 1")
    check("applied file: 1 row(s) with no readable key or stamp" in out,
          "and reports the applied rows it could not read")
    check(src_csv + " 5 row(s)" in out and app_csv + " 3 row(s)" in out,
          "and prints the row count of both files")
    code, out, err = run_cli(sw + ["--sample", "0"])
    check(code == 1 and "STRANDED rows:" not in out,
          "--sample 0 lists no rows")
    code, out, err = run_cli(sw)
    check("STRANDED rows:" in out and "...and" not in out,
          "a short list has no ...and line")
    many = tmpfile("many.csv", "KEY,STAMP\n" + "".join(
        "K%d,2026-03-01 0%d:00:00\n" % (i, i) for i in range(1, 6)))
    code, out, err = run_cli(
        ["safemark", "--source", many, "--applied", app_csv,
         "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
         "2026-03-01 06:00:00", "--sample", "2"])
    check(code == 1 and "...and 3 more" in out,
          "a long list stops at --sample and says how many are left")
    code, out, err = run_cli(
        ["safemark", "--source", many, "--applied", app_csv,
         "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
         "2026-03-01 06:00:00", "--sample", "5"])
    check(code == 1 and "STRANDED rows:" in out and "...and" not in out,
          "a list exactly as long as --sample has no ...and line")
    code, out, err = run_cli(sw[:-1] + ["2026-03-01 09:00:00"])
    check(code == 1 and "AHEAD" in out,
          "a new mark past the source is AHEAD on the CLI")
    ok_src = tmpfile("ok-source.csv", "KEY,STAMP\nA,2026-03-01 01:00:00\n")
    ok_app = tmpfile("ok-applied.csv", "KEY,STAMP\nA,2026-03-01 01:00:00\n")
    ok = ["safemark", "--source", ok_src, "--applied", ok_app,
          "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
          "2026-03-01 01:00:00"]
    code, out, err = run_cli(ok)
    check(code == 0 and "SAFE" in out and "STRANDED rows" not in out
          and "applied file" not in out,
          "a safe advance exits 0 and prints no list and no applied-file "
          "line")
    nul_src = tmpfile("nostamp.csv", "KEY,STAMP\nA,\nB,\n")
    code, out, err = run_cli(
        ["safemark", "--source", nul_src, "--applied", ok_app,
         "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
         "2026-03-01 01:00:00"])
    check(code == 4 and "nothing was compared" in out
          and "UNDATED rows:" in out,
          "a source of unstamped rows exits 4 and lists them")

    # ---- the CLI: only --apply writes
    dest = os.path.join(tmp, "rows.csv")
    code, out, err = run_cli(sw + ["--out", dest])
    check(code == 1 and not os.path.exists(dest)
          and "Check only" in out and "Re-run with --apply" in out,
          "without --apply nothing is written  <-- pinned defect")
    code, out, err = run_cli(sw + ["--out", dest, "--apply"])
    with open(dest, encoding="utf-8") as handle:
        written = handle.read()
    check(code == 1 and "wrote " in out
          and written.splitlines()[0] == "row,key,stamp,class"
          and "2,B,2026-03-01 02:00:00,STRANDED" in written
          and len(written.splitlines()) == 6,
          "with --apply the classification is written, one line per row")

    # ---- the CSV cell no longer starts with a formula character
    for lead, name in (("=", "equals"), ("+", "plus"), ("-", "minus"),
                       ("@", "at sign"), ("\t", "tab"), ("\r", "CR")):
        check(cell(lead + "x") == "'" + lead + "x"
              and uncell(cell(lead + "x")) == lead + "x",
              "a cell that starts with %s gets one apostrophe, and uncell "
              "gives it back  <-- pinned defect" % name)
    check([cell(v) for v in ("P-1", "a=b", "", "'x", "'", "7", "x+y")]
          == ["P-1", "a=b", "", "'x", "'", "7", "x+y"],
          "a cell that does not start with a formula character is "
          "unchanged")
    check(all(uncell(cell(v)) == v for v in (
        "=x", "'=x", "''=x", "'-1", "'", "''", "'a", "@", "\t", "\r\n=",
        "", "x", "+")),
          "uncell(cell(v)) is v for values that already start with the "
          "guard apostrophe  <-- pinned defect")
    check(uncell("'x") == "'x" and uncell("'=x") == "=x"
          and uncell("''=x") == "'=x",
          "uncell removes exactly one apostrophe and only a guard one")
    formula_src = tmpfile(
        "formula-source.csv",
        'KEY,STAMP\n'
        '"=HYPERLINK(""http://x.invalid"",""y"")",2026-03-01 01:00:00\n'
        '+1+1,2026-03-01 01:10:00\n'
        '-2+3,2026-03-01 01:20:00\n'
        '@SUM(1),2026-03-01 01:30:00\n'
        '"\t=tabbed",2026-03-01 01:40:00\n'
        '"\r=returned",2026-03-01 01:50:00\n'
        "'=literal,2026-03-01 02:00:00\n"
        'P-1,2026-03-01 02:10:00\n'
        'a=b,2026-03-01 02:20:00\n')
    formula_app = tmpfile("formula-applied.csv",
                          "KEY,STAMP\nP-1,2026-03-01 02:10:00\n")
    formula_out = os.path.join(tmp, "formula-rows.csv")
    formula_sw = ["safemark", "--source", formula_src, "--applied",
                  formula_app, "--old-watermark", "2026-03-01 00:00:00",
                  "--new-watermark", "2026-03-01 02:20:00"]
    code, out, err = run_cli(formula_sw + ["--out", formula_out, "--apply"])
    with open(formula_out, newline="", encoding="utf-8") as handle:
        formula_rows = list(csv.reader(handle))
    keys = [r[1] for r in formula_rows[1:]]
    check(code == 1 and keys == [
        "'=HYPERLINK(\"http://x.invalid\",\"y\")", "'+1+1", "'-2+3",
        "'@SUM(1)", "'=tabbed", "'=returned", "'=literal", "P-1", "a=b"],
          "every key that starts with = + - @ comes out with a guard "
          "apostrophe in --out, and a plain key does not  <-- pinned "
          "defect")
    check(keys[6] == cell("=literal"),
          "a source key that already reads as guarded is read as the "
          "guard was removed, so --out is not a byte copy of such a key "
          "(a limit the README names)")
    check(all(r[0].isdigit() and r[2][:1].isdigit() and r[3] in CLASSES
              for r in formula_rows[1:]),
          "the row number, the stamp and the class are never guarded: the "
          "number stays a number  <-- pinned defect")
    check(not any(r[1][:1] in FORMULA_LEAD for r in formula_rows[1:]),
          "no key cell in --out starts with a formula character")
    code, out, err = run_cli(
        formula_sw[:4] + [formula_out, "--applied-key-field", "key",
                          "--applied-stamp-field", "stamp"]
        + formula_sw[5:])
    check(code == 0 and "APPLIED 9" in out and "STRANDED 0" in out,
          "the --out file read back as the applied file gives every key "
          "back, so the round trip is lossless  <-- pinned defect")

    nul_dest = os.path.join(tmp, "refused.csv")
    code, out, err = run_cli(
        ["safemark", "--source", nul_src, "--applied", ok_app,
         "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
         "2026-03-01 01:00:00", "--out", nul_dest, "--apply"])
    check(code == 4 and not os.path.exists(nul_dest) and "Refused" in out,
          "a run that compared nothing writes nothing even with --apply")
    code, out, err = run_cli(
        sw + ["--out", os.path.join(tmp, "nodir", "x.csv"), "--apply"])
    check(code == 2 and "could not write" in err and "VERDICT" in out,
          "a failed write exits 2, and the verdict printed above it still "
          "shows")
    before = [open(p, "rb").read() for p in (src_csv, app_csv)]
    for target in (src_csv, app_csv, os.path.join(tmp, ".", "source.csv")):
        code, out, err = run_cli(sw + ["--out", target, "--apply"])
        check(code == 64 and "already exists" in err and out == "",
              "--out naming an input file is refused  <-- pinned defect")
    check(run_cli(sw + ["--out", src_csv])[0] == 64,
          "and is refused on a dry run too")
    check(before == [open(p, "rb").read() for p in (src_csv, app_csv)],
          "and neither input was touched")
    precious = tmpfile("precious.txt", "keep me\n")
    code, out, err = run_cli(sw + ["--out", precious, "--apply"])
    with open(precious, encoding="utf-8") as handle:
        kept = handle.read()
    check(code == 64 and "already exists" in err and kept == "keep me\n",
          "--out naming any other existing file is refused, not overwritten  "
          "<-- pinned defect")
    check(run_cli(sw + ["--out", tmp, "--apply"])[0] == 64,
          "and so is an existing directory")
    refused = False
    try:
        write_classified(precious, judge([("A", "2026-03-01 01:00:00")], {}))
    except FileExistsError:
        refused = True
    with open(precious, encoding="utf-8") as handle:
        kept = handle.read()
    check(refused and kept == "keep me\n",
          "the writer itself refuses an existing path, so a file that "
          "appeared after the check survives  <-- pinned defect")
    code, out, err = run_cli(sw + ["--apply"])
    check(code == 64 and "--apply needs --out" in err,
          "--apply with nothing to write is a usage error")
    for flag in ("--out", "--key-field", "--stamp-field",
                 "--applied-key-field", "--applied-stamp-field"):
        code, out, err = run_cli(sw + [flag, ""])
        check(code == 64 and flag + " cannot be empty" in err and out == "",
              "%s \"\" is refused, not read as not given  <-- pinned "
              "defect" % flag)
    check(run_cli(sw + ["--sample", "-1"])[0] == 64,
          "a negative --sample is a usage error")

    # ---- the CLI: column names
    alt_src = tmpfile("alt-source.csv", "ID,UPDATED\nA,2026-03-01 01:00:00\n")
    alt_app = tmpfile("alt-applied.csv", "ID,SERVED\nA,2026-03-01 01:00:00\n")
    base = ["safemark", "--source", alt_src, "--applied", alt_app,
            "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
            "2026-03-01 01:00:00"]
    code, out, err = run_cli(base)
    check(code == 64 and "It has: ID, UPDATED" in err,
          "default column names that the file lacks are a usage error, and "
          "the message echoes the header row of the file")
    code, out, err = run_cli(base + ["--key-field", "ID", "--stamp-field",
                                     "UPDATED", "--applied-stamp-field",
                                     "SERVED"])
    check(code == 0 and "SAFE" in out,
          "--key-field, --stamp-field and --applied-stamp-field rename the "
          "columns")
    code, out, err = run_cli(base + ["--key-field", "ID", "--stamp-field",
                                     "UPDATED", "--applied-key-field", "ID",
                                     "--applied-stamp-field", "SERVED"])
    check(code == 0, "and --applied-key-field names the applied key column")
    code, out, err = run_cli(base + ["--key-field", "NOPE"])
    check(code == 64 and "NOPE" in err and out == "",
          "an unknown column names itself and prints nothing to stdout")

    # ---- the CLI: unreadable input
    code, out, err = run_cli(
        ["safemark", "--source", os.path.join(tmp, "absent.csv"),
         "--applied", app_csv, "--old-watermark", "2026-03-01",
         "--new-watermark", "2026-03-02"])
    check(code == 2 and "absent.csv" in err,
          "a file that does not exist exits 2, not 64 and not 1")
    nul = os.path.join(tmp, "nul.csv")
    with open(nul, "wb") as handle:
        handle.write(b"KEY,STAMP\nA\x00,2026-03-01\n")
    code, out, err = run_cli(
        ["safemark", "--source", nul, "--applied", app_csv,
         "--old-watermark", "2026-03-01", "--new-watermark", "2026-03-02"])
    check(code != 70 and "Traceback" not in err,
          "a NUL byte in the file never becomes a traceback")
    garbled = os.path.join(tmp, "garbled.csv")
    with open(garbled, "wb") as handle:
        handle.write(b"KEY,STAMP\nA\xff,2026-03-01 01:00:00\n")
    code, out, err = run_cli(
        ["safemark", "--source", garbled, "--applied", ok_app,
         "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
         "2026-03-01 06:00:00"])
    check(code == 2 and "byte 0xff at offset 11 is not UTF-8" in err
          and "garbled.csv" in err and out == "",
          "a source with bytes that are not UTF-8 exits 2 and names the "
          "byte, and prints no verdict")
    code, out, err = run_cli(
        ["safemark", "--source", ok_app, "--applied", garbled,
         "--old-watermark", "2026-03-01 00:00:00", "--new-watermark",
         "2026-03-01 06:00:00"])
    check(code == 2 and "garbled.csv" in err and out == "",
          "so does an applied file with bytes that are not UTF-8")
    cp_src = tmpfile("cp-src.csv", "")
    with open(cp_src, "wb") as handle:
        handle.write(b"KEY,STAMP\nM\xe9,2026-03-01 01:00:00\n"
                     b"M\xeb,2026-03-01 00:30:00\nZ,2026-03-01 02:00:00\n")
    cp_app = tmpfile("cp-app.csv", "")
    with open(cp_app, "wb") as handle:
        handle.write(b"KEY,STAMP\nM\xe9,2026-03-01 01:00:00\n"
                     b"Z,2026-03-01 02:00:00\n")
    code, out, err = run_cli(
        ["safemark", "--source", cp_src, "--applied", cp_app,
         "--old-watermark", "1900-01-01", "--new-watermark",
         "2026-03-01 01:30:00"])
    check(code == 2 and "SAFE" not in out and "offset 11" in err,
          "a Windows-1252 export whose two keys would both decode to U+FFFD "
          "is refused and never reads SAFE  <-- pinned defect")
    utf_src = tmpfile("utf-src.csv", "KEY,STAMP\nM" + chr(0xe9)
                      + ",2026-03-01 01:00:00\nM" + chr(0xeb)
                      + ",2026-03-01 00:30:00\nZ,2026-03-01 02:00:00\n")
    utf_app = tmpfile("utf-app.csv", "KEY,STAMP\nM" + chr(0xe9)
                      + ",2026-03-01 01:00:00\nZ,2026-03-01 02:00:00\n")
    code, out, err = run_cli(
        ["safemark", "--source", utf_src, "--applied", utf_app,
         "--old-watermark", "1900-01-01", "--new-watermark",
         "2026-03-01 01:30:00"])
    check(code == 1 and "STRANDED 1" in out,
          "the same two keys written as UTF-8 stay two keys, and the "
          "stranded one is found")

    huge = tmpfile("huge.csv", "KEY,STAMP\nA," + "x" * 200000 + "\n")
    code, out, err = run_cli(
        ["safemark", "--source", huge, "--applied", app_csv,
         "--old-watermark", "2026-03-01", "--new-watermark", "2026-03-02"])
    check(code == 2 and "field larger than field limit" in err
          and "Traceback" not in err,
          "a field over the csv limit exits 2 on every Python version")
    empty_csv = tmpfile("zero.csv", "")
    code, out, err = run_cli(
        ["safemark", "--source", empty_csv, "--applied", app_csv,
         "--old-watermark", "2026-03-01", "--new-watermark", "2026-03-02"])
    check(code == 64 and "no columns at all" in err,
          "a zero-byte file is a usage error, exit 64")
    tabbed = tmpfile("tab.csv", "KEY\tSTAMP\nA\t2026-03-01 01:00:00\n")
    code, out, err = run_cli(
        ["safemark", "--source", tabbed, "--applied", app_csv,
         "--old-watermark", "2026-03-01", "--new-watermark", "2026-03-02"])
    check(code == 64 and "columns not in the CSV" in err,
          "a tab-delimited export is a usage error, exit 64")

    # ---- the CLI: files that are not what they seem
    def sm_bytes(name, data):
        path = os.path.join(tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return run_cli(["safemark", "--source", path, "--applied", app_csv,
                        "--old-watermark", "2026-03-01", "--new-watermark",
                        "2026-03-02"])

    code, out, err = sm_bytes("u16.csv", "KEY,STAMP\n".encode("utf-16"))
    check(code == 64 and "looks like a UTF-16 file" in err,
          "a UTF-16 export is a usage error, exit 64, on every Python "
          "version")
    code, out, err = sm_bytes("semi.csv", b"KEY;STAMP\nA;2026-03-01\n")
    check(code == 64 and "columns not in the CSV" in err,
          "a semicolon-delimited export is a usage error, exit 64")
    code, out, err = sm_bytes(
        "esri-dates.csv", b"KEY,STAMP\nA,3/1/2026 1:00:00 AM\n"
        b"B,1772326800000\n")
    check(code == 4 and "UNDATED 2" in out,
          "stamps in an Esri or spreadsheet style, and epoch milliseconds, "
          "are UNDATED and the run exits 4, never 0")
    code, out, err = sm_bytes(
        "lines.csv", b'KEY,STAMP\nA,"2026-03-01 01:00:00"\n\n'
        b'B,\n"C\nD",\n')
    check("row 2  key B" in out and "row 3  key C" in out,
          "row numbers count CSV records, not file lines: a blank line and a "
          "quoted newline do not shift them")
    code, out, err = sm_bytes("esc.csv", b"KEY,STAMP\nA\x1b[31m,\n")
    check("A\x1b[31m" in out,
          "control characters in a key are printed as they are")

    class ClosedPipe(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("probe")

    err_buf = io.StringIO()
    with contextlib.redirect_stdout(ClosedPipe()), \
            contextlib.redirect_stderr(err_buf):
        code = main(fz)
    check(code == 70 and "BrokenPipeError" in err_buf.getvalue(),
          "a closed stdout ends the run with exit 70 and no verdict, never a "
          "finding")

    probe_path = tmpfile("flush-probe.txt", "")
    probe_fd = os.open(probe_path, os.O_WRONLY | getattr(os, "O_BINARY", 0))

    class DeadAtFlush(io.StringIO):
        def flush(self):
            raise OSError(22, "probe")

        def fileno(self):
            return probe_fd

    err_buf = io.StringIO()
    with contextlib.redirect_stdout(DeadAtFlush()), \
            contextlib.redirect_stderr(err_buf):
        code = main(fz)
    os.write(probe_fd, b"x")
    os.close(probe_fd)
    check(code == 70 and "OSError" in err_buf.getvalue()
          and os.path.getsize(probe_path) == 0,
          "a pipe that dies at the flush after a short report exits 70 and "
          "points stdout at the null device, so the exit flush cannot turn "
          "it into 120  <-- pinned defect")
    with contextlib.redirect_stdout(None), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(fz + ["--view-max", "2026-03-01 08:30:00"])
    check(code == 1,
          "a run with no stdout at all still returns its exit code")
    read_end, write_end = os.pipe()
    os.close(read_end)
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__)] + fz,
        stdout=write_end, stderr=subprocess.PIPE)
    os.close(write_end)
    check(child.returncode == 70,
          "a real child whose stdout pipe is already closed exits 70, not "
          "120  <-- pinned defect")

    # ---- the CLI: verdicts that must not read as clean
    def sm(src_text, app_text, new_mark, old_mark="2026-02-01 00:00:00"):
        src_path = tmpfile("v-src.csv", src_text)
        app_path = tmpfile("v-app.csv", app_text)
        return run_cli(["safemark", "--source", src_path, "--applied",
                        app_path, "--old-watermark", old_mark,
                        "--new-watermark", new_mark])

    code, out, err = sm("KEY,STAMP\n,2026-03-01 01:00:00\n"
                        ",2026-03-01 02:00:00\n,2026-03-01 03:00:00\n",
                        "KEY,STAMP\n,2026-03-01 03:00:00\n"
                        "Z,2026-03-01 00:30:00\n", "2026-03-01 03:00:00")
    check(code == 4 and "APPLIED 0" in out and "UNDATED 3" in out
          and "SAFE  " not in out,
          "blank keys in source and applied never collapse into APPLIED: "
          "exit 4, not a false SAFE  <-- pinned defect")
    code, out, err = sm("KEY,STAMP\n,2026-03-01 01:00:00\n",
                        "KEY,STAMP\n,2026-03-01 03:00:00\n",
                        "2026-03-01 03:00:00")
    check(code == 4 and "none with a readable key and stamp" in out,
          "an applied file whose every key is blank compares nothing")
    code, out, err = sm("KEY,STAMP\nA,2026-03-01 01:00:00\nB,\n",
                        "KEY,STAMP\nA,2026-03-01 01:00:00\n",
                        "2026-03-01 01:00:00")
    check(code == 1 and "UNDATED" in out and "SAFE  " not in out,
          "an UNDATED row alone makes the run exit 1  <-- pinned defect")
    code, out, err = sm("KEY,STAMP\nA,2026-03-01 01:00:00\n",
                        "KEY,STAMP\nA,2026-03-01 01:00:00\n",
                        "2026-03-05")
    check(code == 1 and "AHEAD" in out and "STRANDED 0" in out
          and "SAFE  " not in out,
          "an AHEAD mark alone makes the run exit 1  <-- pinned defect")
    code, out, err = sm("KEY,STAMP\nA,2026-03-01 01:00:00\n"
                        "B,2026-03-01 02:00:00\nD,2026-03-01 04:30:00\n",
                        "KEY,STAMP\nA,2026-03-01 01:00:00\n",
                        "2026-03-01 04:00:00", "2026-03-01 03:00:00")
    check(code == 1 and "OUTSIDE" in out and "1900-01-01" in out
          and "VERDICT: 1 finding(s)" in out and "SAFE  " not in out,
          "a row skipped by an earlier run makes the run exit 1, not a "
          "clean verdict  <-- pinned defect")
    code, out, err = sm("KEY,STAMP\nA,2026-03-01 01:00:00\n"
                        "B,2026-03-01 02:00:00\n",
                        "KEY,STAMP\nA,3/1/2026 1:10:00 AM\n"
                        "B,3/1/2026 2:05:00 AM\n", "2026-03-01 06:00:00")
    check(code == 4 and "STRANDED" not in out and "date format" in out
          and "nothing was compared" in out,
          "an applied file in an unread date format exits 4, not a false "
          "STRANDED finding  <-- pinned defect")
    code, out, err = sm("KEY,STAMP\n", "KEY,STAMP\n", "2026-03-01")
    check(code == 4 and "SAFE  " not in out and "nothing was compared" in out,
          "a header-only source exits 4 and prints no SAFE line")

    # ---- the CLI: usage
    check(run_cli([])[0] == 64, "no mode and no --self-test is a usage error")
    code, out, err = run_cli([])
    check("name a mode" in err and out == "",
          "and the message says what to do, with nothing on stdout")
    check(run_cli(["frobnicate"])[0] == 64, "an unknown mode is refused")
    code, out, err = run_cli(fz + ["--source", src_csv, "--apply"])
    check(code == 64
          and "--source, --apply cannot be used with frozenmark" in err,
          "a safemark flag given to frozenmark is refused, not ignored  "
          "<-- pinned defect")
    code, out, err = run_cli(sw + ["--base-max", "2026-01-01"])
    check(code == 64 and "--base-max cannot be used with safemark" in err,
          "a frozenmark flag given to safemark is refused")
    code, out, err = run_cli(sw + ["--max-age-hours", "0"])
    check(code == 64 and "--max-age-hours cannot be used with safemark"
          in err,
          "and a zero value counts as given: 0 is not False  "
          "<-- pinned defect")
    code, out, err = run_cli(fz + ["--sample", "0"])
    check(code == 64 and "--sample cannot be used with frozenmark" in err,
          "--sample 0 given to frozenmark is refused too  <-- pinned defect")
    code, out, err = run_cli(["safemark", "--source", src_csv])
    check(code == 64 and "--applied, --old-watermark, --new-watermark" in err,
          "every missing safemark flag is listed at once")
    code, out, err = run_cli(["--help"])
    check(code == 0 and "frozenmark" in out, "--help exits 0 and prints")

    # ---- allow_abbrev=False: a prefix of a flag is not that flag
    for prefix in ("--ap", "--app", "--appl", "--ou"):
        code, out, err = run_cli(sw + ["--out", dest + "x", prefix])
        check(code == 64 and not os.path.exists(dest + "x"),
              "%s, a prefix of a write flag, is refused and writes nothing"
              "  <-- pinned defect" % prefix)
    check(run_cli(sw + ["--out", dest + "x", "--appl", "x"])[0] == 64
          and not os.path.exists(dest + "x"),
          "a prefix with a value is refused too")
    short = sw[:]
    short[short.index("--source")] = "--sour"
    check(run_cli(short)[0] == 64,
          "--sour, a prefix of --source, is refused though the run is "
          "otherwise complete")
    short = sw[:]
    short[short.index("--old-watermark")] = "--old"
    check(run_cli(short)[0] == 64, "and --old is not --old-watermark")
    short = fz[:]
    short[short.index("--base-max")] = "--base"
    check(run_cli(short)[0] == 64, "and --base is not --base-max")
    check(run_cli(fz + ["--max", "5"])[0] == 64,
          "and --max is not --max-age-hours")

    # ---- --self-test is handed through, and ignores the other flags
    real_self_test = globals()["self_test"]
    try:
        globals()["self_test"] = lambda: 7
        handed = run_cli(["--self-test", "--apply"])[0]
    finally:
        globals()["self_test"] = real_self_test
    check(handed == 7,
          "main returns the exit status of --self-test, so a red self-test "
          "fails CI  <-- pinned defect")

    # ---- a character the console cannot encode
    calls = []

    class Reconfigurable(io.StringIO):
        def reconfigure(self, **kw):
            calls.append(kw)

    with contextlib.redirect_stdout(Reconfigurable()), \
            contextlib.redirect_stderr(Reconfigurable()):
        main(["--help"])
    check(calls == [{"errors": "replace"}] * 2,
          "main reconfigures stdout and stderr to replace what they cannot "
          "encode")
    uni = tmpfile("uni.csv", "KEY,STAMP\nH-" + chr(0x141)
                  + " 1,2026-03-01 02:00:00\n")
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "safemark", "--source",
         uni, "--applied", ok_app, "--old-watermark", "2026-03-01 00:00:00",
         "--new-watermark", "2026-03-01 02:00:00"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="ascii"))
    check(child.returncode == 1 and b"H-? 1" in child.stdout
          and b"Traceback" not in child.stderr
          and b"VERDICT: 1 finding(s)" in child.stdout,
          "a key the console cannot encode prints as ? and the verdict "
          "still arrives, instead of a traceback and exit 1  "
          "<-- pinned defect")

    # ---- a crash is not a finding
    real_judge = globals()["judge_safe"]

    def boom(*args, **kw):
        raise RuntimeError("probe")

    try:
        globals()["judge_safe"] = boom
        code, out, err = run_cli(sw)
    finally:
        globals()["judge_safe"] = real_judge
    check(code == 70 and "unexpected RuntimeError: probe" in err
          and "Traceback" in err and "VERDICT" not in out,
          "an unexpected exception exits 70, not the 1 that means a "
          "finding  <-- pinned defect")

    # ---- the temp directory goes even when the body raises
    seen = []

    def exploding(path):
        seen.append(path)
        raise RuntimeError("probe")

    real_body = globals()["_self_test"]
    try:
        globals()["_self_test"] = exploding
        try:
            self_test()
        except RuntimeError:
            seen.append("raised")
    finally:
        globals()["_self_test"] = real_body
    check(len(seen) == 2 and seen[1] == "raised"
          and not os.path.exists(seen[0]),
          "a self-test that raises still removes its temp directory  "
          "<-- pinned defect")

    # ---- claims about the source file itself
    with open(os.path.abspath(__file__), "rb") as handle:
        raw = handle.read()
    tree = ast.parse(raw.decode("utf-8", "replace"))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.add(node.module.split(".")[0])
    check(max(raw) < 128, "the source is pure ASCII")
    check(not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)),
          "the source has no f-string, so Python 3.9 reads it")
    check(not imported_names & {"socket", "http", "urllib", "ssl", "smtplib",
                                "ftplib", "requests"},
          "the module imports no network library, so it has no connection "
          "to leak a credential from  <-- pinned defect")
    parsers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and getattr(n.func, "attr", "") == "ArgumentParser"]
    check(len(parsers) == 1 and any(
        k.arg == "allow_abbrev" and k.value.value is False
        for k in parsers[0].keywords),
          "the one parser sets allow_abbrev=False")
    inside_test = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_self_test":
            inside_test.update(id(n) for n in ast.walk(node))

    def write_mode(call):
        modes = list(call.args[1:2]) + [k.value for k in call.keywords
                                        if k.arg == "mode"]
        return any(not (isinstance(m, ast.Constant)
                        and isinstance(m.value, str))
                   or set(m.value) & set("wxa+") for m in modes)

    writers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and id(n) not in inside_test
               and (getattr(n.func, "id", "") == "open" and write_mode(n)
                    or getattr(n.func, "attr", "") in ("write_text",
                                                       "write_bytes"))]
    check(len(writers) == 1 and writers[0].args[1].value == "x",
          "outside the self-test the module opens a file for writing in "
          "exactly one place, the exclusive report writer  <-- pinned "
          "defect")
    check(write_mode(ast.parse("open(p, mode='ab')", mode="eval").body)
          and write_mode(ast.parse("open(p, 'w+')", mode="eval").body)
          and write_mode(ast.parse("open(p, m)", mode="eval").body)
          and not write_mode(ast.parse("open(p, 'rb')", mode="eval").body),
          "the write detector sees ab, w+ and a mode it cannot read, and "
          "not rb")

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass, which is the one failure no other
    # assertion here could see. The probe's own output is swallowed so a green
    # run prints no FAIL line.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned "
          "defect")
    raises(lambda: (_ for _ in ()).throw(ValueError("x")),
           "and raises() accepts the ValueError it is looking for")

    def summary(passed_count, failures):
        print("-" * 68)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for name in failures:
                print("  FAILED: %s" % name)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    # The footer on its red path. A footer that could only print 0 failed
    # would hide every FAIL line above it from anyone who reads the last line.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1"
          "  <-- pinned defect")

    # ---- importing the module runs nothing.
    spec = importlib.util.spec_from_file_location(
        "wmguard_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    # No bytecode cache, so the self-test writes nothing outside its own
    # temporary directory.
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.gate([], 1) == 0,
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


if __name__ == "__main__":
    sys.exit(main())
