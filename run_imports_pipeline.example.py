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

_log_dir = Path.cwd() / "logs"
_log_dir.mkdir(exist_ok=True)
logger.add(
    _log_dir / f"ingest_{datetime.now():%Y%m%d_%H%M%S}.log",
    level="DEBUG",
    encoding="utf-8",
)

import pipeline_secrets as cfg
from tariochbctools.importers.neon.importer import Importer as NeonImporter
from tariochbctools.importers.revolut.importer import Importer as RevolutImporter
from transaction_fixes import (
    fixes_ibkr,
    fixes_neon,
    fixes_pfg,
    fixes_revolut,
    fixes_sbb,
    fixes_zkb,
)

from drnukebean.importer.finpension import FinPensionImporter
from drnukebean.importer.ibkr import IBKRImporter
from drnukebean.importer.ibkr_flexquery import make_ibkr_setup
from drnukebean.importer.pfg import PFGImporter
from drnukebean.importer.sbb import SBBImporter
from drnukebean.importer.zkb_camt import ZKBCamtImporter
from drnukebean.importer.zkb_ebics import make_zkb_setup
from drnukebean.pipeline.runner import run_all


ZKB_ACCOUNT = "Assets:Bank:ZKB:CHF"

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

pipelines = [
    {
        "name": "finpension",
        # root_account is a template: regex groups (pillar, portfolio) are substituted from
        # each filename, so one importer instance covers all finpension portfolios.
        # The placeholder values in root_account must be valid regex group matches.
        "importer": FinPensionImporter(
            root_account="Assets:Invest:Finpension:S3a:Portfolio1",
            isin_lookup={
                "CH0012345678": "FUNDA",
                "CH0098765432": "FUNDB",
                # add one entry per fund held across all finpension portfolios. 
                # must match beancount commodity tickers
            },
            div_suffix="Div",               # dividends subaccount 
            interest_suffix="Interest",     # interest subaccount suffix
            fees_suffix="Fees",             # fees subaccount suffix
            year=datetime.now().year-1,     # None imports full .csv export
            ignore_funds_transfers=False,   # True to silently drop Deposit rows
            # the regex pattern extracts the pillar (2; 3a) and the portfolio number
            # must return both as groups in that order e.g for finpension_S3a_Portfolio1.csv
            regex=r"finpension_(S[23][a]?)_(Portfolio\d)",  
        ),
        "source_dir": cfg.DOWNLOADS / "finpension",
        "bean_output_file": cfg.LEDGER_DIR / "FinPension.bean",
        "predict": False,  # all postings fully populated by the importer
    },
    {
        "name": "pfg",
        "importer": PFGImporter(
            iban=cfg.PFG_IBAN,
            account="Assets:Bank:PFG:CHF",
        ),
        "source_dir": cfg.DOWNLOADS / "pfg",
        "bean_output_file": cfg.LEDGER_DIR / "PFG.bean",
        "fixes": fixes_pfg,
        "predict": True,
    },
    {
        "name": "sbb",
        "importer": SBBImporter(
            account_expenses="Expenses:Travel:SBB",
            account_halbtax="Assets:Prepaid:HalbtaxPlus",
            account_bank=ZKB_ACCOUNT,  # counter-account for non-Halbtax-PLUS rows
        ),
        "source_dir": cfg.DOWNLOADS / "sbb",
        "bean_output_file": cfg.LEDGER_DIR / "SBB.bean",
        "fixes": fixes_sbb,
        "predict": False,
    },
    {
        "name": "zkb",
        "importer": ZKBCamtImporter(
            iban=cfg.ZKB_IBAN,
            account=ZKB_ACCOUNT,
            currency="CHF",
        ),
        "source_dir": cfg.DOWNLOADS / "zkb",
        "bean_output_file": cfg.LEDGER_DIR / "ZKB.bean",
        "setup": make_zkb_setup(cfg.ZKB_CREDENTIALS, cfg.DOWNLOADS / "zkb"),
        "fixes": fixes_zkb,
        "predict": True,
    },
    {
        "name": "neon",
        "importer": NeonImporter(
            filepattern=r"neon_.*\.csv",
            account="Assets:Bank:Neon:CHF",
        ),
        "source_dir": cfg.DOWNLOADS / "neon",
        "bean_output_file": cfg.LEDGER_DIR / "Neon.bean",
        "fixes": fixes_neon,
        "predict": True,
    },
    {
        "name": "revolut",
        "importer": RevolutImporter(
            filepattern=r"revolut_.*\.csv",
            account="Assets:Bank:Revolut:CHF",
            currency="CHF",
        ),
        "source_dir": cfg.DOWNLOADS / "revolut",
        "bean_output_file": cfg.LEDGER_DIR / "Revolut.bean",
        "fixes": fixes_revolut,
        "predict": True,
    },
    {
        "name": "ibkr",
        "importer": IBKRImporter(
            account="Assets:Invest:IBKR",
            currency="CHF",
            # Map IBKR account IDs (from pipeline_secrets.py) to beancount account config.
            # Add one entry per IBKR account; see pipeline_secrets.example.py for ID constants.
            account_map={
                cfg.IBKR_ACCOUNT_1: {
                    "root": "Assets:Invest:IBKR:AccountName1",
                    "deposit_from": ZKB_ACCOUNT,
                },
                cfg.IBKR_ACCOUNT_2: {
                    "root": "Assets:Invest:IBKR:AccountName2",
                    # no deposit_from -> deposits flagged '!' for manual annotation
                },
            },
            wht_account="Expenses:Invest:IBKR:WHT",
        ),
        "source_dir": cfg.DOWNLOADS / "ibkr",
        "bean_output_file": cfg.LEDGER_DIR / "IBKR.bean",
        "setup": make_ibkr_setup(
            cfg.IBKR_TOKEN,
            cfg.IBKR_QUERY_ID,
            cfg.IBKR_QUERY_NAME,
            cfg.DOWNLOADS / "ibkr",
        ),
        "fixes": fixes_ibkr,
        "predict": False,  # all postings fully populated by the importer
    },
]

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_all(
        pipelines,
        ledger=cfg.LEDGER_DIR / "main.bean",
        statement_dest=cfg.STATEMENT_DEST,
        dry_run_file=cfg.LEDGER_DIR / "testoutput.bean",
        predict_lookback_days=730,  # days of ledger history for smart_importer; None = full
    )
