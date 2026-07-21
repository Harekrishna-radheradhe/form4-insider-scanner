"""
Test bootstrap for the Form-4 insider scanner.

The real `config.py` is gitignored/absent (it holds live credentials). Several
modules import it at top level, which would make them unimportable under test.
We inject a stub `config` module into sys.modules with PLACEHOLDER values only —
no real secrets (constitution A13). Tests that need a database point
DATABASE_CONFIG['db_path'] at a per-test temp file via monkeypatch.
"""
import sys
import types

if 'config' not in sys.modules:
    cfg = types.ModuleType('config')
    cfg.EMAIL_CONFIG = {
        'smtp_server': 'smtp.example.com',
        'smtp_port': 587,
        'sender_email': 'placeholder@example.com',
        'sender_password': 'PLACEHOLDER_NOT_A_REAL_SECRET',
        'recipient_email': 'placeholder@example.com',
    }
    cfg.EDGAR_CONFIG = {
        'user_agent': 'GMC Research placeholder@example.com',
        'request_delay': 0.0,
        'form4_rss_url': 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&output=atom',
        'edgar_base_url': 'https://www.sec.gov/Archives/edgar/data/',
    }
    cfg.ALERT_THRESHOLDS = {
        'min_cluster_size': 2,
        'cluster_window_days': 14,
        'min_purchase_value': 10000,
        'transaction_types': ['P'],
    }
    cfg.DATABASE_CONFIG = {
        'db_path': ':memory:',  # overridden per-test via monkeypatch
    }
    cfg.OPERATIONAL_CONFIG = {
        'lookback_days': 3,
        'max_filings_per_run': 500,
    }
    sys.modules['config'] = cfg
