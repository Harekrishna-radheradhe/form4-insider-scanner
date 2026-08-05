"""
SEC EDGAR Form 4 Filing Fetcher
"""
import re
import requests
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import EDGAR_CONFIG

# EDGAR stamps acceptance times in US Eastern with no offset in the field, so
# the zone has to be supplied here to make the stored value unambiguous.
_EDGAR_TZ = ZoneInfo('America/New_York')
_ACCEPTANCE_RE = re.compile(r'<ACCEPTANCE-DATETIME>\s*(\d{14})')

class EdgarFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': EDGAR_CONFIG['user_agent'],
            'Accept-Encoding': 'gzip, deflate',
        })
        self.last_request_time = 0
    
    def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < EDGAR_CONFIG['request_delay']:
            time.sleep(EDGAR_CONFIG['request_delay'] - elapsed)
        self.last_request_time = time.time()
    
    def _make_request(self, url):
        self._rate_limit()
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            print(f"Request error for {url}: {e}")
            return None
    
    def get_recent_form4_filings(self, count=100):
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=only&count={count}&output=atom"
        response = self._make_request(url)
        if not response:
            return []
        filings = []
        try:
            root = ET.fromstring(response.content)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            for entry in root.findall('atom:entry', ns):
                filing = self._parse_feed_entry(entry, ns)
                if filing:
                    filings.append(filing)
        except ET.ParseError as e:
            print(f"XML parse error: {e}")
        return filings
    
    def _parse_feed_entry(self, entry, ns):
        try:
            title = entry.find('atom:title', ns)
            link = entry.find('atom:link', ns)
            updated = entry.find('atom:updated', ns)
            if title is None or link is None:
                return None
            title_text = title.text or ''
            link_href = link.get('href', '')
            parts = link_href.split('/')
            cik = None
            accession = None
            for i, part in enumerate(parts):
                if part == 'data' and i + 2 < len(parts):
                    cik = parts[i + 1]
                    accession = parts[i + 2]
                    break
            company_name = ''
            if ' - ' in title_text:
                company_name = title_text.split(' - ', 1)[1]
                if '(' in company_name:
                    company_name = company_name.split('(')[0].strip()
            return {
                'title': title_text,
                'link': link_href,
                'cik': cik,
                'accession_number': accession,
                'company_name': company_name,
                'updated': updated.text if updated is not None else None,
            }
        except Exception as e:
            print(f"Error parsing feed entry: {e}")
            return None
    
    def get_filing_index(self, cik, accession_number):
        accession_clean = accession_number.replace('-', '')
        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/index.json"
        response = self._make_request(index_url)
        if not response:
            return None
        try:
            data = response.json()
            for item in data.get('directory', {}).get('item', []):
                name = item.get('name', '').lower()
                if name.endswith('.xml') and 'form4' not in name:
                    continue
                if name.endswith('.xml'):
                    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/{item.get('name')}"
            for item in data.get('directory', {}).get('item', []):
                name = item.get('name', '').lower()
                if name.endswith('.xml') and 'index' not in name:
                    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/{item.get('name')}"
        except Exception as e:
            print(f"Error parsing filing index: {e}")
        return None
    
    def fetch_acceptance_datetime(self, cik, accession_number):
        """SEC's TRUE acceptance datetime for an accession, as ISO 8601.

        Source: the <ACCEPTANCE-DATETIME> field in the accession's full SGML
        submission header. Neither index.json nor the primary XML document
        carries it, so this is the only place the value exists.

        Verified against two real filings on 2026-08-05 rather than assumed
        from the field's description:
            0001104659-26-019194 -> 20260224165940 (FILED AS OF DATE 20260224)
            0001213900-26-016727 -> 20260213214702 (FILED AS OF DATE 20260213)
        The field is 14 digits, YYYYMMDDHHMMSS, US/Eastern, and carries no
        offset of its own — hence the explicit zone below. The second example
        also confirms the late-evening case: Section 16 filings accepted before
        22:00 ET still book the same filing date.

        `cik` must be a CIK associated with the filing (issuer or reporting
        owner). The accession-number prefix is the filing AGENT's CIK and the
        Archives path 404s under it.

        Returns None if the fetch fails or the field is absent/malformed —
        never a guess. The caller decides what to do with a missing value;
        parse() will refuse to stamp one.
        """
        accession_dashed = self._format_accession(accession_number)
        accession_clean = accession_dashed.replace('-', '')
        cik_clean = str(cik).lstrip('0') or '0'
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/"
               f"{accession_clean}/{accession_dashed}.txt")
        response = self._make_request(url)
        if not response:
            return None
        match = _ACCEPTANCE_RE.search(response.text)
        if not match:
            print(f"No <ACCEPTANCE-DATETIME> in submission header: {url}")
            return None
        try:
            naive = datetime.strptime(match.group(1), '%Y%m%d%H%M%S')
        except ValueError as e:
            print(f"Malformed acceptance datetime for {accession_dashed}: {e}")
            return None
        return naive.replace(tzinfo=_EDGAR_TZ).isoformat()

    @staticmethod
    def _format_accession(accession_number):
        """Normalise an accession to EDGAR's dashed form (0001104659-26-019194).

        The atom feed hands us the undashed form; the DB carries both.
        """
        digits = str(accession_number).replace('-', '')
        if len(digits) == 18:
            return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"
        return str(accession_number)

    def fetch_form4_xml(self, cik, accession_number):
        xml_url = self.get_filing_index(cik, accession_number)
        if not xml_url:
            accession_clean = accession_number.replace('-', '')
            xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/primary_doc.xml"
        response = self._make_request(xml_url)
        if response:
            return response.text
        return None


# Convenience function for imports
def fetch_recent_filings(count=100):
    """Wrapper function to match main.py import expectations."""
    fetcher = EdgarFetcher()
    return fetcher.get_recent_form4_filings(count=count)


if __name__ == '__main__':
    fetcher = EdgarFetcher()
    print("Fetching recent Form 4 filings...")
    filings = fetcher.get_recent_form4_filings(count=10)
    print(f"Found {len(filings)} filings")
