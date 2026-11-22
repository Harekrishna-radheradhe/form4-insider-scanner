"""Regressions for disclosure timing, candidate counting and achievable entries."""
from datetime import date
import sys
import types

import pandas as pd
import pytest

from insider_cluster_backtest import detect_clusters, fetch_forward_returns
from research_scan import eligible_purchase, find_clusters, write_report


def purchase(owner='1', trade='2026-11-16', accepted='2026-11-17T17:00:00-05:00', **changes):
    row = dict(insider_cik=owner, issuer_cik='123', issuer_ticker='TEST', issuer_name='Test Co',
               insider_name=f'Buyer {owner}', insider_title='CEO', transaction_date=trade,
               accepted_datetime=accepted, document_type='4', owner_count=1, is_officer=1,
               is_director=0, transaction_code='P', acquired_disposed='A', price_per_share=10,
               shares_amount=100, total_value=1000, security_title='Common Stock', footnotes='',
               accession_number=f'0000000001-26-{int(owner):06d}')
    row.update(changes)
    return row


def test_distinct_buyers_and_seven_inclusive_dates():
    filings = [[purchase('1')], [purchase('1')], [purchase('2')]]
    assert find_clusters(filings, date(2026, 11, 20)) == []
    filings.append([purchase('3', trade='2026-11-14')])
    assert find_clusters(filings, date(2026, 11, 20))[0]['buyers'] == 3
    assert find_clusters(filings, date(2026, 11, 21)) == []


def test_future_disclosure_and_amendments_do_not_inflate_clusters():
    filings = [[purchase('1')], [purchase('2')], [purchase('3', accepted='2026-11-23T17:00:00-05:00')]]
    assert find_clusters(filings, date(2026, 11, 20)) == []
    filings[-1] = [purchase('3')]
    assert len(find_clusters(filings, date(2026, 11, 20))) == 1
    filings.append([purchase('1', document_type='4/A')])
    assert find_clusters(filings, date(2026, 11, 20)) == []


@pytest.mark.parametrize('changes', [
    {'owner_count': 2}, {'is_officer': 0}, {'price_per_share': None},
    {'security_title': 'Preferred Stock'}, {'security_title': 'Common Stock Warrants'},
    {'footnotes': 'Acquired in a private placement'}, {'transaction_code': 'A'},
])
def test_excludes_ambiguous_or_unqualified_purchases(changes):
    assert not eligible_purchase(purchase(**changes))


def test_report_escapes_issuer_and_owner_text(tmp_path):
    filings = [[purchase(str(i), issuer_name='<script>alert(1)</script>')] for i in range(1, 4)]
    clusters = find_clusters(filings, date(2026, 11, 20))
    write_report(tmp_path, clusters, {'as_of': '2026-11-20', 'errors': []})
    report = (tmp_path / 'report.html').read_text()
    assert '<script>' not in report
    assert '&lt;script&gt;' in report


def historical_rows():
    return pd.DataFrame([
        dict(TICKER='TEST', ISSUERNAME='Test', TRANS_DATE=pd.Timestamp('2026-11-16'),
             FILING_DATE=pd.Timestamp(filing), RPTOWNERNAME=owner, ROLE='CEO',
             TRANS_SHARES=100, TRANS_PRICEPERSHARE=10, DOLLAR_VALUE=1000)
        for owner, filing in [('A', '2026-11-17'), ('B', '2026-11-18'), ('C', '2026-11-19')]
    ])


def test_backtest_waits_for_third_public_filing():
    rows = historical_rows()
    assert detect_clusters(rows[rows.FILING_DATE <= '2026-11-18']).empty
    clusters = detect_clusters(rows)
    assert clusters.iloc[0].signal_date == pd.Timestamp('2026-11-19')
    future = rows.iloc[[0]].copy()
    future['RPTOWNERNAME'] = 'D'
    future['FILING_DATE'] = pd.Timestamp('2026-11-20')
    future['DOLLAR_VALUE'] = 9000000
    first = detect_clusters(pd.concat([rows, future])).iloc[0]
    assert first.num_insiders == 3
    assert first.total_dollars == 3000


def test_returns_use_next_session_open_and_matching_benchmark(monkeypatch):
    dates = pd.to_datetime(['2026-11-19', '2026-11-20', '2026-11-23'])
    columns = pd.MultiIndex.from_product([['Open', 'Close'], ['TEST', 'SPY']])
    prices = pd.DataFrame([[5, 50, 6, 51], [10, 100, 11, 101], [12, 102, 13, 103]], index=dates, columns=columns)
    monkeypatch.setitem(sys.modules, 'yfinance', types.SimpleNamespace(download=lambda *a, **k: prices))
    result = fetch_forward_returns(detect_clusters(historical_rows()), return_windows=[1]).iloc[0]
    assert result.entry_date == pd.Timestamp('2026-11-20')
    assert result.entry_price == 10
    assert result.ret_1d == pytest.approx(10)
    assert result.spy_1d == pytest.approx(1)
    assert result.alpha_1d == pytest.approx(9)


def test_sec_xml_date_timezone_suffix_is_supported():
    filings = [[purchase(str(i), trade='2026-11-18-05:00')] for i in range(1, 4)]
    clusters = find_clusters(filings, date(2026, 11, 20))
    assert clusters[0]['buyers'] == 3
    assert clusters[0]['purchases'][0]['transaction_date'] == '2026-11-18'


def test_edgar_holidays_and_weekends():
    from research_scan import filing_days, latest_completed_filing_day
    assert filing_days(date(2026, 11, 7), date(2026, 11, 12)) == [
        date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 12)]
    assert latest_completed_filing_day(date(2026, 11, 22)) == date(2026, 11, 20)
    assert latest_completed_filing_day(date(2026, 11, 27)) == date(2026, 11, 25)


def test_combined_index_includes_amendments(monkeypatch):
    from pathlib import Path
    from edgar_fetcher import EdgarFetcher
    fixture = Path(__file__).parent / 'fixtures/form.20260817.sample.idx'
    response = types.SimpleNamespace(text=fixture.read_text())
    fetcher = EdgarFetcher()
    monkeypatch.setattr(fetcher, '_make_request', lambda url: response)
    filings = fetcher.get_filings_for_date('2026-08-17', form_type=None)
    assert len(filings) == 3
    assert {filing['form_type'] for filing in filings} == {'4', '4/A'}


def test_complete_submission_carries_xml_and_sec_acceptance(monkeypatch):
    from edgar_fetcher import EdgarFetcher
    fetcher = EdgarFetcher()
    response = types.SimpleNamespace(text='<ACCEPTANCE-DATETIME>20261120171230\n<XML><ownershipDocument><documentType>4</documentType></ownershipDocument></XML>')
    monkeypatch.setattr(fetcher, '_make_request', lambda url: response)
    xml, accepted = fetcher.fetch_ownership_submission('https://www.sec.gov/Archives/example.txt')
    assert xml.startswith('<ownershipDocument>')
    assert accepted == '2026-11-20T17:12:30-05:00'


def test_live_markdown_has_evidence_and_no_demo(tmp_path):
    filings = [[purchase(str(i), filing_url='https://www.sec.gov/Archives/test.txt')] for i in range(1, 4)]
    write_report(tmp_path, find_clusters(filings, date(2026, 11, 20)),
                 {'as_of': '2026-11-20', 'errors': [], 'stored_filings': 3})
    report = (tmp_path / 'report.md').read_text()
    assert '[SEC filing](https://www.sec.gov/Archives/test.txt)' in report
    assert 'DEMO' not in report
    assert '3 buyers' in report
