"""
Defect B — accepted_datetime is SEC's acceptance time, not our wall clock.

`Form4Parser.parse()` stamped every row with `datetime.now().isoformat()` — the
moment the parser happened to run. Pre-registration §0.4 makes the EDGAR
acceptance datetime the signal-eligible time, so a parser run at T claiming a
filing was "accepted" at T is lookahead wearing the acceptance field's name.
22 of 194 census rows carry a stamp AFTER their own filing date, which is only
possible because the value was never SEC's.

Fix: parse() takes accepted_datetime as a required parameter and raises rather
than inventing one. The caller fetches the truth from the accession's SGML
submission header.

The header format below was VERIFIED against two real filings on 2026-08-05,
not assumed from a description:
  0001104659-26-019194 -> <ACCEPTANCE-DATETIME>20260224165940
  0001213900-26-016727 -> <ACCEPTANCE-DATETIME>20260213214702
14 digits, YYYYMMDDHHMMSS, US/Eastern, no timezone marker in the field itself.
The URL in the spec (.../data/{cik}/{accession}.txt) 404s unless {cik} is a CIK
actually associated with the filing — the accession-number prefix is the filing
AGENT's CIK and is not. Both verified filings resolve under the ISSUER cik.
"""
import pytest

import edgar_fetcher
from edgar_fetcher import EdgarFetcher
from form4_parser import Form4Parser


MINIMAL_FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer>
    <issuerCik>0002101562</issuerCik>
    <issuerName>Acceptance Test Corp</issuerName>
    <issuerTradingSymbol>ACPT</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0002105547</rptOwnerCik>
      <rptOwnerName>DOE JANE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector><isOfficer>0</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-02-20</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>10.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

# Verbatim head of the real 0001104659-26-019194 submission header.
REAL_HEADER = """<SEC-DOCUMENT>0001104659-26-019194.txt : 20260224
<SEC-HEADER>0001104659-26-019194.hdr.sgml : 20260224
<ACCEPTANCE-DATETIME>20260224165940
ACCESSION NUMBER:\t\t0001104659-26-019194
CONFORMED SUBMISSION TYPE:\t4
PUBLIC DOCUMENT COUNT:\t\t2
CONFORMED PERIOD OF REPORT:\t20260220
FILED AS OF DATE:\t\t20260224
DATE AS OF CHANGE:\t\t20260224
"""


# ── parse() refuses to invent the value ─────────────────────────

def test_parse_requires_accepted_datetime():
    """No silent fallback to datetime.now() — fail loud."""
    with pytest.raises(ValueError):
        Form4Parser().parse(MINIMAL_FORM4, accession_number='ACC-1',
                            filing_date='2026-02-24')


def test_parse_rejects_empty_accepted_datetime():
    """An empty string is a missing value wearing a present value's clothes."""
    with pytest.raises(ValueError):
        Form4Parser().parse(MINIMAL_FORM4, accession_number='ACC-1',
                            filing_date='2026-02-24', accepted_datetime='')


def test_parse_threads_the_supplied_value_through_unchanged():
    txns = Form4Parser().parse(MINIMAL_FORM4, accession_number='ACC-1',
                               filing_date='2026-02-24',
                               accepted_datetime='2026-02-24T16:59:40-05:00')
    assert txns[0]['accepted_datetime'] == '2026-02-24T16:59:40-05:00'


def test_parser_module_holds_no_clock():
    """
    The regression that defines this defect. The parser has no legitimate use
    for the current time; leaving a clock in scope is what let the stamp be
    invented in the first place, so the module must not carry one at all.
    """
    import form4_parser
    assert not hasattr(form4_parser, 'datetime'), \
        "form4_parser still imports a clock it must never consult"
    code_lines = [l for l in open(form4_parser.__file__)
                  if l.lstrip().startswith(('import ', 'from '))]
    assert not any('datetime' in l for l in code_lines), \
        f"form4_parser re-imported a clock: {code_lines}"


def test_convenience_wrapper_also_requires_it():
    with pytest.raises(ValueError):
        edgar_fetcher  # keep the import meaningful for linters
        from form4_parser import parse_form4_filing
        parse_form4_filing(MINIMAL_FORM4, accession_number='ACC-1',
                           filing_date='2026-02-24')


# ── the fetcher ─────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, text):
        self.text = text


def test_fetch_acceptance_datetime_parses_real_header(monkeypatch):
    """20260224165940 -> ISO 8601, Eastern, offset made explicit."""
    f = EdgarFetcher()
    seen = {}

    def fake_request(url):
        seen['url'] = url
        return _FakeResponse(REAL_HEADER)

    monkeypatch.setattr(f, '_make_request', fake_request)
    got = f.fetch_acceptance_datetime('0002101562', '0001104659-26-019194')

    assert got == '2026-02-24T16:59:40-05:00', got
    assert '0001104659-26-019194.txt' in seen['url']
    assert '2101562' in seen['url'], \
        "must use a CIK associated with the filing, not the accession prefix"


def test_fetch_acceptance_datetime_handles_daylight_time(monkeypatch):
    """July is EDT (-04:00). A hardcoded -05:00 would be wrong half the year."""
    f = EdgarFetcher()
    monkeypatch.setattr(f, '_make_request',
                        lambda url: _FakeResponse(
                            '<ACCEPTANCE-DATETIME>20260715183000\n'))
    assert f.fetch_acceptance_datetime('1', '0001-26-1') == \
        '2026-07-15T18:30:00-04:00'


def test_fetch_acceptance_datetime_returns_none_when_field_absent(monkeypatch):
    """Absent is None — the caller then fails loud rather than inventing one."""
    f = EdgarFetcher()
    monkeypatch.setattr(f, '_make_request',
                        lambda url: _FakeResponse('<SEC-HEADER>no such field\n'))
    assert f.fetch_acceptance_datetime('1', '0001-26-1') is None


def test_fetch_acceptance_datetime_returns_none_on_request_failure(monkeypatch):
    """_make_request returns None on HTTP error; do not crash the run."""
    f = EdgarFetcher()
    monkeypatch.setattr(f, '_make_request', lambda url: None)
    assert f.fetch_acceptance_datetime('1', '0001-26-1') is None


def test_fetch_acceptance_datetime_rejects_malformed_stamp(monkeypatch):
    """A short/garbage stamp is not silently coerced into a plausible date."""
    f = EdgarFetcher()
    monkeypatch.setattr(f, '_make_request',
                        lambda url: _FakeResponse('<ACCEPTANCE-DATETIME>2026\n'))
    assert f.fetch_acceptance_datetime('1', '0001-26-1') is None


def test_fetch_acceptance_datetime_is_rate_limited(monkeypatch):
    """Same EDGAR courtesy as its siblings — it goes through _make_request."""
    f = EdgarFetcher()
    calls = []
    monkeypatch.setattr(f, '_rate_limit', lambda: calls.append(1))
    monkeypatch.setattr(f.session, 'get',
                        lambda url, timeout=None: _StubHTTP(REAL_HEADER))
    f.fetch_acceptance_datetime('0002101562', '0001104659-26-019194')
    assert calls, "fetch bypassed the rate limiter"


class _StubHTTP:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None
