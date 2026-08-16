"""
Rule 10b5-1(c) trading-plan checkbox capture — `<aff10b5One>`.

The element is in the source EDGAR XML and was captured nowhere in the
pipeline (JOB-20260815-INSIDERS-DATA-PREP-1 §1.1, verdict
PRESENT-IN-SOURCE-NOT-CAPTURED). Today `analyzer.py` INFERS plan status from
notional ("$5M+ ⇒ possible 10b5-1"); the filing states it outright, and the
measured base rate is ~8–9% true, so the heuristic is guessing at a minority
class it could simply read.

The load-bearing rule under test: **ABSENT MEANS UNKNOWN, NOT FALSE.** The
checkbox only exists on filings made on or after 2023-04-01; before that no
filer could state a plan either way. Writing 0 for an absent element makes
"this filer said no" and "nobody could say" the same value — the identical
mistake `price_per_share = 0.0` made for an absent price (see
test_price_unknown.py), and it is refused here for the same reason.

Two encodings occur in the wild: the XML emits 0/1, and SEC's own quarterly
SUBMISSION.tsv carries 0/1 AND false/true in one column. Both normalize.
"""
import os
import sqlite3

import pytest

import database
from form4_parser import Form4Parser


ACCEPTED = '2026-08-07T16:59:40-05:00'

FIXTURE = os.path.join(os.path.dirname(__file__),
                       'fixtures', 'real_form4_with_purchase.xml')

# A real cached EDGAR document, filed post-2023-04-01 (periodOfReport
# 2026-07-17), carrying <aff10b5One>0</aff10b5One> as a DOCUMENT-level
# element — a sibling of <reportingOwner>, immediately after
# </reportingOwner>, NOT inside <nonDerivativeTable>. That position is
# exactly why a walk which only descends into the transaction table never
# sees it.
with open(FIXTURE) as fh:
    REAL_CHECKBOX_FALSE = fh.read()

REAL_CHECKBOX_TRUE = REAL_CHECKBOX_FALSE.replace(
    '<aff10b5One>0</aff10b5One>', '<aff10b5One>1</aff10b5One>')

# The SUBMISSION.tsv encoding, same column, same meaning.
REAL_CHECKBOX_TRUE_WORD = REAL_CHECKBOX_FALSE.replace(
    '<aff10b5One>0</aff10b5One>', '<aff10b5One>true</aff10b5One>')
REAL_CHECKBOX_FALSE_WORD = REAL_CHECKBOX_FALSE.replace(
    '<aff10b5One>0</aff10b5One>', '<aff10b5One>false</aff10b5One>')

# A pre-2023-style document: the element does not exist at all. This is what
# every filing before 2023-04-01 looks like, and it is ~3.4 of the 13.5 years
# the backfill will cover.
PRE_2023_STYLE = REAL_CHECKBOX_FALSE.replace(
    '<aff10b5One>0</aff10b5One>', '')

# Present but empty — 2023q1 is 91% empty in SEC's own bulk table, because the
# checkbox took effect mid-quarter.
EMPTY_ELEMENT = REAL_CHECKBOX_FALSE.replace(
    '<aff10b5One>0</aff10b5One>', '<aff10b5One></aff10b5One>')


def _parse(xml, accession='ACC-10B5-1'):
    return Form4Parser().parse(xml, accession_number=accession,
                               filing_date='2026-08-07',
                               accepted_datetime=ACCEPTED)


# ── Parser layer ────────────────────────────────────────────────

def test_fixture_really_carries_the_element_where_the_report_says():
    """
    Anti-vacuous guard (A6): if the fixture ever loses the element, every
    "captured correctly" assertion below would pass by capturing nothing.
    Pin the precondition, and pin the POSITION the capture depends on.
    """
    assert '<aff10b5One>0</aff10b5One>' in REAL_CHECKBOX_FALSE
    head = REAL_CHECKBOX_FALSE.split('<aff10b5One>')[0]
    assert head.rstrip().endswith('</reportingOwner>'), (
        "the element is document-level, immediately after </reportingOwner>; "
        "if that moved, the capture's premise moved with it")
    assert '<aff10b5One>' not in PRE_2023_STYLE


@pytest.mark.parametrize('xml,expected,label', [
    (REAL_CHECKBOX_FALSE, 0, 'XML 0'),
    (REAL_CHECKBOX_TRUE, 1, 'XML 1'),
    (REAL_CHECKBOX_FALSE_WORD, 0, 'TSV false'),
    (REAL_CHECKBOX_TRUE_WORD, 1, 'TSV true'),
], ids=['xml-0', 'xml-1', 'tsv-false', 'tsv-true'])
def test_checkbox_value_is_captured_in_both_encodings(xml, expected, label):
    txns = _parse(xml)
    assert txns, "fixture must yield at least one non-derivative transaction"
    for t in txns:
        assert t['is_10b5_1'] == expected, (
            f"{label} parsed to {t['is_10b5_1']!r}, expected {expected}")


@pytest.mark.parametrize('xml,label', [
    (PRE_2023_STYLE, 'element absent (pre-2023 filing)'),
    (EMPTY_ELEMENT, 'element present but empty (2023q1)'),
], ids=['absent', 'empty'])
def test_absent_checkbox_is_none_never_zero(xml, label):
    """
    The whole point. `is None` — not falsy, not 0. `assert not x` would pass
    on 0 and let the defect through, so the identity check is deliberate.
    """
    txns = _parse(xml)
    assert txns
    for t in txns:
        assert t['is_10b5_1'] is None, (
            f"{label} became {t['is_10b5_1']!r} — an unknown manufactured "
            "into a stated 'no', the price_per_share=0.0 mistake repeated")
        assert t['is_10b5_1'] is not 0  # noqa: F632 - identity is the point


def test_unparseable_value_is_unknown_not_guessed():
    """A value we do not recognise is unknown; it is never coerced to 0 or 1."""
    xml = REAL_CHECKBOX_FALSE.replace(
        '<aff10b5One>0</aff10b5One>', '<aff10b5One>maybe</aff10b5One>')
    for t in _parse(xml):
        assert t['is_10b5_1'] is None


def test_capture_changes_nothing_else_about_the_parse():
    """
    The non-flag delta check, at parser level: the same document with and
    without the element must agree on EVERY other field. This is the unit-test
    twin of the dry-run old-vs-new comparison — it catches a capture that
    perturbs the transaction walk it is a sibling of.
    """
    with_flag = _parse(REAL_CHECKBOX_FALSE)
    without = _parse(PRE_2023_STYLE)
    assert len(with_flag) == len(without)
    for a, b in zip(with_flag, without):
        a_rest = {k: v for k, v in a.items() if k != 'is_10b5_1'}
        b_rest = {k: v for k, v in b.items() if k != 'is_10b5_1'}
        assert a_rest == b_rest, (
            "capturing the checkbox perturbed another field: "
            f"{ {k: (a_rest[k], b_rest[k]) for k in a_rest if a_rest[k] != b_rest.get(k)} }")


# ── Write path ──────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A DB created by initialize_database() itself — new schema, no hand-copy."""
    path = str(tmp_path / 'form4.db')
    import config
    monkeypatch.setitem(config.DATABASE_CONFIG, 'db_path', path)
    database.initialize_database()
    return path


def test_schema_carries_the_column(fresh_db):
    con = sqlite3.connect(fresh_db)
    cols = [r[1] for r in con.execute('PRAGMA table_info(form4_transactions)')]
    con.close()
    assert 'is_10b5_1' in cols, cols


@pytest.mark.parametrize('value', [0, 1, None])
def test_round_trip_preserves_the_value_including_null(fresh_db, value):
    """NULL must survive as NULL. A NULL that reads back 0 is the defect."""
    txn = _parse(REAL_CHECKBOX_FALSE)[0]
    txn['is_10b5_1'] = value
    txn['accession_number'] = f'ACC-RT-{value}'
    assert database.insert_transaction(txn) is True

    con = sqlite3.connect(fresh_db)
    stored = con.execute(
        'SELECT is_10b5_1 FROM form4_transactions WHERE accession_number = ?',
        (f'ACC-RT-{value}',)).fetchone()[0]
    con.close()
    assert stored == value and type(stored) is type(value), (
        f"wrote {value!r}, read back {stored!r}")


def test_insert_against_an_unmigrated_table_fails_loudly(tmp_path, monkeypatch):
    """
    THE DEPLOYMENT ORDERING RAIL.

    `initialize_database()` uses CREATE TABLE IF NOT EXISTS, so it will NOT add
    the column to a table that already exists. The live DB is one of those. If
    the code change reaches a box whose DB has not had

        ALTER TABLE form4_transactions ADD COLUMN is_10b5_1 INTEGER;

    applied, every insert raises sqlite3.OperationalError — which
    `insert_transaction` does NOT catch (it catches IntegrityError only), so it
    propagates. This test pins that the failure is LOUD rather than silent, and
    exists so the migration cannot be forgotten without something going red.
    """
    path = str(tmp_path / 'old_schema.db')
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE form4_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, accession_number TEXT,
        filing_date DATE, accepted_datetime DATETIME, issuer_cik TEXT,
        issuer_name TEXT, issuer_ticker TEXT, insider_cik TEXT,
        insider_name TEXT, insider_title TEXT, is_director INTEGER,
        is_officer INTEGER, is_ten_percent_owner INTEGER, transaction_date DATE,
        transaction_code TEXT, shares_amount REAL, price_per_share REAL,
        total_value REAL, acquired_disposed TEXT, shares_owned_after REAL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    con.commit()
    con.close()

    import config
    monkeypatch.setitem(config.DATABASE_CONFIG, 'db_path', path)

    with pytest.raises(sqlite3.OperationalError) as exc:
        database.insert_transaction(_parse(REAL_CHECKBOX_FALSE)[0])
    assert 'is_10b5_1' in str(exc.value)


def test_alter_migration_leaves_existing_rows_null(tmp_path, monkeypatch):
    """
    The migration is additive: existing rows get NULL, and NULL is correct —
    those rows were parsed by code that never read the element, so their plan
    status is genuinely unknown. No historical row is rewritten.
    """
    path = str(tmp_path / 'migrate.db')
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE form4_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, accession_number TEXT,
        transaction_code TEXT)''')
    con.execute("INSERT INTO form4_transactions (accession_number, "
                "transaction_code) VALUES ('ACC-OLD', 'P')")
    con.commit()

    con.execute('ALTER TABLE form4_transactions ADD COLUMN is_10b5_1 INTEGER')
    con.commit()

    row = con.execute("SELECT accession_number, transaction_code, is_10b5_1 "
                      "FROM form4_transactions").fetchone()
    con.close()
    assert row[0] == 'ACC-OLD', "the historical row must be untouched"
    assert row[1] == 'P'
    assert row[2] is None, f"backfilled as {row[2]!r}, must be NULL"
