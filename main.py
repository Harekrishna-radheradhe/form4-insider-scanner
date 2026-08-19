#!/usr/bin/env python3
"""
SEC Form 4 Insider Transaction Scanner - Main Script

This script orchestrates the entire workflow:
1. Fetch recent Form 4 filings from SEC EDGAR
2. Parse and store transaction data
3. Analyze for insider buying clusters
4. Check options volume contamination (Phase 4 cross-signal filter)
5. Analyze for insider sell signals (backtest-validated)
6. Send email alerts (or daily status if no clusters/sells)

Phase 4 Result: Options volume is a kill switch for insider signals.
  Insider + options spike (±20d): -11.83% alpha at 20d, 16.7% win rate
  Call-heavy + insider: -13.86% at 20d, 5.9% win rate
  Clean insider clusters: +0.99% at 20d (significant)

Usage:
    python main.py                # Run full scan
    python main.py --dry-run      # Test without sending emails
    python main.py --fetch-only   # Only fetch, no analysis
    python main.py --analyze-only # Only analyze existing data
    python main.py --status-only  # Just send status email

    python main.py --backfill-date 2026-08-17 --dry-run   # discover+parse, no writes
    python main.py --backfill-date 2026-08-17             # backfill that day (MANUAL)
"""

import json
import sys
import os
from datetime import datetime, timezone

from config import EMAIL_CONFIG
from database import (
    initialize_database,
    get_database_stats,
    insert_transaction,
    mark_filing_processed,
    is_filing_processed,
    accession_has_transactions,
    filing_processed_any_spelling,
    get_cluster_candidates,
    get_recent_purchases,
    was_alert_sent,
    record_alert_sent
)
from edgar_fetcher import EdgarFetcher
from form4_parser import Form4Parser
from analyzer import ClusterAnalyzer, SellSignalAnalyzer
from email_reporter import EmailReporter

# Phase 4 cross-signal filter — graceful fallback if module/DB unavailable
try:
    from options_volume_check import check_options_contamination
    OPTIONS_CHECK_AVAILABLE = True
except ImportError as _ovc_err:
    OPTIONS_CHECK_AVAILABLE = False
    print(f"[OPTIONS_CHECK_UNAVAILABLE] options_volume_check import failed: "
          f"{type(_ovc_err).__name__}: {_ovc_err} — Phase-4 contamination filter DISABLED",
          flush=True)


def log_scan_run(scanner, source_status, n_evaluated, n_fired=0, note='', db_path=None):
    """One row per scanner run -> shared signal-intelligence DB.
    Distinguishes 'ran, evaluated 0 (empty/failed source)' from 'did not run at all'.
    Never raises. db_path param exists for offline unit tests."""
    try:
        import os, sqlite3 as _sl
        db = db_path or os.path.expanduser('~/signal_intelligence.db')
        c = _sl.connect(db)
        c.execute('''CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at TEXT DEFAULT CURRENT_TIMESTAMP,
            scanner TEXT, source_status TEXT,
            n_evaluated INTEGER, n_fired INTEGER, note TEXT)''')
        c.execute('INSERT INTO scan_runs (scanner, source_status, n_evaluated, n_fired, note) VALUES (?,?,?,?,?)',
                  (scanner, source_status, n_evaluated, n_fired, note))
        c.commit(); c.close()
    except Exception as _sr_err:
        print(f"[SCAN_RUN_FAIL] {scanner}: {type(_sr_err).__name__}: {_sr_err}", flush=True)


def log_signal_intelligence(scan_date, scanner, ticker, direction, fired,
                             signal_strength=None, signal_bucket=None,
                             regime_filter_passed=None, regime_value=None,
                             score=None):
    """Write one signal_log row to the shared signal-intelligence DB. A failed
    write is surfaced (never swallowed). Merged from PA production drift 2026-07-22."""
    try:
        import sqlite3 as _sl
        db = os.path.expanduser('~/signal_intelligence.db')
        c = _sl.connect(db)
        c.execute('CREATE TABLE IF NOT EXISTS signal_log (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_date TEXT, scanner TEXT, ticker TEXT, direction TEXT, fired INTEGER, signal_strength REAL, signal_bucket TEXT, regime_filter_passed INTEGER, regime_value REAL, score INTEGER, autotrader_acted INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        c.execute('INSERT INTO signal_log (scan_date,scanner,ticker,direction,fired,signal_strength,signal_bucket,regime_filter_passed,regime_value,score) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (scan_date,scanner,ticker,direction,fired,signal_strength,signal_bucket,regime_filter_passed,regime_value,score))
        c.commit(); c.close()
    except Exception as _sl_err:
        print(f"[SIGNAL_LOG_FAIL] {scanner} {ticker}: {type(_sl_err).__name__}: {_sl_err}", flush=True)


def fetch_and_store_filings():
    """Fetch recent Form 4 filings and store transactions."""
    fetcher = EdgarFetcher()
    parser = Form4Parser()

    print("Fetching recent Form 4 filings from SEC EDGAR...")
    try:
        filings = fetcher.get_recent_form4_filings(count=100)
        # get_recent_form4_filings swallows HTTP errors -> []; a non-empty feed
        # is unambiguously OK, an empty one is EMPTY. A hard exception is FETCH_FAIL.
        source_status = 'OK' if filings else 'EMPTY'
    except Exception as e:
        print(f"  EDGAR feed fetch failed: {e}")
        filings = []
        source_status = 'FETCH_FAIL'
    print(f"Found {len(filings)} filings in feed")

    # Candidates evaluated this run = every filing the feed handed us.
    n_evaluated = len(filings)

    filings_processed = 0
    transactions_added = 0
    errors = 0

    for filing in filings:
        accession = filing.get('accession_number')
        if not accession:
            continue

        if is_filing_processed(accession):
            continue

        try:
            cik = filing.get('cik')
            if not cik:
                mark_filing_processed(accession, 'error', 'Missing CIK')
                errors += 1
                continue

            xml_content = fetcher.fetch_form4_xml(cik, accession)
            if not xml_content:
                mark_filing_processed(accession, 'error', 'Could not fetch XML')
                errors += 1
                continue

            filing_date = filing.get('updated', '')[:10] if filing.get('updated') else None

            # §0.4: the signal-eligible time is SEC's EDGAR acceptance
            # datetime, so it is fetched here and threaded in. The parser no
            # longer has a clock to fall back on. A filing whose acceptance
            # time cannot be established is skipped and marked, not ingested
            # with an invented timestamp.
            accepted_datetime = fetcher.fetch_acceptance_datetime(cik, accession)
            if not accepted_datetime:
                print(f"  {accession}: no EDGAR acceptance datetime — skipped "
                      f"rather than stamped with our own clock")
                mark_filing_processed(accession, 'error',
                                      'No EDGAR acceptance datetime')
                errors += 1
                continue

            transactions = parser.parse(xml_content, accession, filing_date,
                                        accepted_datetime)

            for txn in transactions:
                if insert_transaction(txn):
                    transactions_added += 1

            mark_filing_processed(accession, 'success')
            filings_processed += 1

        except Exception as e:
            print(f"  Error processing {accession}: {e}")
            mark_filing_processed(accession, 'error', str(e))
            errors += 1

    print(f"Processed {filings_processed} new filings, added {transactions_added} transactions, {errors} errors")
    return filings_processed, transactions_added, errors, source_status, n_evaluated


# ============================================================
#  DATE-SCOPED BACKFILL  (JOB-20260819-FORM4-BACKFILL-SCRIPT)
# ============================================================
#
# Closes a one-day collection hole using the SAME guarded per-filing path the
# nightly scan uses. Only discovery is new: `get_filings_for_date` reads EDGAR's
# daily index instead of the rolling `getcurrent` feed, which has no date
# parameter and so can never reach a historical day.
#
# Manual operation. Never scheduled, never triggered by the nightly run, and it
# sends no email and runs no analysis — a backfill's row count is not a scanner
# cycle's row count and must not be reported as one.


def _load_checkpoint(path):
    """Accessions already completed by an earlier run of this backfill."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            return list(json.load(fh).get('completed', []))
    except (ValueError, OSError) as err:
        print(f"[CHECKPOINT_UNREADABLE] {path}: {type(err).__name__}: {err} — "
              f"treating as empty; the DB guards still prevent double-inserts",
              flush=True)
        return []


def _save_checkpoint(path, date_str, completed):
    if not path:
        return
    try:
        with open(path, 'w') as fh:
            json.dump({'date': date_str, 'completed': sorted(completed)}, fh)
    except OSError as err:
        print(f"[CHECKPOINT_WRITE_FAIL] {path}: {type(err).__name__}: {err}",
              flush=True)


def backfill_one_filing(fetcher, parser, filing, dry_run=False):
    """Backfill exactly one discovered filing. Returns an outcome dict.

    Two idempotency guards, in order, because they close different holes:
      1. `filing_processed_any_spelling` — the filing completed on an earlier
         run and is recorded in `processed_filings`.
      2. `accession_has_transactions` — rows are present but the filing was
         never marked, i.e. a run died between its inserts and its
         `mark_filing_processed`. Nothing else catches this, and
         `form4_transactions` has no unique constraint to fall back on.

    Everything after the guards is the existing guarded path, unchanged:
    `fetch_form4_xml` (which resolves the document through the filing's own
    `index.json`, never a constructed path) -> `fetch_acceptance_datetime` ->
    `parse` -> `insert_transaction`.
    """
    accession = filing.get('accession_number')
    base = {'accession': accession, 'rows_inserted': 0}

    if filing_processed_any_spelling(accession):
        return dict(base, outcome='skipped_already_processed')
    if accession_has_transactions(accession):
        return dict(base, outcome='skipped_rows_present')

    ciks = [c for c in (filing.get('ciks') or [filing.get('cik')]) if c]
    if not ciks:
        return dict(base, outcome='failed', reason='no filer CIK in index row')

    xml_content = None
    used_cik = None
    for cik in ciks:
        xml_content = fetcher.fetch_form4_xml(cik, accession)
        if xml_content:
            used_cik = cik
            break
    if not xml_content:
        return dict(base, outcome='failed',
                    reason=f'no Form 4 XML under any of {len(ciks)} filer CIK(s)')

    # Same rule as the nightly path: a filing whose acceptance time cannot be
    # established is skipped, never ingested with an invented timestamp.
    accepted_datetime = fetcher.fetch_acceptance_datetime(used_cik, accession)
    if not accepted_datetime:
        return dict(base, outcome='failed',
                    reason='no EDGAR acceptance datetime')

    filing_date = (filing.get('updated') or '')[:10] or None
    try:
        transactions = parser.parse(xml_content, accession, filing_date,
                                    accepted_datetime)
    except Exception as err:
        return dict(base, outcome='failed',
                    reason=f'parse failed: {type(err).__name__}: {err}')

    if dry_run:
        return dict(base, outcome='would_insert', rows_parsed=len(transactions))

    rows = 0
    for txn in transactions:
        if insert_transaction(txn):
            rows += 1
    mark_filing_processed(accession, 'success')
    return dict(base, outcome='inserted', rows_inserted=rows,
                rows_parsed=len(transactions))


def backfill_date(date_str, dry_run=False, form_type='4', checkpoint_path=None):
    """Backfill every filing of `form_type` EDGAR published on `date_str`.

    Reconciles unprompted: discovered == inserted + skipped + failed, exactly.
    Any unexplained gap sets `reconciled` False and is reported as a STOP rather
    than a footnote, and every failure carries its reason.
    """
    fetcher = EdgarFetcher()
    parser = Form4Parser()

    print("#" * 60)
    print(f"# FORM 4 DATE-SCOPED BACKFILL — {date_str} — form type {form_type}")
    print(f"# {'DRY RUN — nothing will be written' if dry_run else 'LIVE — rows will be inserted'}")
    print("#" * 60)

    filings = fetcher.get_filings_for_date(date_str, form_type=form_type)
    print(f"Discovered {len(filings)} distinct {form_type} accessions in "
          f"EDGAR's daily index for {date_str}")

    completed = set(_load_checkpoint(checkpoint_path))
    if completed:
        print(f"Checkpoint: {len(completed)} already completed by an earlier run")

    results = []
    for i, filing in enumerate(filings, 1):
        accession = filing.get('accession_number')
        if accession in completed:
            results.append({'accession': accession, 'rows_inserted': 0,
                            'outcome': 'skipped_checkpoint'})
            continue

        outcome = backfill_one_filing(fetcher, parser, filing, dry_run=dry_run)
        results.append(outcome)

        if outcome['outcome'] in ('inserted', 'would_insert'):
            completed.add(accession)
            if not dry_run:
                _save_checkpoint(checkpoint_path, date_str, completed)
        if i % 50 == 0 or i == len(filings):
            print(f"  {i}/{len(filings)} filings processed", flush=True)

    def _count(*names):
        return sum(1 for r in results if r['outcome'] in names)

    inserted = _count('inserted', 'would_insert')
    skipped = _count('skipped_already_processed', 'skipped_rows_present',
                     'skipped_checkpoint')
    failed = _count('failed')
    failures = [{'accession': r['accession'], 'reason': r.get('reason', '')}
                for r in results if r['outcome'] == 'failed']

    summary = {
        'date': date_str,
        'form_type': form_type,
        'dry_run': dry_run,
        'discovered': len(filings),
        'inserted_filings': inserted,
        'skipped': skipped,
        'failed': failed,
        'inserted_rows': sum(r.get('rows_inserted', 0) for r in results),
        'would_insert_rows': sum(r.get('rows_parsed', 0) for r in results
                                 if r['outcome'] == 'would_insert'),
        'failures': failures,
        'reconciled': len(filings) == inserted + skipped + failed,
    }

    print("\n" + "=" * 60)
    print(f"BACKFILL RECONCILIATION — {date_str} (form type {form_type})")
    print("=" * 60)
    print(f"  accessions found in daily index : {summary['discovered']}")
    print(f"  {'would insert' if dry_run else 'inserted'} (filings)        : {inserted}")
    print(f"  {'would insert' if dry_run else 'inserted'} (rows)           : "
          f"{summary['would_insert_rows'] if dry_run else summary['inserted_rows']}")
    print(f"  skipped (already present)       : {skipped}")
    print(f"  failed                          : {failed}")
    for f in failures:
        print(f"      FAILED {f['accession']}: {f['reason']}")
    print(f"  found == inserted+skipped+failed : "
          f"{summary['discovered']} == {inserted}+{skipped}+{failed} -> "
          f"{'RECONCILED' if summary['reconciled'] else '*** MISMATCH — STOP ***'}")
    if not summary['reconciled']:
        print("  *** Reconciliation FAILED. Do not treat this run as complete. ***")
    print("=" * 60)
    return summary


def _arg_value(flag, argv=None):
    """Value following `flag` in argv, or None. Missing value is fatal, not
    silently ignored — `--backfill-date` with no date must never fall through
    into a normal scan."""
    argv = sys.argv if argv is None else argv
    if flag not in argv:
        return None
    idx = argv.index(flag)
    if idx + 1 >= len(argv) or argv[idx + 1].startswith('--'):
        raise SystemExit(f"{flag} requires a date argument, e.g. "
                         f"{flag} 2026-08-17")
    return argv[idx + 1]


# ============================================================
#  PHASE 4: OPTIONS VOLUME CONTAMINATION CHECK
# ============================================================

def check_cluster_contamination(clusters):
    """
    Check each insider buying cluster for concurrent options volume spikes.

    Phase 4 Backtest (2021-2026, ±20d window):
      - Insider + options spike: -11.83% alpha at 20d, 16.7% win rate
      - Clean insider clusters: +0.99% alpha at 20d
      - Call-heavy + insider: -13.86% at 20d, 5.9% win rate (nuclear)
      - Options spike first → insider buys: -13.15% at 20d, 8.0% win rate

    Attaches 'options_contamination' dict to each cluster for email rendering.
    """
    if not OPTIONS_CHECK_AVAILABLE:
        print("  Options contamination check: SKIPPED (module not available)")
        return

    print("Checking options volume contamination (Phase 4 filter)...")
    contaminated_count = 0
    clean_count = 0
    error_count = 0

    for cluster in clusters:
        ticker = cluster['ticker']
        # Use last purchase date as anchor for the ±20 trading day window
        event_date = cluster.get('last_purchase') or datetime.now().strftime('%Y-%m-%d')

        try:
            if not OPTIONS_CHECK_AVAILABLE:
                raise RuntimeError("options_volume_check unavailable — Phase-4 filter disabled")
            result = check_options_contamination(ticker, event_date)
            cluster['options_contamination'] = result

            if result['contaminated']:
                contaminated_count += 1
                print(f"  ⚠️  {ticker}: CONTAMINATED ({result['max_deviation']:.1f}x, "
                      f"{len(result['anomalies'])} event{'s' if len(result['anomalies']) != 1 else ''})")
            elif result['error']:
                error_count += 1
                print(f"  ⚪ {ticker}: check skipped ({result['error']})")
            else:
                clean_count += 1
                print(f"  ✅ {ticker}: clean")
        except Exception as e:
            error_count += 1
            cluster['options_contamination'] = {
                'contaminated': False, 'anomalies': [], 'max_deviation': 0,
                'signal_types': [], 'warning_html': '', 'warning_text': '',
                'error': str(e)
            }
            print(f"  ❌ {ticker}: ERROR - {e}")

    print(f"  Summary: {contaminated_count} contaminated, {clean_count} clean, "
          f"{error_count} errors/skipped")


def analyze_and_alert(dry_run=False):
    """Analyze for buying clusters, check contamination, and send alerts."""
    analyzer = ClusterAnalyzer()
    reporter = EmailReporter()

    print("Analyzing for insider buying clusters...")
    clusters = analyzer.find_clusters()
    print(f"Found {len(clusters)} potential clusters")

    today = datetime.now().strftime('%Y-%m-%d')
    for c in clusters:
        log_signal_intelligence(
            scan_date=today,
            scanner='F4_BUY_CLUSTER',
            ticker=c['ticker'],
            direction='BUY',
            fired=1,
            signal_strength=c.get('total_purchased'),
            score=c.get('unique_insiders'),
        )
    new_clusters = []

    for cluster in clusters:
        ticker = cluster['ticker']

        if was_alert_sent('cluster', ticker, today):
            print(f"  Already alerted on {ticker} today, skipping")
            continue

        purchases = get_recent_purchases(ticker)
        cluster['purchases'] = purchases
        new_clusters.append(cluster)

    # Phase 4: Check options volume contamination on new clusters
    if new_clusters:
        check_cluster_contamination(new_clusters)

    if new_clusters:
        if dry_run:
            for cluster in new_clusters:
                contam = cluster.get('options_contamination', {})
                tag = " ⚠️ CONTAMINATED" if contam.get('contaminated') else ""
                print(f"  [DRY RUN] Would send BUY alert for "
                      f"{cluster['ticker']}{tag}: "
                      f"{cluster['unique_insiders']} insiders, "
                      f"${cluster['total_purchased']:,.0f}")
        else:
            if reporter.send_cluster_alert(new_clusters):
                for cluster in new_clusters:
                    record_alert_sent('cluster', cluster['ticker'], today,
                                      f"{cluster['unique_insiders']} insiders")
                print(f"  Sent 1 email with {len(new_clusters)} buying clusters")

    return clusters, len(new_clusters)


def analyze_sells_and_alert(dry_run=False):
    """
    Analyze for insider sell signals and send alerts.

    Backtest-validated criteria:
      S1: Officer+Director, $250K-$5M → -2.54% 5d alpha
      S2: Officer or Director, $250K-$5M → -0.50% to -0.86% 5d
    """
    sell_analyzer = SellSignalAnalyzer(lookback_days=3, min_value=50000)
    reporter = EmailReporter()

    print("Analyzing for insider sell signals...")

    # Find all significant sells (for logging)
    all_sells = sell_analyzer.find_sell_signals()
    today = datetime.now().strftime('%Y-%m-%d')
    for s in all_sells:
        _tag = s['tier_tag'].replace('SELL_', '')
        log_signal_intelligence(
            scan_date=today,
            scanner=f'F4_SELL_{_tag}',
            ticker=s['ticker'],
            direction='SHORT',
            fired=1 if s['tier_tag'] in ('S1','S2') else 0,
            signal_strength=s.get('total_value'),
            score=s.get('num_sellers'),
        )
    s1_total = sum(1 for s in all_sells if s['tier_tag'] == 'S1')
    s2_total = sum(1 for s in all_sells if s['tier_tag'] == 'S2')
    watch_total = sum(1 for s in all_sells if s['tier_tag'] == 'SELL_WATCH')
    print(f"  Found {len(all_sells)} tickers with significant sells: "
          f"{s1_total} S1, {s2_total} S2, {watch_total} WATCH")

    # Get only new (un-alerted) S1 and S2 signals
    new_alerts = sell_analyzer.get_new_sell_alerts()

    if new_alerts:
        if dry_run:
            for alert in new_alerts:
                print(f"  [DRY RUN] Would send SELL alert for {alert['ticker']} "
                      f"({alert['tier_tag']}): {alert['num_sellers']} seller(s), "
                      f"${alert['total_value']:,.0f}")
        else:
            if reporter.send_sell_alert(new_alerts):
                sell_analyzer.mark_sell_alerts_sent(new_alerts)
                print(f"  Sent 1 email with {len(new_alerts)} sell signals")
    else:
        print("  No new sell signals to alert on")

    return all_sells, len(new_alerts)


def send_status_report(filings_processed, transactions_added, clusters_found,
                       sells_found=None, dry_run=False):
    """Send daily status email."""
    reporter = EmailReporter()
    stats = get_database_stats()

    status_data = {
        'filings_processed': filings_processed,
        'transactions_added': transactions_added,
        'clusters_found': len(clusters_found) if clusters_found else 0,
        'sells_found': len(sells_found) if sells_found else 0,
        'stats': stats,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    if dry_run:
        _fp = 'not fetched this run' if filings_processed is None else filings_processed
        _ta = 'not fetched this run' if transactions_added is None else transactions_added
        print(f"[DRY RUN] Would send daily status email")
        print(f"  Filings processed: {_fp}")
        print(f"  Transactions added: {_ta}")
        print(f"  Buying clusters found: {status_data['clusters_found']}")
        print(f"  Sell signals found: {status_data['sells_found']}")
        return True
    else:
        # The per-cycle counters travel WITH the running totals. `stats` alone
        # is what let 2026-08-17 look like a normal night: a zero-insert cycle
        # and a healthy one print the same totals.
        return reporter.send_status_report(
            stats,
            rows_added=transactions_added,
            filings_processed=filings_processed,
        )


def main():
    # Backfill mode is a MANUAL data operation, handled before anything else:
    # it deliberately bypasses the weekday guard below (the PM must be able to
    # close a gap on a Saturday), and it sends no email and runs no analysis.
    backfill_target = _arg_value('--backfill-date')
    if backfill_target:
        initialize_database()
        backfill_date(backfill_target, dry_run='--dry-run' in sys.argv)
        return

    # Skip weekends - markets closed
    if datetime.now(timezone.utc).weekday() >= 5:  # 5=Saturday, 6=Sunday
        print("Weekend - skipping scan")
        return

    print("#" * 60)
    print("# FORM 4 INSIDER TRANSACTION SCANNER")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 60)

    # Parse arguments
    dry_run = '--dry-run' in sys.argv
    fetch_only = '--fetch-only' in sys.argv
    analyze_only = '--analyze-only' in sys.argv
    status_only = '--status-only' in sys.argv

    if dry_run:
        print("\n*** DRY RUN MODE - No emails will be sent ***\n")

    # Initialize database
    initialize_database()

    # Show current stats
    stats = get_database_stats()
    print(f"Current database stats:")
    print(f"  Transactions: {stats['total_transactions']}")
    print(f"  Purchases: {stats['total_purchases']}")
    print(f"  Sells: {stats.get('total_sells', 'N/A')}")
    print(f"  Companies: {stats['unique_companies']}")
    print(f"  Options contamination filter: "
          f"{'ENABLED' if OPTIONS_CHECK_AVAILABLE else 'DISABLED'}")

    # None, not 0: analyze-only and status-only never call the fetch, so their
    # delta is UNKNOWN. Initialising to 0 would make every such run look like a
    # zero-insert weekday cycle and fire the alarm nightly until it was ignored.
    filings_processed = None
    transactions_added = None
    clusters = []
    sells = []
    buy_alerts_sent = 0
    sell_alerts_sent = 0
    # Default run-level accounting for modes that don't fetch (analyze/status only):
    # the source is the existing DB, not an EDGAR fetch.
    source_status = 'OK'
    n_evaluated = 0

    # Fetch new filings
    if not analyze_only and not status_only:
        print("\n" + "=" * 40)
        (filings_processed, transactions_added, errors,
         source_status, n_evaluated) = fetch_and_store_filings()

    # Analyze for buying clusters (now includes Phase 4 contamination check)
    if not fetch_only and not status_only:
        print("\n" + "=" * 40)
        clusters, buy_alerts_sent = analyze_and_alert(dry_run=dry_run)

    # Analyze for sell signals
    if not fetch_only and not status_only:
        print("\n" + "=" * 40)
        sells, sell_alerts_sent = analyze_sells_and_alert(dry_run=dry_run)

    # Always send daily heartbeat so watchdog can confirm scanner is alive.
    if not fetch_only and not analyze_only:
        print("\n" + "=" * 40)
        print("Sending daily heartbeat status email...")
        send_status_report(filings_processed, transactions_added, clusters,
                           sells, dry_run=dry_run)

    # Run-level heartbeat to the shared signal-intelligence DB: exactly one row
    # per run so an empty-source run (n_evaluated=0) is distinguishable from a
    # scanner that never ran at all.
    log_scan_run(
        'FORM4', source_status, n_evaluated,
        n_fired=(buy_alerts_sent + sell_alerts_sent),
    )

    print("\n" + "=" * 40)
    print("Scan complete!")


if __name__ == '__main__':
    main()
