"""
Form 4 XML Parser
"""
import xml.etree.ElementTree as ET


class Form4Parser:
    TRANSACTION_CODES = {
        'P': 'Open market purchase', 'S': 'Open market sale', 'A': 'Grant/Award',
        'M': 'Exercise of derivative', 'G': 'Gift', 'F': 'Payment of exercise price',
    }

    def parse(self, xml_content, accession_number=None, filing_date=None,
              accepted_datetime=None):
        """Parse a Form 4 XML document into transaction dicts.

        `accepted_datetime` is REQUIRED: it is SEC's EDGAR acceptance datetime
        for this accession, which pre-registration §0.4 makes the
        signal-eligible time. It is the caller's job to fetch it (see
        EdgarFetcher.fetch_acceptance_datetime). This used to default to
        datetime.now() — the wall-clock moment the parser happened to run —
        which is a lookahead surface wearing the acceptance field's name, so
        there is deliberately no fallback here.
        """
        if not accepted_datetime:
            raise ValueError(
                "accepted_datetime is required — pass SEC's EDGAR acceptance "
                "datetime for this accession (EdgarFetcher.fetch_acceptance_"
                "datetime). Stamping the parser's own clock is lookahead.")
        if not xml_content:
            return []
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            print(f"XML parse error: {e}")
            return []
        transactions = []
        issuer_info = self._extract_issuer_info(root)
        owner_info = self._extract_owner_info(root)
        non_derivative_txns = self._extract_non_derivative_transactions(root)
        for txn in non_derivative_txns:
            transaction = {
                'accession_number': accession_number,
                'filing_date': filing_date,
                'accepted_datetime': accepted_datetime,
                **issuer_info, **owner_info, **txn
            }
            transactions.append(transaction)
        return transactions
    
    def _extract_issuer_info(self, root):
        issuer = root.find('.//issuer')
        if issuer is None:
            return {'issuer_cik': None, 'issuer_name': None, 'issuer_ticker': None}
        cik = issuer.find('issuerCik')
        name = issuer.find('issuerName')
        ticker = issuer.find('issuerTradingSymbol')
        return {
            'issuer_cik': cik.text.strip() if cik is not None and cik.text else None,
            'issuer_name': name.text.strip() if name is not None and name.text else None,
            'issuer_ticker': ticker.text.strip().upper() if ticker is not None and ticker.text else None
        }
    
    def _extract_owner_info(self, root):
        owner = root.find('.//reportingOwner')
        if owner is None:
            return {'insider_cik': None, 'insider_name': None, 'insider_title': None,
                    'is_director': 0, 'is_officer': 0, 'is_ten_percent_owner': 0}
        owner_id = owner.find('reportingOwnerId')
        cik = owner_id.find('rptOwnerCik') if owner_id is not None else None
        name = owner_id.find('rptOwnerName') if owner_id is not None else None
        relationship = owner.find('reportingOwnerRelationship')
        is_director = relationship.find('isDirector') if relationship is not None else None
        is_officer = relationship.find('isOfficer') if relationship is not None else None
        is_ten_pct = relationship.find('isTenPercentOwner') if relationship is not None else None
        officer_title = relationship.find('officerTitle') if relationship is not None else None
        def bool_to_int(element):
            if element is None or element.text is None:
                return 0
            return 1 if element.text.strip().lower() in ('1', 'true', 'yes') else 0
        return {
            'insider_cik': cik.text.strip() if cik is not None and cik.text else None,
            'insider_name': name.text.strip() if name is not None and name.text else None,
            'insider_title': officer_title.text.strip() if officer_title is not None and officer_title.text else None,
            'is_director': bool_to_int(is_director),
            'is_officer': bool_to_int(is_officer),
            'is_ten_percent_owner': bool_to_int(is_ten_pct)
        }
    
    def _extract_non_derivative_transactions(self, root):
        transactions = []
        table = root.find('.//nonDerivativeTable')
        if table is None:
            return transactions
        for txn in table.findall('nonDerivativeTransaction'):
            transaction = self._parse_non_derivative_transaction(txn)
            if transaction:
                transactions.append(transaction)
        return transactions
    
    def _parse_non_derivative_transaction(self, txn):
        try:
            security = txn.find('securityTitle/value')
            txn_date = txn.find('transactionDate/value')
            coding = txn.find('transactionCoding')
            txn_code = coding.find('transactionCode') if coding is not None else None
            amounts = txn.find('transactionAmounts')
            shares = amounts.find('transactionShares/value') if amounts is not None else None
            price = amounts.find('transactionPricePerShare/value') if amounts is not None else None
            acq_disp = amounts.find('transactionAcquiredDisposedCode/value') if amounts is not None else None
            holdings = txn.find('postTransactionAmounts')
            shares_after = holdings.find('sharesOwnedFollowingTransaction/value') if holdings is not None else None
            shares_amount = float(shares.text) if shares is not None and shares.text else 0
            # A filing that carries no <transactionPricePerShare> states an
            # UNKNOWN price, not a zero one. Writing 0.0 here made the two
            # indistinguishable, and "the insider transacted at zero dollars"
            # never happens. None/NULL keeps the distinction and forces every
            # downstream consumer to make the decision explicitly.
            # An explicit 0.00 (grants/awards carry one) is a KNOWN zero and is
            # left alone.
            price_per_share = float(price.text) if price is not None and price.text else None
            return {
                'transaction_date': txn_date.text.strip() if txn_date is not None and txn_date.text else None,
                'transaction_code': txn_code.text.strip() if txn_code is not None and txn_code.text else None,
                'shares_amount': shares_amount,
                'price_per_share': price_per_share,
                'total_value': (shares_amount * price_per_share
                                if price_per_share is not None else None),
                'acquired_disposed': acq_disp.text.strip() if acq_disp is not None and acq_disp.text else None,
                'shares_owned_after': float(shares_after.text) if shares_after is not None and shares_after.text else None,
            }
        except Exception as e:
            print(f"Error parsing transaction: {e}")
            return None


# Convenience function for imports
def parse_form4_filing(xml_content, accession_number=None, filing_date=None,
                       accepted_datetime=None):
    """Wrapper function to match main.py import expectations."""
    parser = Form4Parser()
    return parser.parse(xml_content, accession_number, filing_date,
                        accepted_datetime)
