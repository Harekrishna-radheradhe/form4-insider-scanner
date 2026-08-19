"""JOB-20260819-FORM4-BACKFILL-SCRIPT: date-scoped discovery + an idempotent backfill.

**The gap this closes.** Before this WO the scanner had exactly one discovery entry
point — `get_recent_form4_filings(count=100)` against
`browse-edgar?action=getcurrent`, a rolling most-recent feed with **no date
parameter**. It cannot be pointed at a historical day, so the 2026-08-17
collection hole (R34.2: 0 rows, PM-verified live on PA) could not be closed by
the pipeline that created it. `--backfill-date` adds the missing step and
nothing else: everything downstream of discovery is the existing guarded path,
untouched.

**Why the daily index and not a constructed path.** EDGAR publishes
`daily-index/{yyyy}/QTR{n}/form.{yyyymmdd}.idx`, a documented endpoint listing
every accession disseminated that day. Measured 2026-08-19 for 2026-08-17:
**652 distinct Form 4 accessions** (+8 Form 4/A), identical sets from `form.idx`
and `master.idx` with an empty set-difference both ways. Constructing
`primary_doc.xml` paths by guess is the 08-16 failure mode (nine unusable 404s)
and is not done here — every accession comes from the index, and every document
URL comes from the filing's own `index.json` via the existing `get_filing_index`.

**One row per FILER, not per filing.** A Form 4 is filed jointly by the issuer
and each reporting owner, so the index lists the same accession once per filer —
1,386 form-4 rows collapse to 652 filings on 08-17. Discovery must dedupe by
accession or the backfill would fetch every filing two to nine times. The
fixture below is a real slice of the 08-17 index containing a 9-filer accession
and a 2-filer accession precisely to pin that.

**Idempotency, and the window that is NOT closed by the obvious guard.** Read on
disk rather than assumed: `form4_transactions` has **no unique constraint** —
`id INTEGER PRIMARY KEY AUTOINCREMENT` and nothing else — so
`insert_transaction`'s `except sqlite3.IntegrityError` can never fire and a
second insert of the same row DOES duplicate it. The real guard is filing-level:
`processed_filings.accession_number` is a PRIMARY KEY and `is_filing_processed`
gates the loop. That is sufficient for a filing that completed, and NOT
sufficient for one interrupted between its inserts and its
`mark_filing_processed` call — a 652-filing run in a PA console session that can
time out will eventually land in exactly that window. So the backfill also
checks whether the accession already has transaction rows before inserting any,
which closes it without a schema change to a live 11,351-row database.
"""
import json
import sqlite3
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "form.20260817.sample.idx"

# The two real form-4 accessions in the fixture slice, and their filer counts.
ACC_MULTI = "0001104659-26-097885"   # Savers Value Village — 9 filer rows
ACC_PAIR = "0001610717-26-000359"    # 10x Genomics — 2 filer rows
ACC_4A = "0001853109-26-000002"      # a Form 4/A — must NOT be discovered as a 4


# ── Discovery: URL construction and date handling ───────────────────

@pytest.mark.parametrize("date_str,quarter", [
    ("2026-01-05", 1), ("2026-03-31", 1),
    ("2026-04-01", 2), ("2026-06-30", 2),
    ("2026-07-01", 3), ("2026-08-17", 3), ("2026-09-30", 3),
    ("2026-10-01", 4), ("2026-12-31", 4),
])
def test_daily_index_url_uses_the_right_quarter(date_str, quarter):
    from edgar_fetcher import daily_index_url
    url = daily_index_url(date_str)
    assert f"/{quarter}/".replace("/", "/QTR") in url or f"QTR{quarter}/" in url
    assert url.startswith("https://www.sec.gov/Archives/edgar/daily-index/2026/")
    assert url.endswith(f"form.{date_str.replace('-', '')}.idx")


@pytest.mark.parametrize("bad", ["17-08-2026", "2026/08/17", "20260817", "", "not-a-date"])
def test_bad_date_is_rejected_loudly(bad):
    """A malformed date must raise, not silently build a URL that 404s and reads
    as 'no filings that day' — that is the vacuous-success shape (A6)."""
    from edgar_fetcher import daily_index_url
    with pytest.raises(ValueError):
        daily_index_url(bad)


# ── Discovery: parsing the real index slice ─────────────────────────

@pytest.fixture
def fetcher_on_fixture(monkeypatch):
    """An EdgarFetcher whose one network call returns the real 08-17 index slice."""
    import edgar_fetcher as ef

    calls = []

    class _Resp:
        text = FIXTURE.read_text(encoding="latin-1")
        content = FIXTURE.read_bytes()

    def _fake(self, url):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(ef.EdgarFetcher, "_make_request", _fake)
    f = ef.EdgarFetcher()
    f._calls = calls
    return f


def test_discovery_dedupes_filer_rows_into_filings(fetcher_on_fixture):
    filings = fetcher_on_fixture.get_filings_for_date("2026-08-17")

    accs = [f["accession_number"] for f in filings]
    assert sorted(accs) == sorted([ACC_MULTI, ACC_PAIR]), (
        "11 form-4 filer rows must collapse to 2 filings")
    assert len(accs) == len(set(accs)), "no accession may appear twice"


def test_discovery_excludes_other_form_types_including_4A(fetcher_on_fixture):
    """The fixture carries a 4/A, an 8-K and a 3. Only exact form type 4 counts:
    R34.4(a) authorises 'Form 4s', and an amendment is a different form."""
    filings = fetcher_on_fixture.get_filings_for_date("2026-08-17")
    accs = {f["accession_number"] for f in filings}

    assert ACC_4A not in accs
    assert all(f.get("form_type") == "4" for f in filings)


def test_form_type_is_selectable_so_4A_needs_no_new_code(fetcher_on_fixture):
    """4/A is a known, separate, not-yet-backfilled gap. Closing it later must be
    an argument, not another WO's worth of code."""
    filings = fetcher_on_fixture.get_filings_for_date("2026-08-17", form_type="4/A")
    assert [f["accession_number"] for f in filings] == [ACC_4A]


def test_discovery_returns_the_shape_the_existing_path_consumes(fetcher_on_fixture):
    """`main.fetch_and_store_filings` reads `cik`, `accession_number` and
    `updated[:10]`. Discovery must hand back that same dict, not a new format."""
    filings = fetcher_on_fixture.get_filings_for_date("2026-08-17")
    f = next(x for x in filings if x["accession_number"] == ACC_PAIR)

    assert f["cik"] and str(f["cik"]).isdigit()
    assert f["updated"][:10] == "2026-08-17"
    assert f["company_name"]
    assert set(f) >= {"cik", "accession_number", "company_name", "updated"}


def test_discovery_keeps_every_filer_cik_for_fallback(fetcher_on_fixture):
    """The Archives path 404s under the wrong CIK (documented in
    `fetch_acceptance_datetime`'s docstring and hit for real in the 08-18
    re-parse). Keeping all filer CIKs lets the fetch retry instead of recording a
    false failure."""
    filings = fetcher_on_fixture.get_filings_for_date("2026-08-17")
    multi = next(x for x in filings if x["accession_number"] == ACC_MULTI)
    pair = next(x for x in filings if x["accession_number"] == ACC_PAIR)

    assert len(multi["ciks"]) == 9
    assert len(pair["ciks"]) == 2
    assert multi["cik"] in multi["ciks"]


def test_discovery_hits_exactly_one_documented_endpoint(fetcher_on_fixture):
    fetcher_on_fixture.get_filings_for_date("2026-08-17")
    assert len(fetcher_on_fixture._calls) == 1
    assert fetcher_on_fixture._calls[0] == (
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260817.idx")


def test_empty_or_unfetchable_index_is_not_silent_success(monkeypatch):
    """A failed fetch must raise, never return [] — '0 filings found' and 'could
    not reach EDGAR' are different facts and the second must not read as the
    first (A6)."""
    import edgar_fetcher as ef
    monkeypatch.setattr(ef.EdgarFetcher, "_make_request", lambda self, url: None)
    with pytest.raises(RuntimeError):
        ef.EdgarFetcher().get_filings_for_date("2026-08-17")


# ── Idempotency ─────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real, initialised, per-test database."""
    import config
    import database
    path = tmp_path / "form4_insider_trades.db"
    monkeypatch.setitem(config.DATABASE_CONFIG, "db_path", str(path))
    database.initialize_database()
    return path


def _txn(accession, insider="A Person"):
    return {
        "accession_number": accession, "filing_date": "2026-08-17",
        "accepted_datetime": "2026-08-17T16:30:00-04:00",
        "issuer_cik": "1770787", "issuer_name": "10x Genomics, Inc.",
        "issuer_ticker": "TXG", "insider_cik": "2030916", "insider_name": insider,
        "insider_title": "Director", "transaction_date": "2026-08-15",
        "transaction_code": "P", "shares_amount": 100.0,
        "price_per_share": 10.0, "total_value": 1000.0,
        "acquired_disposed": "A", "shares_owned_after": 500.0, "is_10b5_1": 0,
    }


def test_the_table_has_no_unique_constraint_so_raw_insert_DOES_duplicate(db):
    """Pins the hazard rather than assuming it away. `insert_transaction`'s
    IntegrityError handler is dead code — nothing constrains this table. This
    test exists so that if a UNIQUE index is ever added, someone revisits the
    guards below rather than leaving them silently redundant."""
    import database
    assert database.insert_transaction(_txn(ACC_PAIR)) is True
    assert database.insert_transaction(_txn(ACC_PAIR)) is True   # duplicates!

    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM form4_transactions "
                    "WHERE accession_number=?", (ACC_PAIR,)).fetchone()[0]
    con.close()
    assert n == 2


def test_accession_has_transactions_detects_the_crash_window(db):
    """The guard that closes the gap `processed_filings` cannot: rows written,
    then the run died before `mark_filing_processed`."""
    import database
    assert database.accession_has_transactions(ACC_PAIR) is False
    database.insert_transaction(_txn(ACC_PAIR))
    assert database.accession_has_transactions(ACC_PAIR) is True


def test_rerunning_a_completed_filing_inserts_nothing(db, monkeypatch):
    """The ordinary re-run: the filing is in `processed_filings`, so the backfill
    skips it without a single network call."""
    import database
    import main
    database.mark_filing_processed(ACC_PAIR, "success")

    def _boom(*a, **k):
        raise AssertionError("a processed filing must not be fetched again")

    monkeypatch.setattr(main.EdgarFetcher, "fetch_form4_xml", _boom)
    result = main.backfill_one_filing(
        main.EdgarFetcher(), main.Form4Parser(),
        {"accession_number": ACC_PAIR, "cik": "1770787",
         "ciks": ["1770787"], "updated": "2026-08-17"})

    assert result["outcome"] == "skipped_already_processed"
    assert result["rows_inserted"] == 0


def test_rerunning_an_interrupted_filing_inserts_nothing(db, monkeypatch):
    """The crash-window re-run: rows are present but the filing was never marked.
    Without this guard a resumed backfill would double every such filing."""
    import database
    import main
    database.insert_transaction(_txn(ACC_PAIR))
    assert database.is_filing_processed(ACC_PAIR) is False   # never marked

    def _boom(*a, **k):
        raise AssertionError("an already-inserted accession must not be refetched")

    monkeypatch.setattr(main.EdgarFetcher, "fetch_form4_xml", _boom)
    result = main.backfill_one_filing(
        main.EdgarFetcher(), main.Form4Parser(),
        {"accession_number": ACC_PAIR, "cik": "1770787",
         "ciks": ["1770787"], "updated": "2026-08-17"})

    assert result["outcome"] == "skipped_rows_present"
    assert result["rows_inserted"] == 0

    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM form4_transactions "
                    "WHERE accession_number=?", (ACC_PAIR,)).fetchone()[0]
    con.close()
    assert n == 1, "the interrupted filing's rows must not be duplicated"


def test_idempotency_guards_match_both_accession_spellings(db, monkeypatch):
    """The database carries BOTH spellings and the two sources disagree.

    The nightly scanner discovers via the atom feed, whose link path yields the
    UNDASHED accession (`000161071726000359`), and stores that. The daily index
    yields the dashed canonical form (`0001610717-26-000359`). A backfill that
    only looked up its own spelling would find no match and re-insert every
    filing the nightly scanner had already stored — silently doubling the
    overlap rather than skipping it.
    """
    import database
    import main
    undashed = ACC_PAIR.replace("-", "")
    database.mark_filing_processed(undashed, "success")

    monkeypatch.setattr(main.EdgarFetcher, "fetch_form4_xml",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("refetched a filing stored under the "
                                           "other accession spelling")))
    result = main.backfill_one_filing(
        main.EdgarFetcher(), main.Form4Parser(),
        {"accession_number": ACC_PAIR, "cik": "1770787",
         "ciks": ["1770787"], "updated": "2026-08-17"})

    assert result["outcome"] == "skipped_already_processed"


def test_rows_present_guard_also_matches_both_spellings(db):
    """Same hazard on the crash-window guard."""
    import database
    undashed = ACC_PAIR.replace("-", "")
    database.insert_transaction(_txn(undashed))

    assert database.accession_has_transactions(ACC_PAIR) is True
    assert database.accession_has_transactions(undashed) is True



# ── Reconciliation and dry-run ──────────────────────────────────────

def test_dry_run_writes_nothing_at_all(db, monkeypatch, capsys):
    """Task 2.2: prove discovery + parse end-to-end before any insert path runs."""
    import database
    import main

    monkeypatch.setattr(
        main.EdgarFetcher, "get_filings_for_date",
        lambda self, d, form_type="4": [
            {"accession_number": ACC_PAIR, "cik": "1770787", "ciks": ["1770787"],
             "company_name": "10x Genomics, Inc.", "updated": "2026-08-17",
             "form_type": "4"}])
    monkeypatch.setattr(main.EdgarFetcher, "fetch_form4_xml",
                        lambda self, c, a: "<xml/>")
    monkeypatch.setattr(main.EdgarFetcher, "fetch_acceptance_datetime",
                        lambda self, c, a: "2026-08-17T16:30:00-04:00")
    monkeypatch.setattr(main.Form4Parser, "parse",
                        lambda self, *a, **k: [_txn(ACC_PAIR)])

    summary = main.backfill_date("2026-08-17", dry_run=True)

    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM form4_transactions").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM processed_filings").fetchone()[0] == 0
    con.close()

    assert summary["discovered"] == 1
    assert summary["would_insert_rows"] == 1
    assert summary["dry_run"] is True


def test_summary_reconciles_every_discovered_accession(db, monkeypatch):
    """Task 3: found == inserted + skipped + failed, exactly. An unexplained gap
    is a STOP, so the arithmetic is pinned rather than eyeballed."""
    import database
    import main

    ok, already, boom = "0000000000-26-000001", "0000000000-26-000002", "0000000000-26-000003"
    database.mark_filing_processed(already, "success")

    monkeypatch.setattr(
        main.EdgarFetcher, "get_filings_for_date",
        lambda self, d, form_type="4": [
            {"accession_number": a, "cik": "1", "ciks": ["1"],
             "company_name": "X", "updated": "2026-08-17", "form_type": "4"}
            for a in (ok, already, boom)])
    monkeypatch.setattr(main.EdgarFetcher, "fetch_acceptance_datetime",
                        lambda self, c, a: "2026-08-17T16:30:00-04:00")
    monkeypatch.setattr(
        main.EdgarFetcher, "fetch_form4_xml",
        lambda self, c, a: None if a == boom else "<xml/>")
    monkeypatch.setattr(main.Form4Parser, "parse",
                        lambda self, x, acc=None, *a, **k: [_txn(acc)])

    s = main.backfill_date("2026-08-17", dry_run=False)

    assert s["discovered"] == 3
    assert s["inserted_filings"] == 1
    assert s["skipped"] == 1
    assert s["failed"] == 1
    assert s["discovered"] == s["inserted_filings"] + s["skipped"] + s["failed"]
    assert s["reconciled"] is True
    # A failure carries its reason; it is never silently dropped.
    assert boom in {f["accession"] for f in s["failures"]}
    assert s["failures"][0]["reason"]


def test_checkpoint_lets_an_interrupted_run_resume(db, monkeypatch, tmp_path):
    """652 filings in a console session that can time out: progress must survive."""
    import main
    cp = tmp_path / "cp.json"

    monkeypatch.setattr(
        main.EdgarFetcher, "get_filings_for_date",
        lambda self, d, form_type="4": [
            {"accession_number": f"0000000000-26-00000{i}", "cik": "1", "ciks": ["1"],
             "company_name": "X", "updated": "2026-08-17", "form_type": "4"}
            for i in (1, 2)])
    monkeypatch.setattr(main.EdgarFetcher, "fetch_acceptance_datetime",
                        lambda self, c, a: "2026-08-17T16:30:00-04:00")
    monkeypatch.setattr(main.EdgarFetcher, "fetch_form4_xml", lambda self, c, a: "<xml/>")
    monkeypatch.setattr(main.Form4Parser, "parse",
                        lambda self, x, acc=None, *a, **k: [_txn(acc)])

    main.backfill_date("2026-08-17", dry_run=False, checkpoint_path=str(cp))
    done = json.loads(cp.read_text())["completed"]
    assert len(done) == 2

    # Second run: everything is already checkpointed, so nothing is refetched.
    monkeypatch.setattr(
        main.EdgarFetcher, "fetch_form4_xml",
        lambda self, c, a: (_ for _ in ()).throw(
            AssertionError("checkpointed filing refetched")))
    s = main.backfill_date("2026-08-17", dry_run=False, checkpoint_path=str(cp))
    assert s["skipped"] == 2
    assert s["inserted_filings"] == 0


# ── Wiring ──────────────────────────────────────────────────────────

def test_backfill_flag_bypasses_the_weekend_guard(monkeypatch):
    """`main()` returns early on Sat/Sun. A backfill is a manual data operation,
    not a scheduled scan — the PM must be able to run it on a Saturday."""
    import main
    called = {}
    monkeypatch.setattr("sys.argv",
                        ["main.py", "--backfill-date", "2026-08-17", "--dry-run"])
    monkeypatch.setattr(main, "backfill_date",
                        lambda d, **k: called.update(date=d, kw=k) or {})
    monkeypatch.setattr(main, "initialize_database", lambda: None)

    class _Sat:
        @staticmethod
        def now(tz=None):
            from datetime import datetime as _d, timezone as _tz
            return _d(2026, 8, 22, 22, 0, tzinfo=_tz.utc)   # Saturday

    monkeypatch.setattr(main, "datetime", _Sat)
    main.main()

    assert called["date"] == "2026-08-17"
    assert called["kw"]["dry_run"] is True


def test_backfill_does_not_send_email_or_run_analysis(monkeypatch):
    """It is a data operation. It must not alert, and must not fire the status
    email — a backfill's row count is not a scanner cycle's row count."""
    import main
    monkeypatch.setattr("sys.argv", ["main.py", "--backfill-date", "2026-08-17"])
    monkeypatch.setattr(main, "backfill_date", lambda d, **k: {})
    monkeypatch.setattr(main, "initialize_database", lambda: None)
    for name in ("analyze_and_alert", "analyze_sells_and_alert",
                 "send_status_report", "fetch_and_store_filings"):
        monkeypatch.setattr(main, name, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError(f"{name} must not run in backfill mode")))
    main.main()
