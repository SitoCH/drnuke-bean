"""
Beancount v3 importer for SBB billing CSV exports.

Covers all SBB transactions. Rows paid via Halbtax PLUS / Half Fare Card are
drawn from the prepaid Halbtax PLUS asset account; all other rows are drawn from
a configurable bank account (or emitted with flag '!' and no postings when no
bank account is configured). The expense leg always uses account_expenses.

Expected CSV columns -- DE:
    Tarif, Strecke, Via (optional), Preis, Mitreisende, Reisedatum,
    Gültigkeit, Bestelldatum, Bestellnummer, Zahlungsmittel, E-Mail Käufer:in
Expected CSV columns -- EN:
    Tariff, Route, Via (optional), Price, Co-passenger(s), Travel date,
    Validity, Order date, Order number, Payment methods, Purchaser e-mail

Quickstart:
    Download the CSV from SBB profile -> Orders -> "Export as list" -> CSV

run_imports.py wiring example:

    from drnukebean.importer.sbb import SBBImporter

    _sbb = SBBImporter(
        account_halbtax=<SBB_ACCOUNT_HALBTAX>,
        account_bank=<SBB_ACCOUNT_BANK>,
        account_expenses=<SBB_ACCOUNT_EXPENSES>,
    )

    pipelines = [
        ...
        {
            'name':        'sbb',
            'importer':    _sbb,
            'source_dir':  cfg.DOWNLOADS / 'sbb',
            'bean_output_file': cfg.LEDGER_DIR / 'SBB.bean',
            'fixes':       fixes_sbb,
        },
    ]
"""

import csv
from datetime import date, datetime

import beangulp
from beancount.core import amount, data
from beancount.core.number import D

# Payment method values that identify Halbtax PLUS purchases.
HALBTAX_PLUS: frozenset[str] = frozenset({"Halbtax PLUS", "Half Fare Card PLUS"})

CURRENCY = "CHF"

# Header fingerprints -- three distinctive columns per language are enough.
HEADERS_DE = ("Tarif", "Strecke", "Zahlungsmittel")
HEADERS_EN = ("Tariff", "Route", "Payment methods")


def _col(row: dict, *keys: str) -> str:
    """Return the value of the first key found in row, or empty string.

    Used to transparently support DE and EN column names side by side.
    """
    for k in keys:
        if k in row:
            return row[k]
    return ""


def _parse_date(date_str: str) -> date:
    """Parse DD.MM.YYYY date strings as used in the SBB export."""
    return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()


class SBBImporter(beangulp.Importer):
    """Importer for SBB CSV billing exports.

    Each transaction is emitted with two postings: the counter posting
    (halbtax or bank, negative amount) and the expense posting.

    Args:
        account_halbtax:  Prepaid Halbtax PLUS asset account, e.g.
                          'Assets:Prepaid:HalbtaxPlus'. Used as the counter-account
                          for rows paid via Halbtax PLUS / Half Fare Card PLUS.
        account_bank:     Bank account used as counter-account for all other rows,
                          e.g. 'Assets:Bank:ZKB:CHF'. When empty (default), non-HT
                          rows are emitted with flag '!' and no postings for manual
                          completion.
        account_expenses: Expense account for all SBB purchases, e.g.
                          'Expenses:Transport:SBB'.
    """

    def __init__(
        self,
        account_halbtax: str,
        account_bank: str = "",
        account_expenses: str = "",
    ) -> None:
        self.account_halbtax = account_halbtax
        self.account_bank = account_bank
        self.account_expenses = account_expenses

    # ------------------------------------------------------------------
    # beangulp interface
    # ------------------------------------------------------------------

    def identify(self, filepath: str) -> bool:
        """Return True when the file looks like an SBB billing export."""
        try:
            with open(filepath, encoding="utf-8-sig") as f:
                first_line = f.readline()
        except Exception:
            return False

        return all(h in first_line for h in HEADERS_DE) or all(h in first_line for h in HEADERS_EN)

    def account(self, filepath: str | None = None) -> str:
        """The primary account for this importer (used by beangulp for archiving)."""
        return self.account_halbtax

    @property
    def name(self) -> str:
        """Return the importer name."""
        return f"sbb.{self.account_halbtax}"

    def date(self, filepath: str) -> date | None:
        """Use the latest order date found in the file as the file date."""
        dates = []
        for _lineno, row in self._iter_rows(filepath):
            try:
                dates.append(_parse_date(_col(row, "Bestelldatum", "Order date")))
            except (KeyError, ValueError):
                pass
        return max(dates) if dates else None

    def filename(self, filepath: str) -> str:
        return "sbb.csv"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        """Parse the CSV and produce one Transaction per row."""
        entries: data.Entries = []

        for lineno, row in self._iter_rows(filepath):
            try:
                entry = self._row_to_transaction(filepath, row, lineno)
            except Exception as e:
                # Emit a Note so the error is visible in the output rather
                # than silently dropping the row or crashing the import.
                meta = data.new_metadata(filepath, lineno)
                entries.append(
                    data.Note(
                        meta,
                        datetime.today().date(),
                        self.account_halbtax,
                        f"Parse error: {e}",
                        frozenset(),
                        frozenset(),
                    )
                )
                continue

            entries.append(entry)

        return entries

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _iter_rows(self, filepath: str):
        """Yield (lineno, row) pairs from the CSV, handling UTF-8 BOM if present."""
        with open(filepath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            yield from enumerate(reader, start=2)

    def _row_to_transaction(self, filepath: str, row: dict, lineno: int) -> data.Transaction:
        """Convert a single CSV row into a beancount Transaction."""

        # Order date becomes the transaction date.
        tx_date = _parse_date(_col(row, "Bestelldatum", "Order date"))

        meta = data.new_metadata(filepath, lineno)

        payee = "SBB"
        narration = _col(row, "Strecke", "Route").strip().replace("→", "->")
        if not narration:
            narration = _col(row, "Tarif", "Tariff").strip()
        reisedatum_str = _col(row, "Reisedatum", "Travel date").strip()
        try:
            reisedatum = _parse_date(reisedatum_str)
        except (ValueError, AttributeError):
            reisedatum = None
        if reisedatum is not None and reisedatum != tx_date:
            suffix = reisedatum_str if reisedatum_str else reisedatum.strftime("%d.%m.%Y")
            narration = f"{narration}, {suffix}" if narration else suffix

        # Parse price -- drop any thousands separator (apostrophe in CH locale).
        price_str = _col(row, "Preis", "Price").strip().replace("'", "")
        price = D(price_str)
        amt = amount.Amount(price, CURRENCY)

        # Select counter-account based on payment method.
        payment = _col(row, "Zahlungsmittel", "Payment methods").strip()
        if payment in HALBTAX_PLUS:
            counter = data.Posting(self.account_halbtax, -amt, None, None, None, None)
            flag = "*"
        elif self.account_bank:
            counter = data.Posting(self.account_bank, -amt, None, None, None, None)
            flag = "*"
        else:
            # No counter account known; emit flag '!' with no postings for manual completion.
            counter = None
            flag = "!"

        if counter is None:
            postings = []
        elif self.account_expenses:
            postings = [
                counter,
                data.Posting(self.account_expenses, None, None, None, None, None),
            ]
        else:
            postings = [counter]

        return data.Transaction(
            meta,
            tx_date,
            flag,
            payee,
            narration,
            data.EMPTY_SET,  # tags
            data.EMPTY_SET,  # links
            postings,
        )
