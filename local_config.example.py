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
    keys_file='/path/to/zkb_keyring.file',   # fintech EBICS keyring file
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

# ---------------------------------------------------------------------------
# IBKR account map
# ---------------------------------------------------------------------------

# Maps each IBKR account ID (the accountId attribute in the FlexQuery XML) to
# its beancount account configuration.
#
# Required key:  'root'         -- beancount account root for all positions and
#                                  cash held in that IBKR account.
# Optional key:  'deposit_from' -- counterpart account for cash deposits /
#                                  withdrawals.  When omitted the transaction is
#                                  emitted with a single IBKR leg and flag '!'
#                                  so it surfaces in bean-check for manual
#                                  completion.
#
# Every account ID present in the FlexQuery response must appear here;
# a missing ID causes a RuntimeError at import time.
#
# IBKR_ACCOUNT_MAP = None  # single-account mode: all statements -> IBKRImporter(account=...)
IBKR_ACCOUNT_MAP = {
    'U1234567': {
        'root': 'Assets:Invest:IBKR:AccountName1',
        'deposit_from': 'Assets:Bank:ZKB:CHF',    # funded from ZKB
    },
    'U7654321': {
        'root': 'Assets:Invest:IBKR:AccountName2',
        # no deposit_from -> deposits flagged '!' for manual annotation
    },
}


