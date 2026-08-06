#!/usr/bin/env python3
"""
One-time remediation — backfill code-P price_per_share rows booked as 0.0.

RULED (PM, 2026-08-06, R12): scope is **code-P only** — the 11 purchase
accessions / 13 rows found by the §1.1 census
(`research/insiders_price_zero_census_20260806.json` in gmc_engine).

Before the Defect-A fix (`63f7558`), `_parse_non_derivative_transaction` wrote
price_per_share = 0 when a filing carried no `<transactionPricePerShare>`
value, so total_value became shares x 0 = 0.0 — indistinguishable from "the
insider transacted at zero dollars", which never happens on an open-market
purchase. Rows parsed before that fix were never backfilled; the 2026-08-05
remediation ruling covered `accepted_datetime` only.

WHAT IS DELIBERATELY OUT OF SCOPE
  The census classified all 3,534 stored zero rows against their source
  filings. Only 814 are defects; **2,701 are legitimate explicit 0.00** — grants
  and gifts really do carry a zero. Re-parsing all of them would turn correct
  values into unknowns, so the all-rows scope is **RULED OUT PERMANENTLY**. The
  remaining 548-accession (non-code-P) cleanup is a separate future WO, queued
  behind CIK-recovery. This tool refuses to touch anything but code-P by
  construction, and a test pins that.

SAFETY
  - Read-only by default. Writes ONLY with --apply.
  - --apply refuses to run without a rollback artifact, written first (A14).
  - REFUSES AMBIGUITY. `(accession, date, shares)` is not a unique key: an IGIC
    filing carries two transactions with the same date and share count, one at
    25.3133 and one at 0. A first-match rule mis-attributed them during the
    census and produced a false finding. Where more than one source transaction
    still matches after date, shares, code and acquired/disposed, the row is
    SKIPPED and reported — never guessed.
  - NEVER widens a correct value into an unknown. An explicit source `0` is a
    KNOWN zero and is left exactly as it is.
  - total_value moves with price_per_share, because `63f7558` sets both to None
    together; nulling the price alone would leave the defect in the column the
    signal actually reads.
  - A filing that cannot be fetched or matched is left exactly as it is and
    reported. Nothing is invented to fill a gap.

NOTE ON THE FETCHER
  This module does NOT import `edgar_fetcher`, because that imports `config`,
  which is gitignored and absent on the Studio — where the dry run has to
  happen. It carries a minimal read-only SEC client instead, with a declared
  User-Agent and the same rate limit. Inject your own with --fetcher-free code
  paths in tests.

USAGE
    python3 backfill_price_per_share.py --db PATH            # dry run
    python3 backfill_price_per_share.py --db PATH --apply    # write
"""
import argparse
import json
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# --- resolution actions
SET_UNKNOWN = "SET_UNKNOWN"     # source carries no numeric price -> NULL/NULL
SET_PRICE = "SET_PRICE"         # source carries a real price the DB lost
LEAVE = "LEAVE"                 # source carries an explicit 0 -> correct already
AMBIGUOUS = "AMBIGUOUS"         # >1 source transaction matches -> refuse
UNMATCHED = "UNMATCHED"         # no source transaction matches -> refuse

SHARE_TOL = 0.01                # the reference rounds shares to 2dp
USER_AGENT = "GMC Engine Research kph3802@gmail.com"
REQUEST_DELAY = 0.13            # <= 10 req/s, SEC's stated ceiling

CANDIDATE_SIGNATURE = """
    transaction_code = 'P'
AND price_per_share IS NOT NULL
AND price_per_share = 0.0
"""


@dataclass(frozen=True)
class Resolution:
    action: str
    price: float | None
    total_value: float | None
    reason: str


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #
def find_code_p_zero_rows(con) -> list:
    """The RULED candidate set: code-P rows booked at exactly 0.0.

    Rows already NULL are not candidates, so a second run after an apply finds
    nothing to do. Non-code-P rows are excluded here rather than downstream,
    so the ruled scope is a property of the query and not of a caller's
    discipline.
    """
    cur = con.execute(f"""
        SELECT id, accession_number, issuer_cik, issuer_ticker, transaction_date,
               transaction_code, shares_amount, price_per_share, total_value,
               acquired_disposed
        FROM form4_transactions
        WHERE {CANDIDATE_SIGNATURE}
        ORDER BY accession_number, id
    """)
    cols = ("id", "accession_number", "issuer_cik", "issuer_ticker",
            "transaction_date", "transaction_code", "shares_amount",
            "price_per_share", "total_value", "acquired_disposed")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# source filing
# --------------------------------------------------------------------------- #
def _child_value(el):
    for c in el.iter():
        if c.tag.endswith("value"):
            return (c.text or "").strip()
    return None


def source_transactions(xml) -> list:
    """Every nonDerivativeTransaction, with its price STATE made explicit.

    The state matters more than the number: a `<transactionPricePerShare>`
    wrapper with no `<value>` child means UNKNOWN, while an explicit `0` means a
    known zero. Collapsing the two is the defect this tool exists to repair, and
    reading the wrapper's presence as "priced" is the mistake the census made on
    its first pass.
    """
    root = ET.fromstring(xml) if isinstance(xml, (str, bytes)) else xml
    out = []
    for t in root.iter():
        if not t.tag.endswith("nonDerivativeTransaction"):
            continue
        date = shares = ad = None
        price_state, price = "NO_ELEMENT", None
        code = None
        for e in t.iter():
            tag = e.tag.rsplit("}", 1)[-1]
            if tag == "transactionDate":
                date = _child_value(e)
            elif tag == "transactionShares":
                shares = _child_value(e)
            elif tag == "transactionAcquiredDisposedCode":
                ad = _child_value(e)
            elif tag == "transactionCode":
                code = (e.text or "").strip()
            elif tag == "transactionPricePerShare":
                v = _child_value(e)
                if v in (None, ""):
                    price_state, price = "ELEM_NO_VALUE", None
                else:
                    try:
                        price, price_state = float(v), "VALUE"
                    except ValueError:
                        price, price_state = None, "ELEM_NO_VALUE"
        try:
            shares_f = float(shares) if shares else None
        except ValueError:
            shares_f = None
        out.append({"date": (date or "")[:10], "shares": shares_f,
                    "code": (code or "").strip().upper() or None,
                    "acq_disp": (ad or "").strip().upper() or None,
                    "price_state": price_state, "price": price})
    return out


def _candidates_for(row, txns):
    """Source transactions matching this DB row, narrowed progressively.

    Date + shares first (what the census used), then transaction code, then
    acquired/disposed. Narrowing only ever runs when it leaves at least one
    match, so a filing that omits a field cannot narrow a real match to zero.
    """
    d = (row.get("transaction_date") or "")[:10]
    sh = row.get("shares_amount") or 0.0
    m = [t for t in txns
         if t["date"] == d and t["shares"] is not None
         and abs(t["shares"] - sh) <= SHARE_TOL]
    for key, want in (("code", (row.get("transaction_code") or "").strip().upper()),
                      ("acq_disp", (row.get("acquired_disposed") or "").strip().upper())):
        if len(m) <= 1 or not want:
            break
        narrowed = [t for t in m if (t.get(key) or "") == want]
        if narrowed:
            m = narrowed
    return m


def resolve_row(row, txns) -> Resolution:
    """Decide what this row should become, or refuse to decide."""
    m = _candidates_for(row, txns)
    if not m:
        return Resolution(UNMATCHED, None, None,
                          "no source transaction matches date+shares")
    if len(m) > 1:
        return Resolution(AMBIGUOUS, None, None,
                          f"{len(m)} source transactions match on date+shares"
                          f"+code+acquired/disposed — refusing to guess")
    t = m[0]
    if t["price_state"] in ("NO_ELEMENT", "ELEM_NO_VALUE"):
        return Resolution(SET_UNKNOWN, None, None,
                          f"source price {t['price_state']} -> unknown")
    if t["price"] == 0:
        return Resolution(LEAVE, None, None,
                          "source carries an explicit 0 — a KNOWN zero")
    shares = row.get("shares_amount") or 0.0
    return Resolution(SET_PRICE, t["price"], t["price"] * shares,
                      f"source carries {t['price']} — the stored 0.0 lost it")


# --------------------------------------------------------------------------- #
# A14 rollback
# --------------------------------------------------------------------------- #
def write_rollback(db_path: Path, rows, out_dir: Path) -> Path:
    """A14: create the rollback artifact BEFORE any write.

    Both a full copy of the database and a row-level record of the exact prior
    values, so the change is reversible without the copy if need be.
    """
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(db_path, out_dir / f"{db_path.name}.pre-price-backfill-{stamp}.bak")

    prior = [{"id": r["id"], "accession_number": r["accession_number"],
              "transaction_code": r["transaction_code"],
              "transaction_date": r["transaction_date"],
              "shares_amount": r["shares_amount"],
              "price_per_share": r["price_per_share"],
              "total_value": r["total_value"]} for r in rows]

    record = out_dir / f"backfill_price_per_share_rollback_{stamp}.json"
    record.write_text(json.dumps({
        "created_utc": stamp,
        "db": str(db_path),
        "backup": f"{db_path.name}.pre-price-backfill-{stamp}.bak",
        "reason": "Defect-A price backfill: a missing price is unknown, not zero",
        "scope": "code-P only (PM ruling R12, 2026-08-06)",
        "n_rows": len(prior),
        "n_accessions": len({p["accession_number"] for p in prior}),
        "prior_values": prior,
    }, indent=2) + "\n")
    return record


def apply_resolutions(db_path: Path, resolutions, *, rollback_record) -> int:
    """Write the resolved rows. Refuses without a rollback artifact (A14).

    Only SET_UNKNOWN and SET_PRICE are written. LEAVE, AMBIGUOUS and UNMATCHED
    are deliberately no-ops: the first is already correct, and the other two are
    rows the tool declined to decide — writing a guess for them is exactly the
    failure this design exists to prevent.
    """
    if not rollback_record or not Path(rollback_record).exists():
        raise RuntimeError(
            "refusing to write: the A14 rollback artifact must exist BEFORE any "
            "row is touched")
    con = sqlite3.connect(str(db_path))
    try:
        n = 0
        for row, res in resolutions:
            if res.action not in (SET_UNKNOWN, SET_PRICE):
                continue
            cur = con.execute(
                "UPDATE form4_transactions SET price_per_share = ?, "
                "total_value = ? WHERE id = ?",
                (res.price, res.total_value, row["id"]))
            n += cur.rowcount
        con.commit()
    finally:
        con.close()
    return n


# --------------------------------------------------------------------------- #
# minimal read-only SEC client (no `config` dependency — see module docstring)
# --------------------------------------------------------------------------- #
class SecFetcher:
    def __init__(self, delay=REQUEST_DELAY, user_agent=USER_AGENT):
        self.delay, self.ua, self._last = delay, user_agent, 0.0

    def _get(self, url):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def filing_xml(self, cik, accession):
        """The filing's ownership XML, resolved through its own index.

        The document name varies (`primary_doc.xml`, `form4.xml`,
        `ownership.xml`, agent-specific names), so the index is authoritative
        and guessing a filename 404s on a large share of filings.
        """
        acc = str(accession).replace("-", "")
        idx = json.loads(self._get(
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"))
        names = [i.get("name") for i in idx["directory"]["item"]]
        xmls = [n for n in names
                if n and n.lower().endswith(".xml") and "index" not in n.lower()]
        pref = [n for n in xmls if "primary_doc" in n.lower()] or xmls
        if not pref:
            raise RuntimeError("no XML document in the filing index")
        return self._get(
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{pref[0]}")


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--apply", action="store_true",
                    help="write the backfilled values (default: dry run)")
    ap.add_argument("--rollback-dir", type=Path, default=None,
                    help="where the A14 rollback artifact goes "
                         "(default: alongside the DB)")
    a = ap.parse_args(argv)

    if not a.db.exists():
        print(f"FAIL — database absent: {a.db}")
        return 2

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        rows = find_code_p_zero_rows(con)
    finally:
        con.close()

    n_acc = len({r["accession_number"] for r in rows})
    print(f"Code-P rows booked at 0.0: {len(rows)} row(s) across {n_acc} "
          f"accession(s) in {a.db}")
    print("Scope: code-P only (PM ruling R12, 2026-08-06). Non-code-P zero rows "
          "are NOT candidates.")
    if not rows:
        print("Nothing to backfill.")
        return 0

    record = None
    if a.apply:
        record = write_rollback(a.db, rows, a.rollback_dir or a.db.parent / "rollback")
        print(f"Rollback artifact written first (A14): {record}")

    fetcher = SecFetcher()
    resolutions, by_action = [], {}
    xml_cache = {}
    print(f"\n  {'accession':<22}{'tkr':<7}{'date':<12}{'shares':>11}"
          f"  {'before':>10} -> {'after':<12} action")
    for r in rows:
        acc = r["accession_number"]
        if acc not in xml_cache:
            try:
                xml_cache[acc] = fetcher.filing_xml(r["issuer_cik"], acc)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                    RuntimeError, ValueError) as e:
                xml_cache[acc] = None
                print(f"  {acc:<22}{str(r['issuer_ticker'])[:6]:<7}"
                      f"{'':<12}{'':>11}  FETCH FAILED: {type(e).__name__}")
        xml = xml_cache[acc]
        if xml is None:
            res = Resolution(UNMATCHED, None, None, "filing could not be fetched")
        else:
            try:
                res = resolve_row(r, source_transactions(xml))
            except ET.ParseError:
                res = Resolution(UNMATCHED, None, None, "filing XML unparseable")
        resolutions.append((r, res))
        by_action[res.action] = by_action.get(res.action, 0) + 1
        after = ("NULL" if res.action == SET_UNKNOWN
                 else f"{res.price}" if res.action == SET_PRICE
                 else "(unchanged)")
        print(f"  {acc:<22}{str(r['issuer_ticker'])[:6]:<7}"
              f"{str(r['transaction_date'])[:10]:<12}{r['shares_amount']:>11,.0f}"
              f"  {r['price_per_share']:>10} -> {after:<12} {res.action}"
              f"  ({res.reason})")

    writable = sum(1 for _, x in resolutions if x.action in (SET_UNKNOWN, SET_PRICE))
    print(f"\n  summary: {by_action}")
    print(f"  writable rows: {writable} of {len(rows)}")

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    n = apply_resolutions(a.db, resolutions, rollback_record=record)
    print(f"\nAPPLIED — {n} row(s) updated.")
    refused = [(r["accession_number"], x.action) for r, x in resolutions
               if x.action in (AMBIGUOUS, UNMATCHED)]
    if refused:
        print(f"REFUSED — {len(refused)} row(s) left exactly as they are, not "
              f"guessed at: {refused}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
