"""
Minimal runner example for the ZKB CAMT.053 importer.

This script demonstrates how to wire the ZKBCamtImporter into beangulp's
Ingest runner with an optional smart_importer prediction layer.

Usage (beangulp subcommands):
    # Show which files in a folder this importer would pick up
    python ZKB_runner_example.py identify ~/downloads/zkb/

    # Extract transactions and print to stdout
    python ZKB_runner_example.py extract ~/downloads/zkb/ -e ledger.bean

    # Archive (move/rename) source files after a successful import
    python ZKB_runner_example.py archive ~/downloads/zkb/ -o ~/archive/zkb/

Write extract output directly to a file:
    python ZKB_runner_example.py extract ~/downloads/zkb/ -e ledger.bean -o ZKB.bean

"""

import beangulp.extract
from beangulp import Ingest
from smart_importer import PredictPostings
from smart_importer.wrapper import ImporterWrapper

from drnukebean.importer.zkb_camt import ZKBCamtImporter

beangulp.extract.HEADER = '' # remove import headers
beangulp.extract.SECTION = '; ** {}' # prevent syntax errors / outcomment file info in output


importer = ZKBCamtImporter(
    iban='CH1234567890123456789',          # IBAN as shown on your ZKB statement
    account='Assets:Bank:ZKB',         # beancount account for this ZKB account
    currency='CHF',
)

importer = ImporterWrapper(importer, PredictPostings())

ingest = Ingest([importer])

if __name__ == '__main__':
    ingest()
