"""Item 2b (JOB-20260727-MONDAY): a nightly RUN record even on 0 signals.

**The defect.** `cross_signal_scanner` writes a `signal_log` row only when a
Tier-2 cross FIRES. `ops/obs/pa_watch.py:111` (on the Studio) judges collector
liveness with `SELECT scanner, MAX(scan_date) FROM signal_log GROUP BY scanner`.
So the board reads *"last time it found something"* and reports it as *"last time
it ran"* — and for a fired=0 design those are entirely different questions.

Measured on the Studio 2026-07-27, read-only: `F4_CROSS_TIER2` `MAX(scan_date)`
= 2026-06-11, **13 rows in its whole history**, all `fired=1`; the live
`pa_watch.json` (sequence 1) held `state: ALARM` naming it 46 days stale while
the PA task log showed nightly runs at return code 0 since the 07-20 re-enable.
Alive and quiet was indistinguishable from dead.

**Why a `fired=0` row is the smallest HONEST mechanism, not a hack.** The
`fired` column already exists for exactly this distinction, and `fired=0` rows
are already the convention on 10 of the farm's 14 real scanners (8K_1.01,
CEL_BEAR, COT_BEAR, DIV_CUT, DIV_INITIATION, F4_SELL_WATCH, PEAD_BEAR,
PEAD_BULL, SI_SQUEEZE, THIRTEENF_BULL — counted on the synced DB). The five that
never write one are the four `F4_*` scanners and `COT_BULL`. This brings
F4_CROSS_TIER2 into line with the majority convention.

It also has to be a RUN row rather than per-candidate `fired=0` rows: on a night
with zero Tier-2 candidates the per-candidate loop is empty, so per-candidate
logging cannot help in the exact case that matters. Hence one explicit sentinel
row per completed scan.

**`signal_log` is the only table that can serve this.** The synced
`signal_intelligence.db` on the Studio contains exactly two tables —
`signal_log` and `ticker_sigma_history`. The per-scanner `scan_runs` heartbeat
added on 07-22 lives in each scanner's own DB and does **not** reach the Studio,
so it cannot feed pa_watch.

**Anti-vacuous (constitution A6).** A liveness record must never be written for a
scan that could not read its input. `get_recent_purchases` returns `[]` both for
"DB missing" (broken) and "DB present, nothing to see" (healthy), so the run
record is gated on the source DB existing — otherwise a broken scanner would
publish proof of life every night.
"""
import sqlite3
from datetime import datetime, timezone

import cross_signal_scanner as css

MONDAY = datetime(2026, 7, 27, 22, 15, tzinfo=timezone.utc)
SATURDAY = datetime(2026, 7, 25, 22, 15, tzinfo=timezone.utc)


class _Pinned:
    """Stand-in for the module's `datetime` with `now()` pinned.

    `now(timezone.utc)` yields the aware instant (the weekend guard and the email
    stamp use it); bare `now()` yields it naive, which is what the existing
    `scan_date` expression uses.
    """
    pinned = MONDAY

    @classmethod
    def now(cls, tz=None):
        return cls.pinned if tz is not None else cls.pinned.replace(tzinfo=None)


def _wire(monkeypatch, tmp_path, *, when=MONDAY, source_db=True, purchases=None):
    """Point the signal DB at a temp file, pin the clock, and stub the source read.

    Returns the signal-DB path. `purchases` defaults to ONE non-Tier-2 purchase —
    a real scan that legitimately produced zero Tier-2 signals, which is the
    fired=0 night this whole test module is about.
    """
    signal_db = tmp_path / "signal_intelligence.db"
    monkeypatch.setattr(css.os.path, "expanduser", lambda p: str(signal_db))

    pinned = type("P", (_Pinned,), {"pinned": when})
    monkeypatch.setattr(css, "datetime", pinned)

    if source_db:
        src = tmp_path / "form4_insider_trades.db"
        src.write_bytes(b"")                       # exists = readable input
        monkeypatch.setattr(css, "DB_PATH", str(src))
    else:
        monkeypatch.setattr(css, "DB_PATH", str(tmp_path / "absent.db"))

    if purchases is None:
        purchases = [{
            "ticker": "QUIET", "insider_name": "A Person",
            "insider_title": "Director",               # NOT C-suite -> not Tier 2
            "transaction_date": "2026-07-27", "shares": 10,
            "price_per_share": 1.0, "total_value": 10.0,
        }]
    monkeypatch.setattr(css, "get_recent_purchases", lambda *a, **k: list(purchases))
    monkeypatch.setattr(css, "send_email", lambda *a, **k: True)
    return signal_db


def _rows(signal_db):
    if not signal_db.exists():
        return []
    con = sqlite3.connect(str(signal_db))
    try:
        return con.execute(
            "SELECT scan_date, scanner, ticker, direction, fired, signal_bucket "
            "FROM signal_log ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _pa_watch_view(signal_db):
    """pa_watch's own query, verbatim from ops/obs/pa_watch.py:111."""
    con = sqlite3.connect(str(signal_db))
    try:
        return dict(con.execute(
            "SELECT scanner, MAX(scan_date) FROM signal_log "
            "WHERE scanner IS NOT NULL GROUP BY scanner"
        ).fetchall())
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# The fix                                                                     #
# --------------------------------------------------------------------------- #

def test_a_zero_tier2_night_still_publishes_a_run_record(monkeypatch, tmp_path):
    """THE defect. A scan that ran and fired nothing must still say it ran."""
    signal_db = _wire(monkeypatch, tmp_path)

    css.main()

    rows = _rows(signal_db)
    assert rows, "a completed zero-Tier2 scan wrote NOTHING — the fired=0 night is invisible"
    runs = [r for r in rows if r[2] == css.RUN_SENTINEL_TICKER]
    assert len(runs) == 1, f"expected exactly one RUN record, got {runs}"
    scan_date, scanner, _ticker, _dir, fired, bucket = runs[0]
    assert scanner == "F4_CROSS_TIER2"
    assert scan_date == "2026-07-27"
    assert fired == 0, "a RUN record must not claim a signal fired"
    assert bucket == "RUN"


def test_the_run_record_is_what_pa_watch_actually_reads(monkeypatch, tmp_path):
    """Acceptance shape: pa_watch's own query must see today's date.

    This is the test that proves the fix reaches the board rather than merely
    landing in a table. Run against pa_watch's verbatim SQL.
    """
    signal_db = _wire(monkeypatch, tmp_path)

    css.main()

    assert _pa_watch_view(signal_db).get("F4_CROSS_TIER2") == "2026-07-27"


def test_a_firing_night_records_both_the_signal_and_the_run(monkeypatch, tmp_path):
    """The run record is additive — it must not displace the fired=1 rows."""
    tier2 = [{
        "ticker": "LOUD", "insider_name": "A Chief",
        "insider_title": "Chief Executive Officer",
        "transaction_date": "2026-07-27", "shares": 1000,
        "price_per_share": 1000.0, "total_value": 1_000_000.0,
    }]
    signal_db = _wire(monkeypatch, tmp_path, purchases=tier2)
    # Stop the run before the network legs; the two log writes are already done.
    monkeypatch.setattr(css, "fetch_short_interest", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("stop here — SI fetch is a network leg")))

    try:
        css.main()
    except RuntimeError:
        pass

    rows = _rows(signal_db)
    fired = [r for r in rows if r[4] == 1]
    runs = [r for r in rows if r[2] == css.RUN_SENTINEL_TICKER]
    assert len(fired) == 1 and fired[0][2] == "LOUD"
    assert len(runs) == 1, "a firing night must ALSO record that the scan ran"


# --------------------------------------------------------------------------- #
# Anti-vacuous rails                                                          #
# --------------------------------------------------------------------------- #

def test_no_run_record_when_the_source_db_is_missing(monkeypatch, tmp_path, capsys):
    """A6. A scan that could not read its input must not publish proof of life.

    `get_recent_purchases` returns `[]` for a missing DB and for a genuinely
    empty one alike, so without this gate a permanently broken scanner would look
    alive every single night — the precise vacuous pass A6 forbids.
    """
    signal_db = _wire(monkeypatch, tmp_path, source_db=False, purchases=[])

    css.main()

    assert not [r for r in _rows(signal_db) if r[2] == css.RUN_SENTINEL_TICKER], \
        "wrote a liveness record for a scan whose source DB was absent"
    assert "[SCAN_RUN_SKIPPED]" in capsys.readouterr().out, \
        "skipping the run record must be LOUD, not silent"


def test_the_weekend_skip_writes_no_run_record(monkeypatch, tmp_path):
    """It returns before scanning, so it must not claim to have scanned.

    Weekday-only run records stay inside pa_watch's 7-day freshness window
    (Friday -> Monday is 3 days), so honesty here costs no board coverage.
    """
    signal_db = _wire(monkeypatch, tmp_path, when=SATURDAY)

    css.main()

    assert _rows(signal_db) == [], "claimed a scan on a day it deliberately skipped"


def test_a_run_record_write_failure_is_loud(monkeypatch, capsys):
    """The run record rides the existing [SIGNAL_LOG_FAIL] visibility contract."""
    monkeypatch.setattr(css.os.path, "expanduser",
                        lambda p: "/does/not/exist/nope/signal_intelligence.db")

    css.log_scan_run("2026-07-27")

    out = capsys.readouterr().out
    assert "[SIGNAL_LOG_FAIL]" in out
    assert "F4_CROSS_TIER2" in out


def test_the_run_sentinel_cannot_collide_with_a_real_ticker():
    """A sentinel a ticker could equal would silently corrupt signal rows."""
    assert css.RUN_SENTINEL_TICKER.startswith("__")
    assert not css.RUN_SENTINEL_TICKER.isalnum()
