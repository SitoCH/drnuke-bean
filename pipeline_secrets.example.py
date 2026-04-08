"""
pipeline_secrets.example.py  --  template for machine-local secrets and paths.

Copy this file to pipeline_secrets.py and fill in the real values.
pipeline_secrets.py is gitignored and must never be committed.

Contents: local filesystem paths, API credentials, account IDs.
Beancount account names belong in run_imports_pipeline.py, not here.
"""

from pathlib import Path

from drnukebean.importer.zkb_ebics import ZKBCredentials

# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------

# Root directory where the pipeline deposits downloaded statement files.
# Finpension CSV exports should be placed in a "finpension/" subdirectory,
# with filenames matching the pattern finpension_(S[23][a]?)_(Portfolio\d)*.csv,
# e.g. finpension_S3a_Portfolio1_2025.csv.
DOWNLOADS = Path("/path/to/DownloadsDir")

# Root of the beancount ledger. Output .bean files are written here.
LEDGER_DIR = Path("/path/to/ledger.bean")

# Archive root for source files after a successful full run.
# Must NOT be a subdirectory of DOWNLOADS (archived files would be re-identified).
STATEMENT_DEST = Path("/path/to/statements/archive")

# ---------------------------------------------------------------------------
# ZKB credentials (EBICS H005)
# ---------------------------------------------------------------------------

ZKB_IBAN = "CHxx xxxx xxxx xxxx xxxx x"  # spaces optional

ZKB_CREDENTIALS = ZKBCredentials(
    keys_file="/path/to/zkb_keyring.file",  # fintech EBICS keyring file
    passphrase="...",  # keyring encryption passphrase
    host_id="...",  # HostID from bank letter
    url="https://...",  # EBICS server URL from bank letter
    partner_id="...",  # PartnerID / ContractID at ZKB
    user_id="...",  # UserID
)

# ---------------------------------------------------------------------------
# IBKR FlexQuery credentials
# ---------------------------------------------------------------------------

IBKR_TOKEN = "..."  # Flex Web Service token (from IBKR Account Management)
IBKR_QUERY_ID = "..."  # numeric Flex Query ID
IBKR_QUERY_NAME = "..."  # queryName in the FlexQueryResponse XML; validated on download

# ---------------------------------------------------------------------------
# IBKR account IDs
# ---------------------------------------------------------------------------

# IBKR account IDs (accountId attribute in the FlexQuery XML).
# One constant per account; used as keys in IBKR_ACCOUNT_MAP in run_imports_pipeline.py.
# Every ID present in the FlexQuery response must have a corresponding entry there;
# a missing ID raises a RuntimeError at import time.

IBKR_ACCOUNT_1 = "U1234567"
IBKR_ACCOUNT_2 = "U7654321"
