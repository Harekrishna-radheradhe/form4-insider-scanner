"""Merged from PA production drift (2026-07-22): the form4 scanners log each fired
signal to the shared signal_intelligence.db. Two visibility guarantees, per the PM
rider on ruling #13:

  1. A signal_log write failure is surfaced ([SIGNAL_LOG_FAIL]), never swallowed.
  2. If options_volume_check goes missing, it ANNOUNCES itself loudly
     ([OPTIONS_CHECK_UNAVAILABLE]) instead of degrading silently, and the call site
     yields a clear "filter disabled" message rather than a cryptic swallowed error.
"""
import importlib
import sys

import main
import cross_signal_scanner


def test_main_signal_log_failure_is_printed(capsys, monkeypatch):
    monkeypatch.setattr(main.os.path, "expanduser",
                        lambda p: "/does/not/exist/nope/signal_intelligence.db")
    main.log_signal_intelligence("2026-07-22", "F4_BUY_CLUSTER", "ABC", "BUY", 1)
    out = capsys.readouterr().out
    assert "[SIGNAL_LOG_FAIL]" in out
    assert "F4_BUY_CLUSTER" in out and "ABC" in out


def test_cross_signal_signal_log_failure_is_printed(capsys, monkeypatch):
    monkeypatch.setattr(cross_signal_scanner.os.path, "expanduser",
                        lambda p: "/does/not/exist/nope/signal_intelligence.db")
    cross_signal_scanner.log_signal_intelligence("2026-07-22", "F4_CROSS_TIER2", "XYZ", "BUY", 1)
    out = capsys.readouterr().out
    assert "[SIGNAL_LOG_FAIL]" in out
    assert "F4_CROSS_TIER2" in out and "XYZ" in out


def test_missing_options_check_announces_itself(capsys):
    # Force `from options_volume_check import ...` to fail, reload main, and assert the
    # import failure is announced loudly (not silent) and the filter flag is False.
    saved = sys.modules.get("options_volume_check")
    sys.modules["options_volume_check"] = None  # a None entry makes the import raise
    try:
        importlib.reload(main)
        out = capsys.readouterr().out
        assert "[OPTIONS_CHECK_UNAVAILABLE]" in out
        assert main.OPTIONS_CHECK_AVAILABLE is False
    finally:
        if saved is not None:
            sys.modules["options_volume_check"] = saved
        else:
            sys.modules.pop("options_volume_check", None)
        importlib.reload(main)


def test_call_site_guards_against_missing_module():
    # The contamination call must be gated on the availability flag so a missing
    # module produces a clear "filter disabled" error, not an undefined-name crash.
    for mod in (main, cross_signal_scanner):
        src = open(mod.__file__).read()
        assert "if not OPTIONS_CHECK_AVAILABLE:" in src
        assert "Phase-4 filter disabled" in src
