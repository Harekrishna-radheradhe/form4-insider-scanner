"""SCANNER-HYGIENE: the run-level log_scan_run() write to the shared
signal-intelligence DB must be LOUD on failure ([SCAN_RUN_FAIL] <scanner>: ...)
yet NON-RAISING. (The per-signal signal_log path is already loud and is not
covered here.)
"""
import main


def test_scan_run_write_failure_is_printed(capsys):
    # A bad db_path forces the run-level write to fail.
    main.log_scan_run("FORM4", "OK", 1, db_path="/does/not/exist/nope/x.db")
    out = capsys.readouterr().out
    assert "[SCAN_RUN_FAIL]" in out
    assert "FORM4" in out


def test_scan_run_write_failure_does_not_raise():
    # Must return normally despite the write failing.
    main.log_scan_run("FORM4", "OK", 1, db_path="/does/not/exist/nope/x.db")
