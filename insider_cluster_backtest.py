#!/usr/bin/env python3
"""
INSIDER CLUSTER BACKTEST - Full Analysis
=========================================
Loads SEC Form 4 bulk data (2020-2025), identifies insider buying clusters,
fetches forward returns, and analyzes across multiple dimensions.

Data source: ~/Desktop/Claude_Programs/Trading_Programs/Form4_Scanner/SEC_Form4_Data/
Each quarter folder contains: SUBMISSION.tsv, NONDERIV_TRANS.tsv, REPORTINGOWNER.tsv

Usage:
    python3 insider_cluster_backtest.py

Output:
    - insider_backtest_results.db (SQLite database with all results)
    - insider_backtest_report.txt (full analysis report)

Requirements:
    pip3 install pandas yfinance
"""

import os
import sys
import pandas as pd
import numpy as np
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

# ============================================================
#  CONFIGURATION
# ============================================================

DATA_DIR = os.path.expanduser(
    os.environ.get("FORM4_DATA_DIR", "SEC_Form4_Data")
)
OUTPUT_DIR = os.path.expanduser(
    os.environ.get("FORM4_OUTPUT_DIR", "research_results")
)
DB_PATH = os.path.join(OUTPUT_DIR, "insider_backtest_results.db")
REPORT_PATH = os.path.join(OUTPUT_DIR, "insider_backtest_report.txt")

# Cluster detection parameters
CLUSTER_WINDOW_DAYS = 7       # Days to group insider buys
MIN_CLUSTER_SIZE = 3           # Minimum unique insiders to qualify

# Forward return windows (trading days)
RETURN_WINDOWS = [5, 10, 20, 40, 60]

# Rate limiting for yfinance
BATCH_SIZE = 50                # Tickers per batch
BATCH_DELAY = 2                # Seconds between batches


# ============================================================
#  STEP 1: LOAD ALL TSV DATA
# ============================================================

def load_all_quarters(data_dir):
    """Load and merge TSV files from all quarterly folders."""
    
    print("=" * 70)
    print("  STEP 1: LOADING SEC FORM 4 DATA")
    print("=" * 70)
    
    all_submissions = []
    all_transactions = []
    all_owners = []
    quarters_found = 0
    
    # Find all quarter folders
    if not os.path.exists(data_dir):
        print(f"\n  ERROR: Data directory not found: {data_dir}")
        print("  Make sure the SEC Form 4 data is at the expected path.")
        sys.exit(1)
    
    folders = sorted([
        f for f in os.listdir(data_dir)
        if os.path.isdir(os.path.join(data_dir, f)) and 'form' in f.lower()
    ])
    
    # If no 'form' folders, try all subdirectories
    if not folders:
        folders = sorted([
            f for f in os.listdir(data_dir)
            if os.path.isdir(os.path.join(data_dir, f))
            and not f.startswith('.')
        ])
    
    if not folders:
        print(f"\n  ERROR: No quarter folders found in {data_dir}")
        print(f"  Contents: {os.listdir(data_dir)[:10]}")
        sys.exit(1)
    
    print(f"\n  Found {len(folders)} quarter folders")
    
    for folder in folders:
        folder_path = os.path.join(data_dir, folder)
        
        # Check for required TSV files
        sub_file = os.path.join(folder_path, "SUBMISSION.tsv")
        trans_file = os.path.join(folder_path, "NONDERIV_TRANS.tsv")
        owner_file = os.path.join(folder_path, "REPORTINGOWNER.tsv")
        
        if not all(os.path.exists(f) for f in [sub_file, trans_file, owner_file]):
            print(f"  ⚠️  Skipping {folder} — missing TSV files")
            continue
        
        try:
            sub = pd.read_csv(sub_file, sep='\t', dtype=str, low_memory=False)
            trans = pd.read_csv(trans_file, sep='\t', dtype=str, low_memory=False)
            owner = pd.read_csv(owner_file, sep='\t', dtype=str, low_memory=False)
            
            all_submissions.append(sub)
            all_transactions.append(trans)
            all_owners.append(owner)
            quarters_found += 1
            
            print(f"  ✅ {folder}: {len(sub):,} submissions, "
                  f"{len(trans):,} transactions, {len(owner):,} owners")
            
        except Exception as e:
            print(f"  ❌ {folder}: Error — {e}")
    
    if quarters_found == 0:
        print("\n  ERROR: No valid quarter data loaded.")
        sys.exit(1)
    
    # Concatenate all quarters
    submissions = pd.concat(all_submissions, ignore_index=True)
    transactions = pd.concat(all_transactions, ignore_index=True)
    owners = pd.concat(all_owners, ignore_index=True)
    
    print(f"\n  TOTAL LOADED:")
    print(f"    Submissions:  {len(submissions):>10,}")
    print(f"    Transactions: {len(transactions):>10,}")
    print(f"    Owners:       {len(owners):>10,}")
    print(f"    Quarters:     {quarters_found}")
    
    return submissions, transactions, owners


# ============================================================
#  STEP 2: EXTRACT OPEN MARKET PURCHASES
# ============================================================

def extract_purchases(submissions, transactions, owners):
    """
    Filter to open market purchases (TRANS_CODE = 'P') and merge
    with submission/owner data to get ticker, date, amount, and role.
    """
    
    print("\n" + "=" * 70)
    print("  STEP 2: EXTRACTING OPEN MARKET PURCHASES")
    print("=" * 70)
    
    # Standardize column names (SEC sometimes varies case)
    submissions.columns = [c.upper().strip() for c in submissions.columns]
    transactions.columns = [c.upper().strip() for c in transactions.columns]
    owners.columns = [c.upper().strip() for c in owners.columns]
    
    # Filter transactions to open market purchases only
    # TRANS_CODE = 'P' and TRANS_ACQUIRED_DISP_CD = 'A'
    purchases = transactions[
        (transactions['TRANS_CODE'] == 'P') &
        (transactions['TRANS_ACQUIRED_DISP_CD'] == 'A')
    ].copy()
    
    print(f"  Open market purchases: {len(purchases):,} "
          f"(of {len(transactions):,} total transactions)")
    
    # Merge with submissions to get ticker symbol and filing date
    # Join key is ACCESSION_NUMBER
    purchases = purchases.merge(
        submissions[['ACCESSION_NUMBER', 'ISSUERTRADINGSYMBOL', 'ISSUERNAME',
                      'FILING_DATE', 'DOCUMENT_TYPE', 'ISSUERCIK']].drop_duplicates('ACCESSION_NUMBER'),
        on='ACCESSION_NUMBER',
        how='left'
    )
    
    # Merge with owners to get insider name, role, and title
    # SEC bulk data uses: RPTOWNERNAME, RPTOWNER_RELATIONSHIP, RPTOWNER_TITLE
    owner_cols = ['ACCESSION_NUMBER']
    for col in ['RPTOWNERCIK', 'RPTOWNERNAME', 'RPTOWNER_RELATIONSHIP', 'RPTOWNER_TITLE']:
        if col in owners.columns:
            owner_cols.append(col)
    
    # Joint filings do not establish independent purchases by every co-filer.
    owners_for_merge = owners[owner_cols].drop_duplicates(['ACCESSION_NUMBER', 'RPTOWNERCIK'])
    counts = owners_for_merge.groupby('ACCESSION_NUMBER')['RPTOWNERCIK'].transform('size')
    owners_for_merge = owners_for_merge[counts == 1]
    purchases = purchases.merge(owners_for_merge, on='ACCESSION_NUMBER', how='left')
    
    # Clean up key fields
    purchases['TICKER'] = purchases['ISSUERTRADINGSYMBOL'].str.strip().str.upper()
    purchases['TRANS_DATE'] = pd.to_datetime(purchases['TRANS_DATE'], errors='coerce', format='mixed')
    purchases['FILING_DATE'] = pd.to_datetime(purchases['FILING_DATE'], errors='coerce', format='mixed')
    purchases['TRANS_SHARES'] = pd.to_numeric(purchases['TRANS_SHARES'], errors='coerce')
    purchases['TRANS_PRICEPERSHARE'] = pd.to_numeric(
        purchases['TRANS_PRICEPERSHARE'], errors='coerce'
    )
    
    # Calculate dollar value of each purchase
    purchases['DOLLAR_VALUE'] = (
        purchases['TRANS_SHARES'] * purchases['TRANS_PRICEPERSHARE']
    )
    
    # Classify insider role using RPTOWNER_TITLE (officer title) and 
    # RPTOWNER_RELATIONSHIP (Director, Officer, TenPercentOwner, Other)
    purchases['ROLE'] = 'Other'
    
    # First: parse title for specific C-Suite roles (highest priority)
    if 'RPTOWNER_TITLE' in purchases.columns:
        title = purchases['RPTOWNER_TITLE'].str.upper().fillna('')
        purchases.loc[title.str.contains('CEO|CHIEF EXECUTIVE|CHIEF EXECUTVE', na=False), 'ROLE'] = 'CEO'
        purchases.loc[title.str.contains('CFO|CHIEF FINANCIAL', na=False), 'ROLE'] = 'CFO'
        purchases.loc[title.str.contains('COO|CHIEF OPERATING', na=False), 'ROLE'] = 'COO'
        purchases.loc[title.str.contains(r'\bPRESIDENT\b', na=False) &
                      (purchases['ROLE'] == 'Other'), 'ROLE'] = 'President'
        purchases.loc[title.str.contains(r'\bVP\b|VICE PRESIDENT', na=False) &
                      (purchases['ROLE'] == 'Other'), 'ROLE'] = 'VP'
    
    # Second: use RPTOWNER_RELATIONSHIP for Director and 10% Owner
    if 'RPTOWNER_RELATIONSHIP' in purchases.columns:
        rel = purchases['RPTOWNER_RELATIONSHIP'].str.upper().fillna('')
        # Officer without a parsed title → generic Officer
        purchases.loc[
            rel.str.contains('OFFICER', na=False) & (purchases['ROLE'] == 'Other'),
            'ROLE'
        ] = 'Officer'
        # Director (only if not already classified as C-Suite)
        purchases.loc[
            rel.str.contains('DIRECTOR', na=False) & (purchases['ROLE'] == 'Other'),
            'ROLE'
        ] = 'Director'
        # 10% Owner
        purchases.loc[
            rel.str.contains('TENPERCENTOWNER', na=False) & (purchases['ROLE'] == 'Other'),
            'ROLE'
        ] = '10% Owner'
    
    # Keep the historical universe close to the live candidate definition.
    # Footnotes are not loaded here, so private-deal classification still needs review.
    purchases = purchases[purchases['DOCUMENT_TYPE'].eq('4')]
    purchases = purchases[
        purchases['SECURITY_TITLE'].str.contains(
            r'\b(?:common stock|common shares|ordinary shares)\b', case=False, na=False)
        & ~purchases['SECURITY_TITLE'].str.contains('preferred|unit|warrant', case=False, na=False)
        & purchases['RPTOWNER_RELATIONSHIP'].str.contains('OFFICER|DIRECTOR', case=False, na=False)
    ]

    # Drop rows without essential data
    before = len(purchases)
    purchases = purchases.dropna(subset=['TICKER', 'TRANS_DATE', 'TRANS_SHARES', 'FILING_DATE', 'RPTOWNERNAME', 'RPTOWNERCIK'])
    purchases = purchases[purchases['TICKER'].str.len() <= 5]  # Valid tickers only
    purchases = purchases[(purchases['TRANS_SHARES'] > 0) &
                          (purchases['TRANS_PRICEPERSHARE'] > 0)]
    
    print(f"  After cleanup: {len(purchases):,} purchases ({before - len(purchases):,} dropped)")
    if purchases.empty:
        return purchases
    print(f"  Date range: {purchases['TRANS_DATE'].min().date()} to "
          f"{purchases['TRANS_DATE'].max().date()}")
    print(f"  Unique tickers: {purchases['TICKER'].nunique():,}")
    print(f"  Unique insiders: {purchases['RPTOWNERNAME'].nunique():,}" 
          if 'RPTOWNERNAME' in purchases.columns else "")
    
    # Role breakdown
    print(f"\n  Role breakdown:")
    for role, count in purchases['ROLE'].value_counts().items():
        print(f"    {role:15s}: {count:>8,}")
    
    return purchases


# ============================================================
#  STEP 3: DETECT BUYING CLUSTERS
# ============================================================

def detect_clusters(purchases, window_days=7, min_insiders=3):
    """Evaluate only purchases publicly disclosed by each filing date.

    SEC bulk data supplies filing dates, so signals become eligible after that
    day's close. Entry is the next session's open. This deliberately ignores
    intraday availability. All cluster features use the same information cutoff.
    """
    required = {'TICKER', 'TRANS_DATE', 'FILING_DATE', 'RPTOWNERNAME'}
    if not required.issubset(purchases.columns):
        raise ValueError(f"Missing required disclosure fields: {required - set(purchases.columns)}")
    purchases = purchases.dropna(subset=list(required)).copy()
    purchases['FILING_DATE'] = pd.to_datetime(purchases['FILING_DATE']).dt.normalize()
    purchases['TRANS_DATE'] = pd.to_datetime(purchases['TRANS_DATE']).dt.normalize()
    clusters = []
    for ticker, ticker_buys in purchases.groupby('TICKER'):
        last_signal = None
        for signal_date in sorted(ticker_buys['FILING_DATE'].unique()):
            signal_date = pd.Timestamp(signal_date)
            # Seven calendar dates inclusive, with no future transactions.
            window_start = signal_date - timedelta(days=window_days - 1)
            window_buys = ticker_buys[
                (ticker_buys['FILING_DATE'] <= signal_date) &
                (ticker_buys['TRANS_DATE'] >= window_start) &
                (ticker_buys['TRANS_DATE'] <= signal_date)
            ]
            identity = 'RPTOWNERCIK' if 'RPTOWNERCIK' in window_buys.columns else 'RPTOWNERNAME'
            unique_insiders = window_buys[identity].nunique()
            if unique_insiders < min_insiders:
                continue
            # Suppress repeat signals using only the previously emitted signal.
            if last_signal is not None and (signal_date - last_signal).days < window_days:
                continue
            roles = sorted(window_buys['ROLE'].dropna().unique())
            total_shares = window_buys['TRANS_SHARES'].sum()
            total_dollars = window_buys['DOLLAR_VALUE'].sum()
            clusters.append({
                'ticker': ticker,
                'company': window_buys['ISSUERNAME'].iloc[0],
                'cluster_start': window_buys['TRANS_DATE'].min(),
                'cluster_end': window_buys['TRANS_DATE'].max(),
                'signal_date': signal_date,
                'num_insiders': unique_insiders,
                'num_transactions': len(window_buys),
                'total_shares': total_shares,
                'total_dollars': total_dollars,
                'avg_price': total_dollars / total_shares,
                'roles': ','.join(roles),
                'has_csuite': bool(set(roles) & {'CEO', 'CFO', 'COO', 'President'}),
                'has_ceo': 'CEO' in roles,
                'has_cfo': 'CFO' in roles,
                'year': signal_date.year,
            })
            last_signal = signal_date
    return pd.DataFrame(clusters)


# ============================================================
#  STEP 4: FETCH FORWARD RETURNS
# ============================================================

def fetch_forward_returns(clusters_df, return_windows=[5, 10, 20, 40, 60]):
    """
    For each cluster signal, fetch the stock price on the signal date
    and at each forward window, plus SPY as benchmark.
    """
    
    try:
        import yfinance as yf
    except ImportError:
        print("  ERROR: yfinance not installed. Run: pip3 install yfinance")
        sys.exit(1)
    
    print("\n" + "=" * 70)
    print("  STEP 4: FETCHING FORWARD RETURNS")
    print(f"  Windows: {return_windows} trading days")
    print(f"  Signals to process: {len(clusters_df):,}")
    print("=" * 70)
    
    # Get unique tickers + SPY
    tickers = list(clusters_df['ticker'].unique())
    all_tickers = tickers + ['SPY']
    
    # Determine date range needed (signal dates + buffer for forward returns)
    min_date = clusters_df['signal_date'].min() - timedelta(days=10)
    max_date = clusters_df['signal_date'].max() + timedelta(days=max(return_windows) * 2)
    
    # Cap at today
    today = datetime.now()
    if max_date > today:
        max_date = today
    
    print(f"  Fetching price data for {len(all_tickers):,} tickers")
    print(f"  Date range: {min_date.date()} to {max_date.date()}")
    
    # Fetch in batches to avoid rate limits
    all_prices = {}
    all_opens = {}
    failed_tickers = []
    
    for batch_start in range(0, len(all_tickers), BATCH_SIZE):
        batch = all_tickers[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(all_tickers) + BATCH_SIZE - 1) // BATCH_SIZE
        
        print(f"  Batch {batch_num}/{total_batches}: "
              f"Fetching {len(batch)} tickers...", end='', flush=True)
        
        try:
            data = yf.download(
                batch,
                start=min_date.strftime('%Y-%m-%d'),
                end=max_date.strftime('%Y-%m-%d'),
                progress=False,
                auto_adjust=True,
                threads=True
            )
            
            if isinstance(data.columns, pd.MultiIndex):
                # Multiple tickers returned
                close = data['Close']
                for t in batch:
                    if t in close.columns and close[t].notna().sum() > 0:
                        all_prices[t] = close[t].dropna()
                        all_opens[t] = data['Open'][t].dropna()
                    else:
                        failed_tickers.append(t)
            elif len(batch) == 1:
                # Single ticker
                if 'Close' in data.columns and data['Close'].notna().sum() > 0:
                    all_prices[batch[0]] = data['Close'].dropna()
                    all_opens[batch[0]] = data['Open'].dropna()
                else:
                    failed_tickers.append(batch[0])
            
            fetched = sum(1 for t in batch if t in all_prices)
            print(f" got {fetched}/{len(batch)}")
            
        except Exception as e:
            print(f" ERROR: {e}")
            failed_tickers.extend(batch)
        
        if batch_start + BATCH_SIZE < len(all_tickers):
            time.sleep(BATCH_DELAY)
    
    print(f"\n  Price data fetched: {len(all_prices):,} tickers")
    print(f"  Failed/missing: {len(failed_tickers):,} tickers")
    
    # Calculate forward returns for each cluster
    spy_prices = all_prices.get('SPY')
    if spy_prices is None:
        print("  WARNING: Could not fetch SPY data. Benchmark returns unavailable.")
    
    results = []
    skipped = 0
    
    for idx, row in clusters_df.iterrows():
        ticker = row['ticker']
        signal_date = row['signal_date']
        
        if ticker not in all_prices:
            skipped += 1
            continue
        
        prices = all_prices[ticker]
        
        # Use the first available session strictly AFTER disclosure day.
        opens = all_opens[ticker]
        entry_candidates = opens[opens.index > signal_date]
        if entry_candidates.empty:
            skipped += 1
            continue
        entry_price_date = entry_candidates.index[0]
        entry_price = entry_candidates.iloc[0]
        if entry_price_date not in prices.index or entry_price <= 0:
            skipped += 1
            continue
        entry_pos = prices.index.get_loc(entry_price_date)

        result = {
            'ticker': ticker,
            'company': row['company'],
            'signal_date': signal_date,
            'entry_date': entry_price_date,
            'entry_price': entry_price,
            'num_insiders': row['num_insiders'],
            'num_transactions': row['num_transactions'],
            'total_dollars': row['total_dollars'],
            'avg_price': row['avg_price'],
            'roles': row['roles'],
            'has_csuite': row['has_csuite'],
            'has_ceo': row['has_ceo'],
            'has_cfo': row['has_cfo'],
        }
        
        # Calculate returns at each window
        for window in return_windows:
            exit_pos = entry_pos + window - 1
            
            # Stock return
            if exit_pos < len(prices):
                exit_price = prices.iloc[exit_pos]
                ret = ((exit_price / entry_price) - 1) * 100
                result[f'ret_{window}d'] = ret
            else:
                result[f'ret_{window}d'] = None
            
            # SPY benchmark return (same dates)
            if spy_prices is not None and exit_pos < len(prices):
                exit_date = prices.index[exit_pos]
                
                spy_opens = all_opens.get('SPY')
                if (spy_opens is not None and entry_price_date in spy_opens.index
                        and exit_date in spy_prices.index):
                    spy_ret = ((spy_prices.loc[exit_date] / spy_opens.loc[entry_price_date]) - 1) * 100
                    result[f'spy_{window}d'] = spy_ret
                    result[f'alpha_{window}d'] = ret - spy_ret
                else:
                    result[f'spy_{window}d'] = None
                    result[f'alpha_{window}d'] = None
            else:
                result[f'spy_{window}d'] = None
                result[f'alpha_{window}d'] = None
        
        results.append(result)
    
    results_df = pd.DataFrame(results)
    
    print(f"\n  Results calculated: {len(results_df):,}")
    print(f"  Skipped (no price data): {skipped:,}")
    
    return results_df


# ============================================================
#  STEP 5: MULTI-DIMENSIONAL ANALYSIS
# ============================================================

def analyze_results(results_df, return_windows=[5, 10, 20, 40, 60]):
    """Run the full analysis matrix and generate report."""
    
    print("\n" + "=" * 70)
    print("  STEP 5: ANALYZING RESULTS")
    print("=" * 70)
    
    report_lines = []
    
    def rpt(text=""):
        print(text)
        report_lines.append(text)
    
    def section(title):
        rpt("\n" + "─" * 70)
        rpt(f"  {title}")
        rpt("─" * 70)
    
    def stats_table(df, label=""):
        """Print stats for a subset of results."""
        if len(df) < 5:
            rpt(f"  {label}: Too few signals ({len(df)}) for meaningful analysis")
            return
        
        rpt(f"\n  {label} (n={len(df):,})")
        header = f"  {'Window':>8s} | {'Avg Ret':>8s} | {'Med Ret':>8s} | {'Win%':>6s} | {'Avg Alpha':>10s} | {'Alpha Win%':>10s} | {'Std Dev':>8s}"
        rpt(header)
        rpt(f"  {'-'*8} | {'-'*8} | {'-'*8} | {'-'*6} | {'-'*10} | {'-'*10} | {'-'*8}")
        
        for w in return_windows:
            col = f'ret_{w}d'
            alpha_col = f'alpha_{w}d'
            
            if col not in df.columns:
                continue
            
            valid = df[col].dropna()
            if len(valid) == 0:
                continue
            
            avg_ret = valid.mean()
            med_ret = valid.median()
            win_pct = (valid > 0).mean() * 100
            std_dev = valid.std()
            
            if alpha_col in df.columns:
                alpha_valid = df[alpha_col].dropna()
                avg_alpha = alpha_valid.mean() if len(alpha_valid) > 0 else float('nan')
                alpha_win = (alpha_valid > 0).mean() * 100 if len(alpha_valid) > 0 else float('nan')
            else:
                avg_alpha = float('nan')
                alpha_win = float('nan')
            
            rpt(f"  {w:>6d}d | {avg_ret:>+7.2f}% | {med_ret:>+7.2f}% | {win_pct:>5.1f}% | "
                f"{avg_alpha:>+9.2f}% | {alpha_win:>9.1f}% | {std_dev:>7.2f}%")
    
    # ── Overall Results ──
    rpt("=" * 70)
    rpt("  INSIDER CLUSTER BACKTEST — FULL RESULTS")
    rpt(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    rpt(f"  Total signals: {len(results_df):,}")
    rpt(f"  Date range: {results_df['signal_date'].min().date()} to "
        f"{results_df['signal_date'].max().date()}")
    rpt("=" * 70)
    
    section("A. OVERALL PERFORMANCE")
    stats_table(results_df, "All Clusters")
    
    # ── By Cluster Size ──
    section("B. BY CLUSTER SIZE (number of unique insiders)")
    for size in sorted(results_df['num_insiders'].unique()):
        subset = results_df[results_df['num_insiders'] == size]
        stats_table(subset, f"{size} insiders")
    
    # Also test thresholds
    for threshold in [3, 4, 5, 7, 10]:
        subset = results_df[results_df['num_insiders'] >= threshold]
        if len(subset) >= 5:
            stats_table(subset, f"{threshold}+ insiders")
    
    # ── By Dollar Amount ──
    section("C. BY TOTAL DOLLAR VALUE OF CLUSTER")
    
    dollar_bins = [
        (0, 100_000, "Under $100K"),
        (100_000, 500_000, "$100K - $500K"),
        (500_000, 1_000_000, "$500K - $1M"),
        (1_000_000, 5_000_000, "$1M - $5M"),
        (5_000_000, float('inf'), "$5M+"),
    ]
    
    for low, high, label in dollar_bins:
        subset = results_df[
            (results_df['total_dollars'] >= low) &
            (results_df['total_dollars'] < high)
        ]
        stats_table(subset, label)
    
    # ── By Insider Role ──
    section("D. BY INSIDER ROLE INVOLVEMENT")
    
    # C-Suite involved vs not
    csuite = results_df[results_df['has_csuite'] == True]
    no_csuite = results_df[results_df['has_csuite'] == False]
    stats_table(csuite, "C-Suite involved (CEO/CFO/COO/President)")
    stats_table(no_csuite, "No C-Suite (Directors/10% Owners/Other only)")
    
    # CEO specifically
    ceo = results_df[results_df['has_ceo'] == True]
    if len(ceo) >= 5:
        stats_table(ceo, "CEO involved")
    
    # ── By Market Context (prior return) ──
    section("E. BY STOCK CONTEXT (entry price vs cluster avg price)")
    
    if 'entry_price' in results_df.columns and 'avg_price' in results_df.columns:
        results_df['price_vs_cluster'] = (
            (results_df['entry_price'] / results_df['avg_price']) - 1
        ) * 100
        
        beaten_down = results_df[results_df['price_vs_cluster'] < -10]
        neutral = results_df[
            (results_df['price_vs_cluster'] >= -10) &
            (results_df['price_vs_cluster'] <= 10)
        ]
        elevated = results_df[results_df['price_vs_cluster'] > 10]
        
        stats_table(beaten_down, "Entry price >10% below cluster avg (beaten down)")
        stats_table(neutral, "Entry price within 10% of cluster avg (neutral)")
        stats_table(elevated, "Entry price >10% above cluster avg (elevated)")
    
    # ── By Year ──
    section("F. BY YEAR (market regime)")
    results_df['year'] = results_df['signal_date'].dt.year
    for year in sorted(results_df['year'].unique()):
        subset = results_df[results_df['year'] == year]
        stats_table(subset, str(year))
    
    # ── Top and Bottom Signals ──
    section("G. NOTABLE SIGNALS")
    
    if 'ret_20d' in results_df.columns:
        valid_20d = results_df.dropna(subset=['ret_20d'])
        
        rpt("\n  Top 10 best 20-day returns:")
        rpt(f"  {'Ticker':>8s} | {'Signal Date':>12s} | {'Insiders':>8s} | "
            f"{'$Value':>12s} | {'20d Ret':>8s} | {'Alpha':>8s}")
        rpt(f"  {'-'*8} | {'-'*12} | {'-'*8} | {'-'*12} | {'-'*8} | {'-'*8}")
        
        for _, row in valid_20d.nlargest(10, 'ret_20d').iterrows():
            alpha = row.get('alpha_20d', float('nan'))
            rpt(f"  {row['ticker']:>8s} | {row['signal_date'].date()} | "
                f"{row['num_insiders']:>8.0f} | "
                f"${row['total_dollars']:>10,.0f} | "
                f"{row['ret_20d']:>+7.2f}% | {alpha:>+7.2f}%")
        
        rpt("\n  Top 10 worst 20-day returns:")
        rpt(f"  {'Ticker':>8s} | {'Signal Date':>12s} | {'Insiders':>8s} | "
            f"{'$Value':>12s} | {'20d Ret':>8s} | {'Alpha':>8s}")
        rpt(f"  {'-'*8} | {'-'*12} | {'-'*8} | {'-'*12} | {'-'*8} | {'-'*8}")
        
        for _, row in valid_20d.nsmallest(10, 'ret_20d').iterrows():
            alpha = row.get('alpha_20d', float('nan'))
            rpt(f"  {row['ticker']:>8s} | {row['signal_date'].date()} | "
                f"{row['num_insiders']:>8.0f} | "
                f"${row['total_dollars']:>10,.0f} | "
                f"{row['ret_20d']:>+7.2f}% | {alpha:>+7.2f}%")
    
    # ── Summary Verdict ──
    section("H. SUMMARY VERDICT")
    
    for w in return_windows:
        col = f'alpha_{w}d'
        if col in results_df.columns:
            valid = results_df[col].dropna()
            if len(valid) > 0:
                avg = valid.mean()
                win = (valid > 0).mean() * 100
                if avg > 0.5 and win > 52:
                    verdict = "✅ SIGNAL CONFIRMED"
                elif avg > 0 and win > 50:
                    verdict = "⚠️  WEAK SIGNAL"
                else:
                    verdict = "❌ NO SIGNAL"
                rpt(f"  {w:>3d}-day: Alpha {avg:>+.2f}%, Win rate {win:.1f}%  →  {verdict}")
    
    return report_lines


# ============================================================
#  STEP 6: SAVE RESULTS
# ============================================================

def save_results(results_df, clusters_df, report_lines):
    """Save to SQLite database and text report."""
    
    print("\n" + "=" * 70)
    print("  STEP 6: SAVING RESULTS")
    print("=" * 70)
    
    # Save to SQLite
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    results_df.to_sql('backtest_results', conn, if_exists='replace', index=False)
    clusters_df.to_sql('clusters', conn, if_exists='replace', index=False)
    conn.close()
    print(f"  Database saved: {DB_PATH}")
    
    # Save report
    with open(REPORT_PATH, 'w') as f:
        f.write('\n'.join(report_lines))
    print(f"  Report saved: {REPORT_PATH}")


# ============================================================
#  MAIN
# ============================================================

def main():
    start_time = time.time()
    
    print("\n" + "=" * 70)
    print("  INSIDER CLUSTER BACKTEST")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Step 1: Load data
    submissions, transactions, owners = load_all_quarters(DATA_DIR)
    
    # Step 2: Extract purchases
    purchases = extract_purchases(submissions, transactions, owners)
    
    # Step 3: Detect clusters
    clusters_df = detect_clusters(
        purchases, 
        window_days=CLUSTER_WINDOW_DAYS,
        min_insiders=MIN_CLUSTER_SIZE
    )
    
    if len(clusters_df) == 0:
        print("\n  No clusters found. Exiting.")
        return
    
    # Step 4: Fetch returns
    results_df = fetch_forward_returns(clusters_df, return_windows=RETURN_WINDOWS)
    
    if len(results_df) == 0:
        print("\n  No results with price data. Exiting.")
        return
    
    # Step 5: Analyze
    report_lines = analyze_results(results_df, return_windows=RETURN_WINDOWS)
    
    # Step 6: Save
    save_results(results_df, clusters_df, report_lines)
    
    elapsed = time.time() - start_time
    print(f"\n  Total runtime: {elapsed/60:.1f} minutes")
    print("  Done!")


if __name__ == '__main__':
    main()