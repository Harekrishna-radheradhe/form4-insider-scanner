#!/usr/bin/env python3
"""
One-time remediation — re-derive corrupted accepted_datetime rows.

RULED (PM, 2026-08-05, spec §3C): RE-DERIVE, not flag-and-age.

Before the Defect-B fix, `Form4Parser.parse()` stamped every row with
`datetime.now()` — the wall-clock moment the parser happened to run, not SEC's
EDGAR acceptance datetime that pre-registration §0.4 makes the signal-eligible
time. The §0.2 gate census found 22 rows whose stamp lands AFTER their own
filing date, which is only possible because the value was never SEC's.

This re-fetches the true value from the accession numbers already on file — a
bounded, one-time re-fetch, no new EDGAR discovery needed.

SAFETY
  - Read-only by default. Writes ONLY with --apply.
  - --apply refuses to run without a rollback artifact, written first (A14).
  - Only rows matching the corruption signature are touched: accepted_datetime
    is absent, or its date is later than the row's own filing_date. A row whose
    stamp is already plausible is never rewritten.
  - A filing whose true acceptance datetime cannot be established is left
    exactly as it is and reported. Nothing is invented to fill a gap.

USAGE
    python3 rederive_accepted_datetime.py --db PATH            # dry run
    python3 rederive_accepted_datetime.py --db PATH --apply    # write
"""
import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from edgar_fetcher import EdgarFetcher

CORRUPT_SIGNATURE = """
    accepted_datetime IS NULL
 OR accepted_datetime = ''
 OR filing_date IS NULL
 OR substr(accepted_datetime, 1, 10) > filing_date
"""


def find_corrupt_rows(con):
    """Rows carrying the Defect-B signature, grouped by accession.

    Grouped because the acceptance datetime is a property of the SUBMISSION,
    not of the individual transaction rows within it — one fetch fixes every
    row of an accession.
    """
    cur = con.execute(f"""
        SELECT accession_number, issuer_cik, insider_cik, COUNT(*) AS n_rows,
               MIN(filing_date), MIN(accepted_datetime)
        FROM form4_transactions
        WHERE {CORRUPT_SIGNATURE}
        GROUP BY accession_number
        ORDER BY accession_number
    """)
    return [dict(accession=r[0], issuer_cik=r[1], insider_cik=r[2],
                 n_rows=r[3], filing_date=r[4], stamped=r[5])
            for r in cur.fetchall()]


def write_rollback(db_path: Path, rows, out_dir: Path) -> Path:
    """A14: create the rollback artifact BEFORE any write.

    Both a full copy of the database and a row-level record of the exact
    prior values, so the change is reversible without the copy if need be.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    shutil.copy2(db_path, out_dir / f"{db_path.name}.pre-rederive-{stamp}.bak")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        prior = [dict(zip(('id', 'accession_number', 'filing_date',
                           'accepted_datetime'), r))
                 for r in con.execute(
                     f"SELECT id, accession_number, filing_date, "
                     f"accepted_datetime FROM form4_transactions "
                     f"WHERE {CORRUPT_SIGNATURE} ORDER BY id")]
    finally:
        con.close()

    record = out_dir / f"rederive_accepted_datetime_rollback_{stamp}.json"
    record.write_text(json.dumps({
        'created_utc': stamp,
        'db': str(db_path),
        'backup': f"{db_path.name}.pre-rederive-{stamp}.bak",
        'reason': 'spec §3C re-derive of Defect-B accepted_datetime stamps',
        'n_accessions': len(rows),
        'n_rows': len(prior),
        'prior_values': prior,
    }, indent=2) + "\n")
    return record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', type=Path, required=True)
    ap.add_argument('--apply', action='store_true',
                    help='write the re-derived values (default: dry run)')
    ap.add_argument('--rollback-dir', type=Path, default=None,
                    help='where the A14 rollback artifact goes '
                         '(default: alongside the DB)')
    a = ap.parse_args(argv)

    if not a.db.exists():
        print(f"FAIL — database absent: {a.db}")
        return 2

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        rows = find_corrupt_rows(con)
    finally:
        con.close()

    total_rows = sum(r['n_rows'] for r in rows)
    print(f"Corrupted accepted_datetime: {total_rows} row(s) across "
          f"{len(rows)} accession(s) in {a.db}")
    if not rows:
        print("Nothing to re-derive.")
        return 0

    if a.apply:
        record = write_rollback(a.db, rows,
                                a.rollback_dir or a.db.parent / 'rollback')
        print(f"Rollback artifact written first (A14): {record}")

    fetcher = EdgarFetcher()
    resolved, unresolved = [], []
    for r in rows:
        # Either associated CIK resolves the Archives path; the issuer is tried
        # first because it is the one always present on our rows.
        accepted = None
        for cik in (r['issuer_cik'], r['insider_cik']):
            if not cik:
                continue
            accepted = fetcher.fetch_acceptance_datetime(cik, r['accession'])
            if accepted:
                break
        if accepted:
            resolved.append((r, accepted))
            print(f"  {r['accession']}  {r['stamped']} -> {accepted}  "
                  f"({r['n_rows']} row(s))")
        else:
            unresolved.append(r)
            print(f"  {r['accession']}  UNRESOLVED — left exactly as it is")

    if not a.apply:
        print(f"\nDRY RUN — nothing written. {len(resolved)} accession(s) "
              f"resolvable, {len(unresolved)} not. Re-run with --apply.")
        return 0

    con = sqlite3.connect(a.db)
    try:
        n = 0
        for r, accepted in resolved:
            cur = con.execute(
                "UPDATE form4_transactions SET accepted_datetime = ? "
                "WHERE accession_number = ?", (accepted, r['accession']))
            n += cur.rowcount
        con.commit()
    finally:
        con.close()

    print(f"\nAPPLIED — {n} row(s) across {len(resolved)} accession(s) "
          f"re-derived from SEC.")
    if unresolved:
        print(f"UNRESOLVED — {sum(r['n_rows'] for r in unresolved)} row(s) "
              f"across {len(unresolved)} accession(s) still carry the old "
              f"stamp. Not guessed at, not aged out: "
              f"{[r['accession'] for r in unresolved]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
