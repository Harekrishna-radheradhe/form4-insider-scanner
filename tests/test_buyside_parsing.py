"""
ITEM 1 regression: insider open-market PURCHASE ('P' / A/D='A') rows must
survive the full pipeline (parser -> DB insert -> purchase classification)
and must NOT be conflated with equity grants (code 'A'/'M', which also carry
A/D='A'). Purchase counting keys on transaction_code='P', not on A/D alone.
"""
import os
import pytest

from form4_parser import Form4Parser
import database as db


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')

# Synthetic Form-4: one genuine open-market PURCHASE (P/A), one SALE (S/D),
# and one equity GRANT (A/A) — the grant must never be counted as a purchase.
SYNTHETIC_FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer>
    <issuerCik>0001234567</issuerCik>
    <issuerName>Synthetic Test Corp</issuerName>
    <issuerTradingSymbol>SYN</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0007654321</rptOwnerCik>
      <rptOwnerName>DOE JANE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-17</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>110000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-17</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>51.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-17</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>0.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the database module at a fresh temp SQLite file."""
    dbfile = str(tmp_path / 'test_form4.db')
    # database.py holds a reference to this same dict; mutating the key is enough
    monkeypatch.setitem(db.DATABASE_CONFIG, 'db_path', dbfile)
    db.initialize_database()
    return dbfile


# ---- Parser layer ---------------------------------------------------------

def test_parser_captures_purchase_row():
    """A P/A open-market purchase must be captured by the parser."""
    txns = Form4Parser().parse(SYNTHETIC_FORM4, accession_number='ACC-P-1',
                               filing_date='2026-07-17')
    purchases = [t for t in txns if t['transaction_code'] == 'P']
    assert len(purchases) == 1, "parser dropped the open-market purchase row"
    p = purchases[0]
    assert p['acquired_disposed'] == 'A'
    assert p['shares_amount'] == 10000
    assert p['price_per_share'] == 50.0
    assert p['total_value'] == 500000
    assert p['issuer_ticker'] == 'SYN'


def test_parser_keeps_all_three_codes():
    """Parser must not silently drop P, S, or A rows."""
    txns = Form4Parser().parse(SYNTHETIC_FORM4)
    codes = sorted(t['transaction_code'] for t in txns)
    assert codes == ['A', 'P', 'S']


def test_parser_real_edgar_purchase_fixture():
    """Real SEC EDGAR Form-4 (CapsoVision, 10% owner P buy) survives parse."""
    path = os.path.join(FIXTURE_DIR, 'real_form4_with_purchase.xml')
    with open(path) as f:
        xml = f.read()
    txns = Form4Parser().parse(xml, accession_number='0001009165-26-000006',
                               filing_date='2026-07-20')
    purchases = [t for t in txns if t['transaction_code'] == 'P']
    assert len(purchases) == 1, "real-world P purchase was dropped by parser"
    p = purchases[0]
    assert p['acquired_disposed'] == 'A'
    assert p['shares_amount'] == 61068
    assert p['price_per_share'] == 6.73
    assert p['issuer_ticker'] == 'CV'


# ---- End-to-end: parser -> DB insert -> classification --------------------

def test_purchase_survives_end_to_end(temp_db):
    """
    Full path: parse synthetic filing, insert every row, then verify the
    purchase-counting queries return the P buy and EXCLUDE the equity grant.
    """
    txns = Form4Parser().parse(SYNTHETIC_FORM4, accession_number='ACC-E2E-1',
                               filing_date='2026-07-17')
    for t in txns:
        assert db.insert_transaction(t) is True

    stats = db.get_database_stats()
    assert stats['total_transactions'] == 3
    # Purchase counter keys on code 'P' — the grant (A/A) must NOT inflate it.
    assert stats['total_purchases'] == 1, "grant miscounted as purchase"
    assert stats['total_sells'] == 1

    # get_recent_purchases keys on P + A/D='A'; must return the buy, not grant.
    recent = db.get_recent_purchases('SYN', days=3650)
    assert len(recent) == 1
    assert recent[0]['shares_amount'] == 10000
    assert recent[0]['total_value'] == 500000


def test_real_purchase_survives_end_to_end(temp_db):
    """Real EDGAR P purchase is stored and retrievable via purchase query."""
    path = os.path.join(FIXTURE_DIR, 'real_form4_with_purchase.xml')
    with open(path) as f:
        xml = f.read()
    txns = Form4Parser().parse(xml, accession_number='0001009165-26-000006',
                               filing_date='2026-07-20')
    for t in txns:
        db.insert_transaction(t)

    stats = db.get_database_stats()
    assert stats['total_purchases'] == 1
    recent = db.get_recent_purchases('CV', days=3650)
    assert len(recent) == 1
    assert recent[0]['shares_amount'] == 61068
