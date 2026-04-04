"""
run_imports.py  --  personal import entrypoint (user-side, not part of drnukebean)

Usage:
    python run_imports.py                          # run all importers, last month
    python run_imports.py zkb                      # ZKB only
    python run_imports.py pfg neon                 # PFG and Neon
    python run_imports.py --dry-run                # dry run, all importers -> temp file
    python run_imports.py --dry-run zkb            # dry run, ZKB only
    python run_imports.py --from 2026-01           # January 2026 only
    python run_imports.py --from 2026-01 --to 2026-02   # Jan + Feb 2026
    python run_imports.py zkb --from 2026-01       # ZKB only, January

Sensitive values (paths, IBANs, API credentials) live in local_config.py.
See local_config.py.example for the expected structure.
"""

from datetime import datetime
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Logging — file sink (DEBUG and above → ./logs/ingest_<timestamp>.log)
# stderr sink (INFO and above) is kept from loguru's default.
# ---------------------------------------------------------------------------

_log_dir = Path.cwd() / "logs"
_log_dir.mkdir(exist_ok=True)
logger.add(
    _log_dir / f"ingest_{datetime.now():%Y%m%d_%H%M%S}.log",
    level="DEBUG",
    encoding="utf-8",
)

import local_config as cfg

from drnukebean.pipeline.runner import run_all

# Importers already migrated to beangulp
from drnukebean.importer.zkb_camt import ZKBCamtImporter
from drnukebean.importer.zkb_ebics import make_zkb_setup
from drnukebean.importer.ibkr import IBKRImporter
from drnukebean.importer.ibkr_flexquery import make_ibkr_setup

# Neon and Revolut: adopted from tariochbctools as-is (already beangulp-native)
from tariochbctools.importers.neon.importer import Importer as NeonImporter
from tariochbctools.importers.revolut.importer import Importer as RevolutImporter

# User-maintained fix functions (confidential, not in repo)
from transaction_fixes import (
    fixes_zkb,
    fixes_pfg,
    fixes_neon,
    fixes_revolut,
    fixes_ibkr,
    fixes_finpension,
    fixes_halbtax,
)

# ---------------------------------------------------------------------------
# Importer instances
# ---------------------------------------------------------------------------

_zkb = ZKBCamtImporter(
    iban=cfg.ZKB_IBAN,
    account='Assets:Bank:ZKB:CHF',
    balance_account='Assets:Taxable:ZKB:PF:CHF',
    currency='CHF',
)

_neon = NeonImporter(
    filepattern=r'neon_.*\.csv',
    account='Assets:Bank:Neon:CHF',
)

_revolut = RevolutImporter(
    filepattern=r'revolut_.*\.csv',
    account='Assets:Bank:Revolut:CHF',
    currency='CHF',
)

_ibkr = IBKRImporter(
    account='Assets:Invest:IBKR',
    currency='CHF',
    wht_account='Expenses:Invest:IBKR:WHT',
    deposit_account='',   # set to e.g. 'Assets:Bank:ZKB:CHF' to capture deposits
)

# ---------------------------------------------------------------------------
# Derived paths
# ---------------------------------------------------------------------------

_LEDGER = cfg.LEDGER_DIR / 'main.bean'

# ---------------------------------------------------------------------------
# Smart importer training window
# ---------------------------------------------------------------------------

# Number of days of ledger history fed to smart_importer for account prediction.
# Reducing this speeds up loading and keeps predictions focused on recent patterns.
# Set to None to load the full ledger (no limit).
PREDICT_LOOKBACK_DAYS = 730  # two years

# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------

pipelines = [
    {
        'name':        'zkb',
        'importer':    _zkb,
        'source_dir':  cfg.DOWNLOADS / 'zkb',
        'output_file': cfg.LEDGER_DIR / 'ZKB.bean',
        'setup':       make_zkb_setup(cfg.ZKB_CREDENTIALS, cfg.DOWNLOADS / 'zkb'),
        'fixes':       fixes_zkb,
        'predict':     True,
    },
    {
        'name':        'neon',
        'importer':    _neon,
        'source_dir':  cfg.DOWNLOADS / 'neon',
        'output_file': cfg.LEDGER_DIR / 'Neon.bean',
        'fixes':       fixes_neon,
        'predict':     True,
    },
    {
        'name':        'revolut',
        'importer':    _revolut,
        'source_dir':  cfg.DOWNLOADS / 'revolut',
        'output_file': cfg.LEDGER_DIR / 'Revolut.bean',
        'fixes':       fixes_revolut,
        'predict':     True,
    },
    {
        'name':        'ibkr',
        'importer':    _ibkr,
        'source_dir':  cfg.DOWNLOADS / 'ibkr',
        'output_file': cfg.LEDGER_DIR / 'IBKR.bean',
        'setup':       make_ibkr_setup(cfg.IBKR_TOKEN, cfg.IBKR_QUERY_ID, cfg.IBKR_QUERY_NAME, cfg.DOWNLOADS / 'ibkr'),
        'fixes':       fixes_ibkr,
        'predict':     False,   # all postings are fully populated by the importer
    },
]

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    run_all(
        pipelines,
        ledger=_LEDGER,
        statement_dest=cfg.STATEMENT_DEST,
        dry_run_file=cfg.LEDGER_DIR / 'testoutput.bean',
        predict_lookback_days=PREDICT_LOOKBACK_DAYS,
    )
