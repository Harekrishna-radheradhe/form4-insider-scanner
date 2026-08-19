"""JOB-20260819-FORM4-BACKFILL-EMAIL-FIX Task 2: rows-added delta + zero-insert flag.

**The defect (R34.3).** The 2026-08-17 cycle inserted zero rows and the status
email read normal, because the status email carries only RUNNING TOTALS. On
08-17 it reported `Total Transactions: 11,351` — the same number a healthy night
would show, and the same number the next morning would show. `main.py` already
computes the per-cycle delta: `fetch_and_store_filings()` returns
`transactions_added`, and `main.send_status_report()` packs it into a
`status_data` dict at line 349 — then calls `reporter.send_status_report(stats)`
and throws `status_data` away. The counter exists; nothing ever renders it.

So a night that ingests nothing is INDISTINGUISHABLE from a night that ingests
normally. That is the invisible-failure shape, and it is what these tests pin.

**Two things are required, and they are not the same thing.**

1. A `rows_added` delta line — what THIS cycle inserted, not the running total.
2. A loud, subject-line-level flag when that delta is zero on a day the scanner
   was supposed to work.

**Why the flag is weekday-gated, and why that gate lives HERE.** The scanner
fires 22:00 UTC seven days a week; `main.main()` returns early on Sat/Sun
(`datetime.now(timezone.utc).weekday() >= 5`), so a weekend zero-insert is
BY DESIGN — no new EDGAR filings exist to ingest. A flag that fired on weekends
would cry wolf 104 times a year, and an alarm nobody reads is worse than no
alarm: it would become the next invisible-failure mode rather than the cure for
this one. The weekday rule is therefore re-stated in this module rather than
inherited from `main`'s early return — an alarm whose correctness depends on its
caller having already returned is not an alarm, it is a coincidence. Fixture (c)
below exercises the weekend case by calling the email path DIRECTLY, which is
exactly the path `main`'s guard would never reach.

**Why `None` is not zero (constitution A6).** `--status-only` and `--analyze-only`
runs never call the fetch, so their `transactions_added` is *unknown*, not zero.
Rendering "0 rows added" for a cycle that never tried to add any — and alarming
on it — is vacuous. `rows_added=None` must produce no delta line and no flag.
"""
from datetime import datetime, timezone

import pytest

import email_reporter


# 2026-08-17 is the Monday whose zero-insert cycle this job exists to make
# visible; 2026-08-16 is the Sunday before it. Both stamped at 22:00 UTC, the
# scanner's actual fire time, so the UTC weekday under test is the real one.
MONDAY = datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 8, 16, 22, 0, tzinfo=timezone.utc)

STATS = {
    'total_transactions': 11351,
    'total_purchases': 604,
    'total_sells': 10747,
    'unique_companies': 1893,
}


class _Pinned:
    """Stand-in for the module's `datetime` with `now()` pinned.

    Mirrors the idiom already used in `tests/test_scan_run_record.py`:
    `now(tz)` yields the aware instant, bare `now()` yields it naive.
    """
    pinned = MONDAY

    @classmethod
    def now(cls, tz=None):
        return cls.pinned if tz is not None else cls.pinned.replace(tzinfo=None)


@pytest.fixture
def render(monkeypatch):
    """Render a status email and hand back (subject, html, text).

    `_send_email` is stubbed rather than `dry_run=True`: dry-run short-circuits
    before the bodies are handed off, and the WO requires the BODIES be
    inspected, not just the subject.
    """
    def _render(*, when, **kwargs):
        monkeypatch.setattr(
            email_reporter, "datetime",
            type("P", (_Pinned,), {"pinned": when}),
        )
        captured = {}

        def _capture(self, subject, html_content, text_content):
            captured.update(subject=subject, html=html_content,
                            text=text_content)
            return True

        monkeypatch.setattr(email_reporter.EmailReporter, "_send_email",
                            _capture)
        assert email_reporter.EmailReporter().send_status_report(
            STATS, **kwargs) is True
        return captured

    return _render


# ── Fixture (a): a normal weekday cycle, N > 0 ──────────────────────

def test_normal_weekday_cycle_shows_delta_and_no_flag(render):
    out = render(when=MONDAY, rows_added=37, filings_processed=12)

    # The delta is rendered, in both bodies, as THIS cycle's number.
    assert "37" in out["text"]
    assert "Rows Added This Cycle" in out["text"]
    assert "Rows Added This Cycle" in out["html"]

    # The running total still appears — the delta ADDS a line, it replaces
    # nothing. Losing the total would trade one blind spot for another.
    assert "11,351" in out["text"]

    # No flag anywhere.
    assert not out["subject"].startswith("[ZERO-INSERT]")
    assert "ZERO INSERT" not in out["text"].upper()
    assert "ZERO INSERT" not in out["html"].upper()


# ── Fixture (b): a weekday zero-insert cycle — the 08-17 shape ──────

def test_weekday_zero_insert_shows_delta_zero_and_loud_flag(render):
    out = render(when=MONDAY, rows_added=0, filings_processed=0)

    assert "Rows Added This Cycle: 0" in out["text"]

    # Unmissable at the SUBJECT level. A line buried in the body is what the
    # totals already were: present, and never read.
    assert out["subject"].startswith("[ZERO-INSERT]")
    assert "Form 4 Scanner Status" in out["subject"]

    # And carried in both bodies, so the alarm survives a client that renders
    # only one of them.
    assert "ZERO INSERT" in out["text"].upper()
    assert "ZERO INSERT" in out["html"].upper()


# ── Fixture (c): a weekend zero-insert cycle — by design, NOT a fault ──

def test_weekend_zero_insert_shows_delta_zero_and_no_flag(render):
    out = render(when=SUNDAY, rows_added=0, filings_processed=0)

    # The delta is still reported honestly...
    assert "Rows Added This Cycle: 0" in out["text"]

    # ...but zero on a Sunday is the designed outcome, not a fault. No flag.
    assert not out["subject"].startswith("[ZERO-INSERT]")
    assert "ZERO INSERT" not in out["text"].upper()
    assert "ZERO INSERT" not in out["html"].upper()


# ── A6: unknown is not zero ─────────────────────────────────────────

def test_no_fetch_this_run_renders_no_delta_and_no_flag(render):
    """`--status-only` / `--analyze-only`: the fetch never ran, so the delta is
    unknown. Reporting it as 0 and alarming on it would be a vacuous alarm."""
    out = render(when=MONDAY, rows_added=None)

    assert "Rows Added This Cycle" not in out["text"]
    assert "Rows Added This Cycle" not in out["html"]
    assert not out["subject"].startswith("[ZERO-INSERT]")
    assert "ZERO INSERT" not in out["text"].upper()


def test_send_status_report_still_works_with_no_delta_args(render):
    """Back-compat: the added parameters are optional. An existing caller that
    passes only `stats` must keep working exactly as before."""
    out = render(when=MONDAY)

    assert out["subject"] == "Form 4 Scanner Status - 2026-08-17"
    assert "11,351" in out["text"]


# ── The weekday rule itself, tested directly ────────────────────────

@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc), True),   # Mon
    (datetime(2026, 8, 18, 22, 0, tzinfo=timezone.utc), True),   # Tue
    (datetime(2026, 8, 19, 22, 0, tzinfo=timezone.utc), True),   # Wed
    (datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc), True),   # Thu
    (datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc), True),   # Fri
    (datetime(2026, 8, 22, 22, 0, tzinfo=timezone.utc), False),  # Sat
    (datetime(2026, 8, 23, 22, 0, tzinfo=timezone.utc), False),  # Sun
])
def test_zero_insert_alarm_fires_on_weekdays_only(when, expected):
    assert email_reporter._zero_insert_alarm(0, when) is expected
    # A non-zero delta never alarms, on any day.
    assert email_reporter._zero_insert_alarm(5, when) is False
    # An unknown delta never alarms, on any day.
    assert email_reporter._zero_insert_alarm(None, when) is False


# ── The wiring: the counter has to actually REACH the email ─────────
#
# `main.fetch_and_store_filings()` has always returned `transactions_added`, and
# `main.send_status_report()` has always packed it into a `status_data` dict —
# and then called `reporter.send_status_report(stats)`, dropping it. Rendering
# the delta in `email_reporter` fixes nothing until that drop is repaired, so
# these tests pin the hand-off itself.

def _capture_reporter(monkeypatch):
    """Replace `main`'s EmailReporter with one that records the call kwargs."""
    import main

    seen = {}

    class _Recorder:
        def send_status_report(self, stats, **kwargs):
            seen.update(stats=stats, **kwargs)
            return True

    monkeypatch.setattr(main, "EmailReporter", _Recorder)
    monkeypatch.setattr(main, "get_database_stats", lambda: dict(STATS))
    return main, seen


def test_main_passes_the_cycle_delta_to_the_reporter(monkeypatch):
    main, seen = _capture_reporter(monkeypatch)

    main.send_status_report(filings_processed=12, transactions_added=37,
                            clusters_found=[], sells_found=[])

    assert seen["rows_added"] == 37
    assert seen["filings_processed"] == 12


def test_main_passes_a_real_zero_through_as_zero(monkeypatch):
    """The 08-17 case: the fetch RAN and added nothing. That zero must survive
    the hand-off, because it is the whole signal."""
    main, seen = _capture_reporter(monkeypatch)

    main.send_status_report(filings_processed=0, transactions_added=0,
                            clusters_found=[], sells_found=[])

    assert seen["rows_added"] == 0


def test_main_passes_an_unrun_fetch_through_as_none(monkeypatch):
    """`--status-only` / `--analyze-only` never call the fetch. `main` must hand
    over `None`, not the 0 an initialised counter would otherwise supply — that
    0 is the difference between a real alarm and a nightly false one."""
    main, seen = _capture_reporter(monkeypatch)

    main.send_status_report(filings_processed=None, transactions_added=None,
                            clusters_found=[], sells_found=[])

    assert seen["rows_added"] is None
    assert seen["filings_processed"] is None


def test_main_does_not_initialise_the_counter_to_zero(monkeypatch):
    """The initialiser is load-bearing, so it is pinned. If `transactions_added`
    is initialised to 0 and no fetch runs, every `--status-only` invocation
    reports a zero-insert weekday cycle and the alarm is dead on arrival."""
    import inspect
    import main
    src = inspect.getsource(main.main)
    assert "transactions_added = None" in src
    assert "filings_processed = None" in src
