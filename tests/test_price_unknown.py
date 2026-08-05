"""
Defect A — a missing transaction price is UNKNOWN, not zero.

4 of the 177 code-P rows in the §0.2 gate census carry no
<transactionPricePerShare> element. Today the parser writes 0.0 and
total_value = shares x 0 = 0.0, which is indistinguishable from "the insider
transacted at zero dollars" — something that never happens. Two of those four
are CEO open-market buys of 350,000 and 326,875 shares (PALO, KTWO); booked at
$0 they fall under cross_signal_scanner's MIN_PURCHASE_VALUE threshold and are
dropped SILENTLY, which is the defect reaching the live path.

Ruled (PM, 2026-08-05, spec §4): internally the unknown is None/NULL — never a
sentinel number, never a string. Human-facing surfaces render it as
"price unknown" — never blank, never $0, never the literal None/null.
"""
import os
import sqlite3

import pytest

import analyzer
import cross_signal_scanner as css
import email_reporter
from form4_parser import Form4Parser


ACCEPTED = '2026-02-24T16:59:40-05:00'

# A real shape: EDGAR omits the whole <transactionPricePerShare> element on the
# priceless filings, it does not send an empty one. Both are covered below.
NO_PRICE_ELEMENT = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer>
    <issuerCik>0002101562</issuerCik>
    <issuerName>Priceless Test Corp</issuerName>
    <issuerTradingSymbol>NOPX</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0002105547</rptOwnerCik>
      <rptOwnerName>DOE JANE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-02-20</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>350000</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>350000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

EMPTY_PRICE_ELEMENT = NO_PRICE_ELEMENT.replace(
    '<transactionShares><value>350000</value></transactionShares>',
    '<transactionShares><value>350000</value></transactionShares>\n'
    '        <transactionPricePerShare><value></value></transactionPricePerShare>')

# The control: a priced row must keep behaving exactly as it does today.
PRICED = NO_PRICE_ELEMENT.replace(
    '<transactionShares><value>350000</value></transactionShares>',
    '<transactionShares><value>350000</value></transactionShares>\n'
    '        <transactionPricePerShare><value>12.50</value></transactionPricePerShare>')


# ── Parser layer ────────────────────────────────────────────────

@pytest.mark.parametrize('xml,label', [(NO_PRICE_ELEMENT, 'element absent'),
                                       (EMPTY_PRICE_ELEMENT, 'element empty')])
def test_missing_price_parses_to_none_not_zero(xml, label):
    """price_per_share is None when the filing carries no price — never 0."""
    txns = Form4Parser().parse(xml, accession_number='ACC-NOPRICE-1',
                               filing_date='2026-02-24',
                               accepted_datetime=ACCEPTED)
    assert len(txns) == 1
    t = txns[0]
    assert t['price_per_share'] is None, (
        f"missing price ({label}) became {t['price_per_share']!r} — "
        "a real data point manufactured from an absent one")


@pytest.mark.parametrize('xml,label', [(NO_PRICE_ELEMENT, 'element absent'),
                                       (EMPTY_PRICE_ELEMENT, 'element empty')])
def test_missing_price_gives_none_total_value_not_zero(xml, label):
    """total_value is None when price is None — never shares x 0."""
    txns = Form4Parser().parse(xml, accession_number='ACC-NOPRICE-2',
                               filing_date='2026-02-24',
                               accepted_datetime=ACCEPTED)
    t = txns[0]
    assert t['total_value'] is None, (
        f"missing price ({label}) booked total_value={t['total_value']!r} — "
        "a $0 CEO purchase, which never happens")
    # shares are known and must survive intact — only the price is unknown
    assert t['shares_amount'] == 350000


def test_priced_row_is_unchanged():
    """Regression guard: a filing that DOES carry a price is untouched."""
    t = Form4Parser().parse(PRICED, accession_number='ACC-PRICED',
                            filing_date='2026-02-24',
                            accepted_datetime=ACCEPTED)[0]
    assert t['price_per_share'] == 12.50
    assert t['total_value'] == 350000 * 12.50


def test_genuine_zero_price_stays_zero():
    """
    A filing that explicitly states 0.00 (grants/awards do) is a KNOWN zero and
    must not be laundered into an unknown. Zero and unknown are different facts.
    """
    xml = NO_PRICE_ELEMENT.replace(
        '<transactionShares><value>350000</value></transactionShares>',
        '<transactionShares><value>350000</value></transactionShares>\n'
        '        <transactionPricePerShare><value>0.00</value></transactionPricePerShare>')
    t = Form4Parser().parse(xml, accession_number='ACC-ZERO',
                            filing_date='2026-02-24',
                            accepted_datetime=ACCEPTED)[0]
    assert t['price_per_share'] == 0.0
    assert t['total_value'] == 0.0


# ── analyzer.py:191 — the sort that crashes on None ─────────────

def _sell_row(ticker, total_value, price):
    return {'ticker': ticker, 'company_name': f'{ticker} Corp',
            'insider_name': f'{ticker} Seller',
            'insider_title': 'Chief Executive Officer',
            'is_officer': 1, 'is_director': 1, 'is_ten_percent_owner': 0,
            'transaction_date': '2026-02-20', 'shares_amount': 10000.0,
            'price_per_share': price, 'total_value': total_value,
            'accession_number': f'ACC-{ticker}'}


def test_ticker_sort_expression_tolerates_none():
    """
    The sort key at analyzer.py:191 indexed r['total_value'] directly while the
    sell-level sort six lines above already guarded with `or 0`. Unary minus on
    None raises, so the bare expression is genuinely unsafe — this pins the
    guard in place independently of who feeds it.
    """
    tier_priority = {'S1': 0, 'S2': 1, 'SELL_WATCH': 2}
    rows = [{'tier_tag': 'S1', 'total_value': None},
            {'tier_tag': 'S1', 'total_value': 500_000.0}]
    rows.sort(key=lambda r: (tier_priority.get(r['tier_tag'], 99),
                             -(r['total_value'] or 0)))
    assert rows[0]['total_value'] == 500_000.0


def test_find_sell_signals_survives_none_total_value(monkeypatch):
    """
    End-to-end characterisation of the same path.

    NOTE, measured rather than assumed: this does NOT reach the line-191 crash
    today, because the per-ticker accumulator at line 165 already coerces with
    `sell['total_value'] or 0`, so the ticker-level value is 0 by the time the
    sort sees it. The guard above is still correct — it stops being reachable
    only by accident of an upstream `or 0` — and this test locks the whole path
    down so a change to that accumulator cannot reintroduce the crash silently.
    """
    rows = [_sell_row('NOPX', None, None), _sell_row('AAA', 500_000.0, 50.0)]
    monkeypatch.setattr(analyzer, 'get_significant_sells', lambda **kw: rows)

    results = analyzer.SellSignalAnalyzer().find_sell_signals()

    assert {r['ticker'] for r in results} == {'NOPX', 'AAA'}
    # The unknown must not be ranked ABOVE a known $500K sell by being read as 0.
    assert results[0]['ticker'] == 'AAA'


# ── cross_signal_scanner — unknown is EXCLUDED, not zeroed ──────

@pytest.fixture
def scanner_db(tmp_path, monkeypatch):
    """A Form-4 DB carrying one priced and one price-unknown code-P row."""
    path = str(tmp_path / 'form4.db')
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE form4_transactions (
        id INTEGER PRIMARY KEY, accession_number TEXT, filing_date TEXT,
        accepted_datetime DATETIME, issuer_cik TEXT, issuer_name TEXT,
        issuer_ticker TEXT, insider_cik TEXT, insider_name TEXT,
        insider_title TEXT, is_director INTEGER, is_officer INTEGER,
        is_ten_percent_owner INTEGER, transaction_date TEXT,
        transaction_code TEXT, shares_amount REAL, price_per_share REAL,
        total_value REAL, acquired_disposed TEXT, shares_owned_after REAL)''')
    today = css.datetime.now(css.timezone.utc).strftime('%Y-%m-%d')
    con.executemany(
        'INSERT INTO form4_transactions (accession_number, issuer_ticker, '
        'insider_name, insider_title, transaction_date, transaction_code, '
        'shares_amount, price_per_share, total_value) VALUES (?,?,?,?,?,?,?,?,?)',
        [('ACC-PRICED', 'AAA', 'Priced Buyer', 'Chief Executive Officer',
          today, 'P', 40000.0, 25.0, 1000000.0),
         ('ACC-UNKNOWN', 'NOPX', 'Unknown Buyer', 'Chief Executive Officer',
          today, 'P', 350000.0, None, None)])
    con.commit()
    con.close()
    monkeypatch.setattr(css, 'DB_PATH', path)
    return path


def test_price_unknown_row_is_not_ranked_as_zero(scanner_db):
    """
    The COALESCE(..., 0) folded an unknown-priced row to $0 and let it be
    ranked and thresholded as a real, wrong data point. RULED OUT — such rows
    are excluded from the ranking/threshold path entirely.
    """
    rows = css.get_recent_purchases(days_back=3)
    unknown = [r for r in rows if r['ticker'] == 'NOPX']
    assert not unknown, (
        "the price-unknown row reached the ranking path; it must be excluded, "
        f"got {unknown!r}")
    assert any(r['ticker'] == 'AAA' for r in rows), \
        "excluding unknowns must not drop the priced rows with them"


def test_price_unknown_exclusion_is_counted_not_silent(scanner_db, capsys):
    """A dropped candidate that nobody counts is the same silence, relocated."""
    css.get_recent_purchases(days_back=3)
    out = capsys.readouterr().out
    assert 'price unknown' in out.lower(), (
        "excluded rows must be reported, not silently discarded; got:\n" + out)


def test_filter_tier2_never_thresholds_on_none():
    """filter_tier2 compares total_value >= MIN_PURCHASE_VALUE — None raises."""
    purchases = [
        {'ticker': 'NOPX', 'insider_title': 'Chief Executive Officer',
         'total_value': None, 'price_per_share': None, 'shares': 350000.0},
        {'ticker': 'AAA', 'insider_title': 'Chief Executive Officer',
         'total_value': 1_000_000.0, 'price_per_share': 25.0, 'shares': 40000.0},
    ]
    tier2 = css.filter_tier2(purchases)
    assert [p['ticker'] for p in tier2] == ['AAA']


# ── Human-facing rendering ──────────────────────────────────────

def test_fmt_value_renders_unknown_not_zero_or_none():
    """Never blank, never $0, never the literal words None/null."""
    rendered = email_reporter._fmt_value(None)
    assert rendered == 'price unknown', rendered
    assert rendered.strip(), "blank reads as an oversight"
    assert 'none' not in rendered.lower() and 'null' not in rendered.lower()
    assert '$0' not in rendered


def test_fmt_value_known_values_unchanged():
    assert email_reporter._fmt_value(0) == '$0'
    assert email_reporter._fmt_value(50_000) == '$50,000'
    assert email_reporter._fmt_value(2_500_000) == '$2.50M'


def test_scanner_html_renders_unknown_price():
    """cross_signal_scanner's HTML formats price with :.2f — None raises."""
    signal = {
        'ticker': 'NOPX', 'insider_name': 'Unknown Buyer',
        'insider_title': 'Chief Executive Officer',
        'transaction_date': '2026-02-20', 'shares': 350000.0,
        'price_per_share': None, 'total_value': None,
    }
    line = css.format_purchase_line(signal)
    assert 'price unknown' in line.lower()
    assert '$0' not in line
    assert 'None' not in line
    assert '350,000 shares' in line
