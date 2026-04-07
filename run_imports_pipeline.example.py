"""
run_imports_pipeline.py  --  personal import entrypoint (user-side, not part of drnukebean).

Usage:
    python run_imports_pipeline.py                              # all importers, last month
    python run_imports_pipeline.py zkb                          # ZKB only
    python run_imports_pipeline.py pfg neon                     # PFG and Neon
    python run_imports_pipeline.py --dry-run                    # dry run -> temp file
    python run_imports_pipeline.py --from 2026-01               # January 2026
    python run_imports_pipeline.py --from 2026-01 --to 2026-02  # Jan + Feb 2026
    python run_imports_pipeline.py zkb --from 2026-01           # ZKB, January

Sensitive values (paths, credentials) live in pipeline_secrets.py.
See pipeline_secrets.example.py for the expected structure.
"""

from datetime import datetime
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_dir = Path.cwd() / 'logs'
_log_dir.mkdir(exist_ok=True)
logger.add(
    _log_dir / f"ingest_{datetime.now():%Y%m%d_%H%M%S}.log",
    level='DEBUG',
    encoding='utf-8',
)

import pipeline_secrets as cfg

from drnukebean.pipeline.runner import run_all
from drnukebean.importer.zkb_camt import ZKBCamtImporter
from drnukebean.importer.zkb_ebics import make_zkb_setup
from drnukebean.importer.ibkr import IBKRImporter
from drnukebean.importer.ibkr_flexquery import make_ibkr_setup
from drnukebean.importer.sbb import SBBImporter
from tariochbctools.importers.neon.importer import Importer as NeonImporter
from tariochbctools.importers.revolut.importer import Importer as RevolutImporter

from transaction_fixes import (
    fixes_zkb,
    fixes_pfg,
    fixes_neon,
    fixes_revolut,
    fixes_ibkr,
    fixes_finpension,
    fixes_sbb,
)

# ---------------------------------------------------------------------------
# Beancount account names
# ---------------------------------------------------------------------------

ZKB_ACCOUNT          = 'Assets:Bank:ZKB:CHF'
NEON_ACCOUNT         = 'Assets:Bank:Neon:CHF'
REVOLUT_ACCOUNT      = 'Assets:Bank:Revolut:CHF'

SBB_ACCOUNT_EXPENSES = 'Expenses:Travel:SBB'
SBB_ACCOUNT_HALBTAX  = 'Assets:Prepaid:HalbtaxPlus'
SBB_ACCOUNT_BANK     = 'Assets:Bank:ZKB:CHF'   # counter-account for non-Halbtax-PLUS rows

IBKR_ACCOUNT_ROOT    = 'Assets:Invest:IBKR'
IBKR_WHT_ACCOUNT     = 'Expenses:Invest:IBKR:WHT'

# Map of IBKR account IDs (from pipeline_secrets.py) to beancount account config.
# Account IDs are secrets; account names live here alongside all other accounts.
# Add one entry per IBKR account; see pipeline_secrets.example.py for the ID constants.
IBKR_ACCOUNT_MAP = {
    cfg.IBKR_ACCOUNT_1: {
        'root': 'Assets:Invest:IBKR:AccountName1',
        'deposit_from': ZKB_ACCOUNT,
    },
    cfg.IBKR_ACCOUNT_2: {
        'root': 'Assets:Invest:IBKR:AccountName2',
        # no deposit_from -> deposits flagged '!' for manual annotation
    },
}

# ---------------------------------------------------------------------------
# Pipeline tuning
# ---------------------------------------------------------------------------

# Days of ledger history fed to smart_importer for account prediction.
# Reduce to speed up loading; set to None for the full ledger.
PREDICT_LOOKBACK_DAYS = 730

# ---------------------------------------------------------------------------
# Importer instances
# ---------------------------------------------------------------------------

_zkb = ZKBCamtImporter(
    iban=cfg.ZKB_IBAN,
    account=ZKB_ACCOUNT,
    currency='CHF',
)

_neon = NeonImporter(
    filepattern=r'neon_.*\.csv',
    account=NEON_ACCOUNT,
)

_revolut = RevolutImporter(
    filepattern=r'revolut_.*\.csv',
    account=REVOLUT_ACCOUNT,
    currency='CHF',
)

_sbb = SBBImporter(
    account_expenses=SBB_ACCOUNT_EXPENSES,
    account_halbtax=SBB_ACCOUNT_HALBTAX,
    account_bank=SBB_ACCOUNT_BANK,
)

_ibkr = IBKRImporter(
    account=IBKR_ACCOUNT_ROOT,
    currency='CHF',
    account_map=IBKR_ACCOUNT_MAP,
    wht_account=IBKR_WHT_ACCOUNT,
)

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

pipelines = [
    {
        'name':        'sbb',
        'importer':    _sbb,
        'source_dir':  cfg.DOWNLOADS / 'sbb',
        'output_file': cfg.LEDGER_DIR / 'SBB.bean',
        'fixes':       fixes_sbb,
        'predict':     False,
    },
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
        'setup':       make_ibkr_setup(
                           cfg.IBKR_TOKEN,
                           cfg.IBKR_QUERY_ID,
                           cfg.IBKR_QUERY_NAME,
                           cfg.DOWNLOADS / 'ibkr',
                       ),
        'fixes':       fixes_ibkr,
        'predict':     False,   # all postings fully populated by the importer
    },
]

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    run_all(
        pipelines,
        ledger=cfg.LEDGER_DIR / 'main.bean',
        statement_dest=cfg.STATEMENT_DEST,
        dry_run_file=cfg.LEDGER_DIR / 'testoutput.bean',
        predict_lookback_days=PREDICT_LOOKBACK_DAYS,
    )
