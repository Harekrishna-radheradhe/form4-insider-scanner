"""Task 1(c), JOB-20260804 (R10). The Tier-2 cross scanner's run heartbeat moves
out of `signal_log` and into `scan_runs`.

The heartbeat itself is not new. It was written straight onto PythonAnywhere on
2026-07-27 as a sentinel row `ticker='__RUN__'` in `signal_log`, for a reason
stated in that code and correct when written: `scan_runs` "lives in each scanner's
own DB and does NOT reach the Studio". That premise is now false — every scanner
writes `scan_runs` into the SHARED `~/signal_intelligence.db`, and from 2026-08-04
the Studio sync merges it — and the sentinel had a cost nobody had priced:

  * `direction` is NULL on a sentinel row; SQLite treats NULLs as DISTINCT in a
    UNIQUE index; so the Studio's `INSERT OR IGNORE` re-inserted the row on every
    sync until the duplicate tripped the sync's own dedup pre-check. That deadlock
    took the whole PA->Studio pipeline down from 07-30 to 08-04.
  * A run record shaped like a signal will eventually be read as one.

Same guarantee, right table.
"""
import sqlite3

import cross_signal_scanner as css


def _runs(db_path):
    c = sqlite3.connect(db_path)
    try:
        return c.execute(
            'SELECT scanner, source_status, n_evaluated, n_fired, note FROM scan_runs'
        ).fetchall()
    finally:
        c.close()


def test_the_run_heartbeat_lands_in_scan_runs(tmp_path):
    dbp = str(tmp_path / 'signal_intelligence.db')
    css.log_scan_run('F4_CROSS_TIER2', 'OK', 12, n_fired=2, note='', db_path=dbp)
    assert _runs(dbp) == [('F4_CROSS_TIER2', 'OK', 12, 2, '')]


def test_a_night_with_nothing_to_evaluate_still_records_the_run(tmp_path):
    """The whole point of a run record: 'ran, evaluated nothing' must be
    distinguishable from 'did not run at all'."""
    dbp = str(tmp_path / 'signal_intelligence.db')
    css.log_scan_run('F4_CROSS_TIER2', 'OK', 0, db_path=dbp)
    assert _runs(dbp) == [('F4_CROSS_TIER2', 'OK', 0, 0, '')]


def test_one_row_per_call(tmp_path):
    dbp = str(tmp_path / 'signal_intelligence.db')
    for _ in range(3):
        css.log_scan_run('F4_CROSS_TIER2', 'OK', 1, db_path=dbp)
    assert len(_runs(dbp)) == 3


def test_the_run_heartbeat_writes_no_signal_log_row(tmp_path):
    """A run must leave nothing in `signal_log`. That table records what was
    FOUND, and a heartbeat found nothing — which is exactly the confusion being
    removed."""
    dbp = str(tmp_path / 'signal_intelligence.db')
    css.log_scan_run('F4_CROSS_TIER2', 'OK', 0, db_path=dbp)
    c = sqlite3.connect(dbp)
    try:
        present = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_log'"
        ).fetchone()
    finally:
        c.close()
    assert present is None, "a run heartbeat must not touch signal_log at all"


def test_the_sentinel_is_gone_from_the_module():
    """Named explicitly so the retirement cannot be quietly undone: the sentinel
    is what deadlocked the sync, and an edit reintroducing it should turn this
    suite red rather than the pipeline."""
    assert not hasattr(css, 'RUN_SENTINEL_TICKER')
    with open(css.__file__) as fh:
        assert '__RUN__' not in fh.read()


def test_a_write_failure_is_surfaced_never_swallowed(capsys):
    css.log_scan_run('F4_CROSS_TIER2', 'OK', 0,
                     db_path='/nonexistent-dir-for-this-test/x.db')
    assert '[SCAN_RUN_FAIL]' in capsys.readouterr().out


def test_the_helper_never_raises():
    css.log_scan_run('F4_CROSS_TIER2', 'OK', 0, db_path='/nope/nope.db')
