"""Tests for the code-P price_per_share backfill (PM ruling R12, 2026-08-06).

Written BEFORE the tool touched even the read-only mirror, per the WO.

Scope is RULED: code-P only — the 11 purchase accessions / 13 rows from the
§1.1 census (`research/insiders_price_zero_census_20260806.json` in gmc_engine).
The full 548-accession cleanup is a separate future WO queued behind
CIK-recovery, and the literal "all 3,534 zero rows" scope is RULED OUT
permanently: 2,701 of those zeros are legitimate explicit 0.00 grants, and
rewriting them to NULL would turn correct values into unknowns.

Every rule below came out of the census rather than from taste, and two of them
exist because the census got them wrong first:

  * `(accession, date, shares)` is NOT unique — an IGIC filing carries two
    transactions with the same date and share count, one at 25.3133 and one at
    0, and a first-match rule mis-attributed them. The tool REFUSES an ambiguous
    match instead of picking one.
  * A price is unknown only when no NUMERIC value exists. A
    `<transactionPricePerShare>` wrapper with no `<value>` child is unknown; an
    explicit `0` is a KNOWN zero and must be left alone.
"""
import json
import sqlite3

import pytest

from backfill_price_per_share import (
    AMBIGUOUS, LEAVE, SET_PRICE, SET_UNKNOWN, UNMATCHED,
    find_code_p_zero_rows, resolve_row, source_transactions, write_rollback,
    apply_resolutions,
)


# ── source XML parsing ───────────────────────────────────────────

def _xml(txns):
    """Minimal ownershipDocument with the given nonDerivativeTransaction bodies."""
    return ("<ownershipDocument><nonDerivativeTable>"
            + "".join(txns) + "</nonDerivativeTable></ownershipDocument>")


def _txn(date="2026-02-20", shares="350000", code="P", ad="A", price=None,
         price_element=True):
    p = ""
    if price_element:
        inner = f"<value>{price}</value>" if price is not None else ""
        p = f"<transactionPricePerShare>{inner}</transactionPricePerShare>"
    return (f"<nonDerivativeTransaction>"
            f"<transactionDate><value>{date}</value></transactionDate>"
            f"<transactionAmounts>"
            f"<transactionShares><value>{shares}</value></transactionShares>"
            f"{p}"
            f"<transactionAcquiredDisposedCode><value>{ad}</value>"
            f"</transactionAcquiredDisposedCode>"
            f"</transactionAmounts>"
            f"<transactionCoding><transactionCode>{code}</transactionCode>"
            f"</transactionCoding>"
            f"</nonDerivativeTransaction>")


def test_source_parsing_reads_date_shares_code_and_price_state():
    t = source_transactions(_xml([_txn(price="12.34")]))[0]
    assert t["date"] == "2026-02-20"
    assert t["shares"] == pytest.approx(350000.0)
    assert t["code"] == "P"
    assert t["acq_disp"] == "A"
    assert t["price_state"] == "VALUE"
    assert t["price"] == pytest.approx(12.34)


def test_a_price_wrapper_with_no_value_child_is_UNKNOWN_not_zero():
    """The exact shape all 13 ruled rows carry. Reading it as 0 is the defect."""
    t = source_transactions(_xml([_txn(price=None, price_element=True)]))[0]
    assert t["price_state"] == "ELEM_NO_VALUE"
    assert t["price"] is None


def test_a_missing_price_element_is_UNKNOWN():
    t = source_transactions(_xml([_txn(price_element=False)]))[0]
    assert t["price_state"] == "NO_ELEMENT"
    assert t["price"] is None


def test_an_explicit_zero_is_a_KNOWN_zero():
    t = source_transactions(_xml([_txn(price="0")]))[0]
    assert t["price_state"] == "VALUE"
    assert t["price"] == pytest.approx(0.0)


# ── resolution: the decision table ───────────────────────────────

def _row(date="2026-02-20", shares=350000.0, code="P", ad="A", rid=1,
         acc="000110465926019190"):
    return {"id": rid, "accession_number": acc, "transaction_date": date,
            "shares_amount": shares, "transaction_code": code,
            "acquired_disposed": ad, "price_per_share": 0.0,
            "total_value": 0.0, "issuer_cik": "1", "issuer_ticker": "X"}


def test_no_numeric_price_in_source_resolves_to_NULL():
    r = resolve_row(_row(), source_transactions(_xml([_txn(price=None)])))
    assert r.action == SET_UNKNOWN
    assert r.price is None and r.total_value is None


def test_explicit_zero_in_source_is_LEFT_ALONE():
    """Never widen a correct value into an unknown."""
    r = resolve_row(_row(), source_transactions(_xml([_txn(price="0")])))
    assert r.action == LEAVE


def test_a_real_price_in_source_is_RECOVERED_not_nulled():
    """If the source does carry a number, the stored 0.0 lost it — write it back
    and recompute total_value rather than blanking the row."""
    r = resolve_row(_row(shares=100.0),
                    source_transactions(_xml([_txn(shares="100", price="5.50")])))
    assert r.action == SET_PRICE
    assert r.price == pytest.approx(5.50)
    assert r.total_value == pytest.approx(550.0)


def test_total_value_moves_with_price_per_share():
    """63f7558 sets both to None together. A backfill that nulls the price and
    leaves total_value = 0.0 leaves the defect in the column the signal reads."""
    r = resolve_row(_row(), source_transactions(_xml([_txn(price=None)])))
    assert r.price is None and r.total_value is None


# ── ambiguity: the rule the census learned the hard way ──────────

def test_two_source_transactions_matching_on_date_and_shares_are_AMBIGUOUS():
    """The IGIC shape: same date, same share count, different prices. Picking
    the first produced a false 'lost price' finding during the census."""
    xml = _xml([_txn(shares="24", price="25.3133"), _txn(shares="24", price="0")])
    r = resolve_row(_row(shares=24.0), source_transactions(xml))
    assert r.action == AMBIGUOUS
    assert r.price is None


def test_ambiguity_is_broken_by_transaction_code_when_it_can_be():
    """Matching on code as well as date+shares narrows a pair that would
    otherwise be ambiguous — the census matched on date+shares alone."""
    xml = _xml([_txn(shares="24", code="A", price="25.3133"),
                _txn(shares="24", code="P", price=None)])
    r = resolve_row(_row(shares=24.0, code="P"), source_transactions(xml))
    assert r.action == SET_UNKNOWN


def test_ambiguity_is_broken_by_acquired_disposed_when_it_can_be():
    xml = _xml([_txn(shares="24", ad="D", price="9.99"),
                _txn(shares="24", ad="A", price=None)])
    r = resolve_row(_row(shares=24.0, ad="A"), source_transactions(xml))
    assert r.action == SET_UNKNOWN


def test_no_matching_source_transaction_is_UNMATCHED_not_a_guess():
    xml = _xml([_txn(date="2020-01-01", shares="999")])
    r = resolve_row(_row(), source_transactions(xml))
    assert r.action == UNMATCHED
    assert r.price is None


def test_an_empty_source_is_UNMATCHED_rather_than_a_null_write():
    """A filing that fetched but parsed to nothing must never be read as
    'no price found, therefore NULL' — that is a fetch failure writing data."""
    r = resolve_row(_row(), [])
    assert r.action == UNMATCHED


def test_share_matching_tolerates_only_rounding_not_a_different_lot():
    assert resolve_row(_row(shares=350000.001),
                       source_transactions(_xml([_txn(shares="350000",
                                                      price=None)]))).action == SET_UNKNOWN
    assert resolve_row(_row(shares=350001.0),
                       source_transactions(_xml([_txn(shares="350000",
                                                      price=None)]))).action == UNMATCHED


# ── candidate selection: the RULED scope, enforced in code ───────

def _db(tmp_path, rows):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE form4_transactions (
        id INTEGER PRIMARY KEY, accession_number TEXT, filing_date DATE,
        accepted_datetime DATETIME, issuer_cik TEXT, issuer_name TEXT,
        issuer_ticker TEXT, insider_cik TEXT, insider_name TEXT,
        insider_title TEXT, is_director INTEGER, is_officer INTEGER,
        is_ten_percent_owner INTEGER, transaction_date DATE,
        transaction_code TEXT, shares_amount REAL, price_per_share REAL,
        total_value REAL, acquired_disposed TEXT, shares_owned_after REAL,
        created_at DATETIME)""")
    for r in rows:
        con.execute(
            "INSERT INTO form4_transactions (accession_number, issuer_cik, "
            "issuer_ticker, transaction_date, transaction_code, shares_amount, "
            "price_per_share, total_value, acquired_disposed) "
            "VALUES (?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()
    return p


def test_only_code_P_zero_rows_are_candidates(tmp_path):
    """The ruled scope. A code-A zero is NOT a candidate — 2,701 of them are
    legitimate explicit grants, and the all-rows scope is ruled out permanently."""
    p = _db(tmp_path, [
        ("accP", "1", "AAA", "2026-01-01", "P", 100.0, 0.0, 0.0, "A"),
        ("accA", "1", "BBB", "2026-01-01", "A", 100.0, 0.0, 0.0, "A"),
        ("accM", "1", "CCC", "2026-01-01", "M", 100.0, 0.0, 0.0, "A"),
    ])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = find_code_p_zero_rows(con)
    con.close()
    assert [r["accession_number"] for r in rows] == ["accP"]


def test_priced_code_P_rows_are_not_candidates(tmp_path):
    p = _db(tmp_path, [("acc1", "1", "AAA", "2026-01-01", "P", 100.0, 7.5, 750.0, "A")])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    assert find_code_p_zero_rows(con) == []
    con.close()


def test_already_null_rows_are_not_candidates(tmp_path):
    """Idempotence: a second run after an apply must find nothing to do."""
    p = _db(tmp_path, [("acc1", "1", "AAA", "2026-01-01", "P", 100.0, None, None, "A")])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    assert find_code_p_zero_rows(con) == []
    con.close()


# ── A14: rollback artifact BEFORE any write, enforced in code ────

def test_rollback_artifact_records_prior_values_and_copies_the_db(tmp_path):
    p = _db(tmp_path, [("acc1", "1", "AAA", "2026-01-01", "P", 100.0, 0.0, 0.0, "A")])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = find_code_p_zero_rows(con)
    con.close()
    rec = write_rollback(p, rows, tmp_path / "rollback")
    assert rec.exists()
    doc = json.loads(rec.read_text())
    assert doc["n_rows"] == 1
    assert doc["prior_values"][0]["price_per_share"] == 0.0
    assert doc["scope"] == "code-P only (PM ruling R12, 2026-08-06)"
    backups = list((tmp_path / "rollback").glob("*.bak"))
    assert len(backups) == 1


def test_apply_refuses_to_write_without_a_rollback_artifact(tmp_path):
    """Enforced in code, not convention — the WO's §3 wording."""
    p = _db(tmp_path, [("acc1", "1", "AAA", "2026-01-01", "P", 100.0, 0.0, 0.0, "A")])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = find_code_p_zero_rows(con)
    con.close()
    from backfill_price_per_share import Resolution
    res = [(rows[0], Resolution(SET_UNKNOWN, None, None, "test"))]
    with pytest.raises(RuntimeError, match="rollback"):
        apply_resolutions(p, res, rollback_record=None)


def test_apply_writes_null_for_both_columns(tmp_path):
    p = _db(tmp_path, [("acc1", "1", "AAA", "2026-01-01", "P", 100.0, 0.0, 0.0, "A")])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = find_code_p_zero_rows(con)
    con.close()
    rec = write_rollback(p, rows, tmp_path / "rollback")
    from backfill_price_per_share import Resolution
    n = apply_resolutions(p, [(rows[0], Resolution(SET_UNKNOWN, None, None, "x"))],
                          rollback_record=rec)
    assert n == 1
    con = sqlite3.connect(p)
    got = con.execute("SELECT price_per_share, total_value FROM "
                      "form4_transactions WHERE id = ?", (rows[0]["id"],)).fetchone()
    con.close()
    assert got == (None, None)


def test_apply_never_writes_an_ambiguous_or_unmatched_row(tmp_path):
    """The rows the tool refuses to decide must not be touched at all."""
    p = _db(tmp_path, [("acc1", "1", "AAA", "2026-01-01", "P", 100.0, 0.0, 0.0, "A"),
                       ("acc2", "1", "BBB", "2026-01-01", "P", 200.0, 0.0, 0.0, "A")])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = find_code_p_zero_rows(con)
    con.close()
    rec = write_rollback(p, rows, tmp_path / "rollback")
    from backfill_price_per_share import Resolution
    n = apply_resolutions(p, [
        (rows[0], Resolution(AMBIGUOUS, None, None, "two matches")),
        (rows[1], Resolution(UNMATCHED, None, None, "no match")),
    ], rollback_record=rec)
    assert n == 0
    con = sqlite3.connect(p)
    vals = con.execute("SELECT price_per_share, total_value FROM "
                       "form4_transactions ORDER BY id").fetchall()
    con.close()
    assert vals == [(0.0, 0.0), (0.0, 0.0)]


def test_apply_leaves_a_known_zero_untouched(tmp_path):
    p = _db(tmp_path, [("acc1", "1", "AAA", "2026-01-01", "P", 100.0, 0.0, 0.0, "A")])
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = find_code_p_zero_rows(con)
    con.close()
    rec = write_rollback(p, rows, tmp_path / "rollback")
    from backfill_price_per_share import Resolution
    n = apply_resolutions(p, [(rows[0], Resolution(LEAVE, None, None, "explicit 0"))],
                          rollback_record=rec)
    assert n == 0
