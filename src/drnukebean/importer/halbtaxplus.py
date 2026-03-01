"""
Beancount v2 importer for SBB billing CSV exports.

Only imports rows paid via "Halbtax PLUS" / "Half Fare Card", as those do
not appear on any bank statement and would otherwise go unrecorded.

Expected CSV columns — DE:
    Tarif, Strecke, Via (optional), Preis, Mitreisende, Reisedatum,
    Gültigkeit, Bestelldatum, Bestellnummer, Zahlungsmittel, E-Mail Käufer:in
Expected CSV columns — EN:
    Tariff, Route, Via (optional), Price, Co-passenger(s), Travel date,
    Validity, Order date, Order number, Payment methods, Purchaser e-mail

Quickstart:
    Download the CSV from SBB profile -> Orders -> "Export as list" -> CSV

Invocation:
    bean-extract config_halbtax.py <source_folder> -f ledger_main.bean

config_halbtax.py should look like:

    from drnukebean.importer.halbtaxplus import HalbtaxPlusImporter
    from beancount.ingest import extract

    extract.HEADER = ''

    CONFIG = [
        HalbtaxPlusImporter(
            account_expenses="expenses account here",
            account_asset="HalbtaxPlus asset account here",
        ),
    ]
"""

import csv
from datetime import datetime

from beancount.core import amount, data, flags
from beancount.core.number import D
from beancount.ingest import importer

# Payment method values that identify Halbtax PLUS purchases.
HALBTAX_PLUS = {"Halbtax PLUS", "Half Fare Card PLUS"}

CURRENCY = "CHF"

# Header fingerprints — three distinctive columns per language are enough.
HEADERS_DE = ("Tarif", "Strecke", "Zahlungsmittel")
HEADERS_EN = ("Tariff", "Route", "Payment methods")


def _col(row, *keys):
    """Return the value of the first key found in row, or empty string.

    Used to transparently support DE and EN column names side by side.
    """
    for k in keys:
        if k in row:
            return row[k]
    return ""


def _parse_date(date_str: str):
    """Parse DD.MM.YYYY date strings as used in the SBB export."""
    return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()


class HalbtaxPlusImporter(importer.ImporterProtocol):
    """Importer for SBB CSV exports — Halbtax PLUS / Half Fare Card rows only."""

    def __init__(self, account_expenses, account_asset):
        self.account_expenses = account_expenses
        self.account_asset    = account_asset

    def identify(self, file) -> bool:
        """Return True when the file looks like an SBB billing export."""
        try:
            with open(file.name, encoding="utf-8-sig") as f:
                first_line = f.readline()
        except Exception:
            return False

        return (
            all(h in first_line for h in HEADERS_DE) or
            all(h in first_line for h in HEADERS_EN)
        )

    def file_account(self, file) -> str:
        """The 'home' account for this file."""
        return self.account_asset

    def file_date(self, file):
        """Use the latest order date found in the file as the file date."""
        dates = []
        for row in self._iter_rows(file):
            try:
                dates.append(_parse_date(_col(row, "Bestelldatum", "Order date")))
            except (KeyError, ValueError):
                pass
        return max(dates) if dates else None

    def file_name(self, file) -> str:
        return "sbb_halbtax.csv"

    def extract(self, file, existing_entries=None):
        """Parse the CSV and produce one Transaction per Halbtax PLUS row."""
        entries = []

        for row in self._iter_rows(file):
            # Skip rows not paid via Halbtax PLUS (e.g. Raiffeisen Pay).
            if _col(row, "Zahlungsmittel", "Payment methods").strip() not in HALBTAX_PLUS:
                continue

            try:
                entry = self._row_to_transaction(file, row)
            except Exception as e:
                # Emit a Note so the error is visible in the output rather
                # than silently dropping the row or crashing the import.
                meta = data.new_metadata(file.name, 0)
                entries.append(
                    data.Note(meta, datetime.today().date(),
                              self.account_asset, f"Parse error: {e}")
                )
                continue

            entries.append(entry)

        return entries

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _iter_rows(self, file):
        """Yield CSV rows as dicts, handling UTF-8 BOM if present."""
        with open(file.name, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield row

    def _row_to_transaction(self, file, row):
        """Convert a single CSV row into a beancount Transaction."""

        # Order date becomes the transaction date.
        tx_date = _parse_date(_col(row, "Bestelldatum", "Order date"))

        meta = data.new_metadata(file.name, 0)

        # Payee is always SBB; narration is the route.
        payee     = "SBB"
        narration = (_col(row, "Strecke", "Route").strip().replace("→", "->")
                     .replace("↔", "<->"))
        # Stash useful extras as beancount metadata.
        meta["reisedatum"]    = _col(row, "Reisedatum",    "Travel date")
        meta["bestellnummer"] = _col(row, "Bestellnummer", "Order number")
        meta["tarif"]         = _col(row, "Tarif",         "Tariff")

        # Parse price — drop any thousands separator (apostrophe in CH locale).
        price_str = _col(row, "Preis", "Price").strip().replace("'", "")
        price = D(price_str)
        amt   = amount.Amount(price, CURRENCY)

        # Two postings:
        #   Expense leg — positive (money was spent)
        #   Asset leg   — negative (reduces the Halbtax PLUS prepaid balance)
        postings = [
            data.Posting(self.account_expenses,  amt, None, None, None, None),
            data.Posting(self.account_asset,     -amt, None, None, None, None),
        ]

        return data.Transaction(
            meta,
            tx_date,
            flags.FLAG_OKAY,  # '*' — treat as cleared
            payee,
            narration,
            data.EMPTY_SET,   # tags
            data.EMPTY_SET,   # links
            postings,
        )