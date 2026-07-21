"""
ITEM 2: run-level logging. Every scanner run must write exactly ONE row to the
shared signal-intelligence DB's `scan_runs` table — even when the input source
is empty (n_evaluated=0) — so a silent 'ran but evaluated nothing' run is
distinguishable from 'did not run at all'. The helper must never raise.
"""
import sqlite3
import pytest

import main


def _rows(db_path):
    c = sqlite3.connect(db_path)
    try:
        cur = c.execute(
            'SELECT scanner, source_status, n_evaluated, n_fired, note FROM scan_runs')
        return cur.fetchall()
    finally:
        c.close()


def test_log_scan_run_writes_exactly_one_row(tmp_path):
    dbp = str(tmp_path / 'signal_intelligence.db')
    main.log_scan_run('FORM4', 'OK', 12, n_fired=2, note='nightly', db_path=dbp)
    rows = _rows(dbp)
    assert len(rows) == 1
    assert rows[0] == ('FORM4', 'OK', 12, 2, 'nightly')


def test_log_scan_run_empty_source_still_logs(tmp_path):
    """Empty/failed source (n_evaluated=0) must still produce a row."""
    dbp = str(tmp_path / 'signal_intelligence.db')
    main.log_scan_run('FORM4', 'EMPTY', 0, db_path=dbp)
    rows = _rows(dbp)
    assert len(rows) == 1
    assert rows[0] == ('FORM4', 'EMPTY', 0, 0, '')


def test_log_scan_run_one_row_per_call(tmp_path):
    dbp = str(tmp_path / 'signal_intelligence.db')
    main.log_scan_run('FORM4', 'OK', 5, db_path=dbp)
    main.log_scan_run('FORM4', 'FETCH_FAIL', 0, note='edgar error', db_path=dbp)
    rows = _rows(dbp)
    assert len(rows) == 2
    assert rows[0][1] == 'OK'
    assert rows[1][1] == 'FETCH_FAIL'


def test_log_scan_run_never_raises(tmp_path):
    """A bad db_path must be swallowed, never propagated."""
    bad = str(tmp_path / 'no_such_dir' / 'nested' / 'x.db')
    # Must not raise despite the unwritable path.
    main.log_scan_run('FORM4', 'OK', 1, db_path=bad)
