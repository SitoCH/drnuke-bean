"""
local_config.py.example  --  template for the machine-local pipeline configuration.

Copy this file to local_config.py and fill in the real values.
local_config.py is gitignored and must never be committed.

All sensitive values (credentials, passphrases, API tokens) live here.
run_imports.py imports this module as `cfg` and accesses values by name.
"""

from pathlib import Path

from drnukebean.importer.zkb_ebics import ZKBCredentials

# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------

# Root directory where the pipeline deposits downloaded statement files.
# Each importer gets its own subdirectory (zkb/, ibkr/, neon/, revolut/).
DOWNLOADS = Path('/path/to/downloads')

# Root of the beancount ledger.  Output .bean files are written here.
LEDGER_DIR = Path('/path/to/ledger')

# Archive root for source files after a successful full run.
# Files are moved to <STATEMENT_DEST>/<account-as-path>/<suggested-filename>.
# Must NOT be a subdirectory of DOWNLOADS (the runner scans DOWNLOADS recursively
# and would re-identify archived files on the next run).
STATEMENT_DEST = Path('/path/to/statements/archive')

# ---------------------------------------------------------------------------
# ZKB credentials (EBICS H005)
# ---------------------------------------------------------------------------

# All fields come from the bank letter and the EBICS initialisation process.
ZKB_IBAN = 'CHxx xxxx xxxx xxxx xxxx x'   # IBAN without spaces also accepted

ZKB_CREDENTIALS = ZKBCredentials(
    keys_file='/path/to/zkb_keyring.db',   # fintech EBICS keyring file
    passphrase='...',                        # keyring encryption passphrase
    host_id='...',                           # HostID from bank letter
    url='https://...',                       # EBICS server URL from bank letter
    partner_id='...',                        # PartnerID / ContractID at ZKB
    user_id='...',                           # UserID
)

# ---------------------------------------------------------------------------
# IBKR FlexQuery credentials
# ---------------------------------------------------------------------------

IBKR_TOKEN = '...'         # Flex Web Service token (from IBKR Account Management)
IBKR_QUERY_ID = '...'      # numeric Flex Query ID
IBKR_QUERY_NAME = '...'    # queryName as it appears in the FlexQueryResponse XML;
                            # validated on every download to catch token/ID mismatches
