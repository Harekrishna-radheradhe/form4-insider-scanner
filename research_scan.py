"""Collect completed EDGAR filing days and write a reviewable cluster report.

Usage: python research_scan.py --start 2026-11-09 --end 2026-11-20
Requires config.py copied from config_example.py and SEC_USER_AGENT in the env.
"""
import argparse
import html
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import EDGAR_CONFIG
from edgar_fetcher import EdgarFetcher
from form4_parser import Form4Parser

EASTERN = ZoneInfo('America/New_York')
COMMON_STOCK = re.compile(r'\b(common stock|common shares|ordinary shares)\b', re.I)
PRIVATE_DEAL = re.compile(r'private placement|privately negotiated|subscription agreement|purchase agreement', re.I)


def transaction_calendar_date(value):
    """SEC XML dates may include a timezone suffix after the calendar date."""
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:Z|[+-]\d{2}:\d{2})?', value or '')
    if not match:
        raise ValueError(f'Invalid SEC transaction date: {value!r}')
    return date.fromisoformat(match.group(1))


def eligible_purchase(transaction):
    """Conservative candidates for manual filing review, including footnotes."""
    return (
        transaction.get('document_type') == '4'
        and transaction.get('owner_count') == 1
        and bool(transaction.get('is_officer') or transaction.get('is_director'))
        and bool(transaction.get('insider_cik'))
        and transaction.get('transaction_code') == 'P'
        and transaction.get('acquired_disposed') == 'A'
        and (transaction.get('price_per_share') or 0) > 0
        and (transaction.get('shares_amount') or 0) > 0
        and bool(COMMON_STOCK.search(transaction.get('security_title') or ''))
        and not re.search(r'preferred|unit|warrant', transaction.get('security_title') or '', re.I)
        and not PRIVATE_DEAL.search(transaction.get('footnotes') or '')
    )


def find_clusters(filings, as_of, days=7, minimum=3):
    """Group known, single-owner purchases within seven inclusive calendar dates.

    An amendment without a machine-readable original accession makes that
    issuer require review. Suppress its candidates to avoid counting both filings.
    """
    cutoff = as_of - timedelta(days=days - 1)
    amended_issuers = set()
    transactions = []
    for filing in filings:
        for transaction in filing:
            accepted = datetime.fromisoformat(transaction['accepted_datetime']).astimezone(EASTERN).date()
            if accepted > as_of:
                continue
            if transaction.get('document_type') == '4/A':
                amended_issuers.add(transaction['issuer_cik'])
            transaction_date = transaction_calendar_date(transaction['transaction_date'])
            if cutoff <= transaction_date <= as_of and eligible_purchase(transaction):
                transactions.append(dict(transaction, transaction_date=str(transaction_date)))
    groups = {}
    for transaction in transactions:
        issuer = transaction['issuer_cik']
        if issuer in amended_issuers:
            continue
        groups.setdefault(issuer, []).append(transaction)
    clusters = []
    for issuer, purchases in groups.items():
        buyers = {str(int(t['insider_cik'])) for t in purchases}
        if len(buyers) >= minimum:
            clusters.append({
                'issuer_cik': issuer,
                'ticker': purchases[0]['issuer_ticker'],
                'company': purchases[0]['issuer_name'],
                'buyers': len(buyers),
                'total_value': sum(t['total_value'] for t in purchases),
                'purchases': purchases,
            })
    return sorted(clusters, key=lambda c: (-c['buyers'], -c['total_value']))


def collect_filing(fetcher, parser, filing):
    accession = filing['accession_number']
    for cik in filing.get('ciks') or [filing['cik']]:
        xml = fetcher.fetch_form4_xml(cik, accession)
        if not xml:
            continue
        root = ET.fromstring(xml)
        if root.tag != 'ownershipDocument':
            raise ValueError('Unexpected ownership document root')
        accepted = fetcher.fetch_acceptance_datetime(cik, accession)
        if not accepted:
            raise ValueError('SEC acceptance timestamp unavailable')
        transactions = parser.parse(xml, accession, filing['updated'][:10], accepted)
        expected = len(root.findall('./nonDerivativeTable/nonDerivativeTransaction'))
        if len(transactions) != expected:
            raise ValueError('Parser did not preserve every non-derivative transaction')
        for transaction in transactions:
            transaction['filing_url'] = (f'https://www.sec.gov/Archives/edgar/data/{cik}/'
                                         f'{accession.replace("-", "")}/{accession}-index.html')
            transaction['transaction_date'] = str(transaction_calendar_date(transaction['transaction_date']))
        # Amendments with only holdings still invalidate automated issuer counting.
        if root.findtext('documentType') == '4/A' and not transactions:
            transactions = [{
                'document_type': '4/A',
                'issuer_cik': root.findtext('./issuer/issuerCik'),
                'accepted_datetime': accepted,
                'transaction_date': filing['updated'][:10],
            }]
        return transactions
    raise ValueError('Could not retrieve filing XML under any listed filer')


def write_report(output, clusters, coverage):
    output.mkdir(parents=True, exist_ok=True)
    payload = {'coverage': coverage, 'clusters': clusters}
    (output / 'report.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    status = 'Collection complete for requested published indexes' if not coverage['errors'] else 'PARTIAL COLLECTION — review errors before using candidates'
    if coverage.get('demo'):
        status = 'DEMO — fictional purchases for testing only'
    sections = [f'<h1>Insider purchase research</h1><p>{html.escape(status)}</p>',
                f'<p>As of {coverage["as_of"]}. Three officers or directors within seven calendar dates. '
                'Positive-price common stock purchases only. Review original filings and footnotes.</p>',
                '<p>Joint-owner filings and detected private deals are excluded. '
                'Issuers with collected amendments require manual review and are excluded. '
                'These filters can miss purchases. Results depend on the requested collection window.</p>']
    for cluster in clusters:
        sections.append(f'<h2>{html.escape(cluster["ticker"] or "Unknown ticker")} — {html.escape(cluster["company"] or "")}</h2>'
                        f'<p>{cluster["buyers"]} buyers · ${cluster["total_value"]:,.2f} reported value</p><ul>')
        for t in cluster['purchases']:
            accession = t['accession_number']
            url = t.get('filing_url', '')
            filing_link = (f'<a href="{html.escape(url)}">SEC filing</a>'
                           if url.startswith('https://www.sec.gov/') else 'Fictional filing' if coverage.get('demo') else 'Filing link unavailable')
            sections.append(f'<li>{html.escape(t["insider_name"] or "Unknown")} · '
                            f'{html.escape(t["insider_title"] or "Director")} · {t["transaction_date"]} · '
                            f'${t["total_value"]:,.2f} · {filing_link}</li>')
        sections.append('</ul>')
    if not clusters:
        sections.append('<p>No qualifying clusters in collected filings.</p>')
    sections.append('<h2>Collection details</h2><pre>' + html.escape(json.dumps(coverage, indent=2)) + '</pre>')
    (output / 'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
                                       '<title>Insider purchase research</title><body>' + ''.join(sections) + '</body></html>', encoding='utf-8')


def main():
    today = datetime.now(EASTERN).date()
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument('--demo', action='store_true', help='Create a clearly labeled report from fictional purchases')
    argument_parser.add_argument('--start', type=date.fromisoformat, default=today - timedelta(days=14))
    argument_parser.add_argument('--end', type=date.fromisoformat, default=today - timedelta(days=1))
    argument_parser.add_argument('--database', default='research_filings.db')
    argument_parser.add_argument('--output', type=Path, default=Path('research_results'))
    args = argument_parser.parse_args()
    if not args.start <= args.end < today:
        argument_parser.error('Use a completed range with start <= end < today in US Eastern time')
    if args.demo:
        fixture = Path(__file__).parent / 'tests/fixtures/real_form4_with_purchase.xml'
        root = ET.fromstring(fixture.read_text())
        parser = Form4Parser()
        filings = []
        for number in range(1, 4):
            root.find('./issuer/issuerName').text = 'Fictional Demonstration Company'
            root.find('./issuer/issuerTradingSymbol').text = 'DEMO'
            owner = root.find('./reportingOwner')
            owner.find('./reportingOwnerId/rptOwnerCik').text = str(number)
            owner.find('./reportingOwnerId/rptOwnerName').text = f'Fictional Buyer {number}'
            owner.find('./reportingOwnerRelationship/isOfficer').text = '1'
            for element in root.findall('.//transactionDate/value'):
                element.text = str(args.end)
            rows = parser.parse(ET.tostring(root, encoding='unicode'),
                                f'0000000000-00-{number:06d}', str(args.end),
                                f'{args.end}T17:00:00-05:00')
            filings.append(rows)
        clusters = find_clusters(filings, args.end)
        write_report(args.output, clusters, {'as_of': str(args.end), 'errors': [], 'demo': True})
        print(f'Fictional demo report: {args.output / "report.html"}')
        return 0
    user_agent = EDGAR_CONFIG['user_agent']
    if '@' not in user_agent or 'example.com' in user_agent or 'your_email' in user_agent:
        argument_parser.error('Set SEC_USER_AGENT to your name and contact email')
    fetcher, parser = EdgarFetcher(), Form4Parser()
    coverage = {'start': str(args.start), 'as_of': str(args.end), 'indexes': [], 'errors': [], 'stored_filings': 0}
    with sqlite3.connect(args.database, timeout=60) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS research_filings (accession TEXT PRIMARY KEY, filing_date TEXT, payload TEXT NOT NULL)')
        current = args.start
        while current <= args.end:
            if current.weekday() < 5:
                try:
                    # Existing date-index reader provides full-day coverage, not a rolling RSS sample.
                    filings = fetcher.get_filings_for_date(str(current), form_type='4')
                    amendments = fetcher.get_filings_for_date(str(current), form_type='4/A')
                    coverage['indexes'].append({'date': str(current), 'filings': len(filings), 'amendments': len(amendments)})
                    for filing in filings + amendments:
                        accession = filing['accession_number']
                        if connection.execute('SELECT 1 FROM research_filings WHERE accession=?', (accession,)).fetchone():
                            continue
                        try:
                            transactions = collect_filing(fetcher, parser, filing)
                            # One transaction commits both the complete snapshot and processed state.
                            connection.execute('INSERT OR IGNORE INTO research_filings VALUES (?,?,?)',
                                               (accession, str(current), json.dumps(transactions)))
                            connection.commit()
                        except Exception as error:
                            coverage['errors'].append({'accession': accession, 'error': str(error)})
                except Exception as error:
                    # Includes missing indexes on federal holidays. Never silently label these complete.
                    coverage['errors'].append({'date': str(current), 'error': str(error)})
                print(f'{current}: collection checked', flush=True)
            current += timedelta(days=1)
        rows = connection.execute('SELECT payload FROM research_filings WHERE filing_date BETWEEN ? AND ?',
                                  (str(args.start), str(args.end))).fetchall()
    coverage['stored_filings'] = len(rows)
    clusters = find_clusters([json.loads(row[0]) for row in rows], args.end)
    write_report(args.output, clusters, coverage)
    print(f'{len(clusters)} candidate clusters. Report: {args.output / "report.html"}')
    return 1 if coverage['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
