# SEC Insider Buying Tracker

An AI-assisted adaptation of an open-source SEC Form 4 scanner, maintained in Nikhil Korvi's GitHub repository.

**[Open the live report](LIVE_REPORT.md)** · **[View daily runs](https://github.com/Harekrishna-radheradhe/form4-insider-scanner/actions/workflows/live.yml)**

The tracker finds companies where at least three distinct officers or directors
reported positive-price common-share purchases within seven calendar dates.
Each candidate includes buyer names, roles, purchase dates, reported dollar
values and links to the original SEC filings.

## What this version adds

- Complete daily SEC index collection, including Form 4 amendments.
- A seven-day filter that counts distinct reporting-owner CIKs.
- Exclusion of placeholder tickers, joint-owner filings and detected private deals.
- Issuer identity normalization and conservative handling of amendments.
- Disclosure-date timing corrections and next-session opening entries in the historical engine.
- Cached filing collection, a shared request limiter and observed federal holiday handling.
- Daily Markdown, HTML and JSON reports with explicit collection status.
- 164 automated tests covering the original scanner and the added workflow.

## Use the report

1. Open the live report linked above.
2. Check the latest filing day and collection status.
3. Review the company, buyers and reported amounts.
4. Follow each SEC filing link to inspect the transaction and footnotes.

The report updates at 13:17 UTC, around 5:17 AM Pacific standard time or 6:17 AM
Pacific daylight time. GitHub may delay scheduled runs. Weekends and observed
federal holidays retain the latest completed filing day.

For a manual refresh, open Actions, choose **Daily live insider report**, then
select **Run workflow**. The separate **Research checks and report** workflow
produces a clearly labeled fictional demo for software verification.

## Data workflow

| Stage | Implementation |
| --- | --- |
| Discover | Completed EDGAR daily indexes |
| Retrieve | Ownership XML and acceptance timestamp from the complete SEC submission |
| Store | Atomic filing snapshots in SQLite, cached between hosted runs |
| Filter | Three distinct officers or directors within seven calendar dates |
| Publish | Readable report in this repository, with HTML and JSON downloads |

## Run locally

Use Python 3.12. Copy the sample configuration and set `SEC_USER_AGENT` in your
environment to your name and contact email.

```bash
python -m pip install -r requirements-research.txt
cp config_example.py config.py
python -m pytest -q
python research_scan.py
```

Open `research_results/report.html` for the generated report. For a specific
completed range, use `--start YYYY-MM-DD --end YYYY-MM-DD`. Collect at least seven
calendar dates. Use `--demo` for the fictional test report.

GitHub Actions uses the optional `SEC_USER_AGENT` repository secret or a human
contact email already public in Git commit history. The header is masked in logs.
Credentials belong in the environment and must stay out of Git commits.

## Interpretation and limitations

Code P can include private purchases. Footnote keywords cannot classify every
deal. Joint-owner filings and issuers with collected amendments are excluded
pending manual review, which can omit valid purchases. The dollar values describe
reported insider transactions.

Entry and exit profitability remains unverified. The historical engine still
needs full amendment and footnote reconciliation, trading costs and a
delisting-complete price source. Earlier upstream return figures require
recomputation after the timing corrections. The original `main.py` is retained
as a legacy entry point. Use `research_scan.py` for this version's reports.

## Attribution

This project adapts **KPH3802/form4-insider-scanner**. The original scanner,
parser and related components remain part of the codebase. This fork adds the
research reporting workflow and the corrections described above.

The upstream README states MIT, and a standalone license file was absent at the
reviewed upstream commit. Licensing clarification remains necessary before a
separate product release.

[Original project](https://github.com/KPH3802/form4-insider-scanner) · [Preserved original documentation](UPSTREAM_README.md)
