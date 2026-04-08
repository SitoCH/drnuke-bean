"""
Beancount v3 importer for PostFinance giro account CSV exports.

Format: semicolon-delimited CSV, UTF-8, exported from the PostFinance online portal.

File structure:
    Row 0:  Datum von:     ;="DD.MM.YYYY"   -> date_from
    Row 1:  Datum bis:     ;="DD.MM.YYYY"   -> date_to
    Row 2:  Kategorie:     ;="Alle"         -> ignored
    Row 3:  Konto:         ;="CH..."        -> IBAN
    Row 4:  Währung:       ;="CHF"          -> currency
    Row 5:  (empty)
    Row 6:  column headers (Datum; Avisierungstext; Gutschrift in CHF; ...)
    Row 7:  (empty)
    Row 8+: transaction data (8 columns, reverse-chronological)
    Last:   disclaimer text row (skipped)

Transaction columns (DE / EN):
    0  Datum              / Date
    1  Avisierungstext    / Notification text
    2  Gutschrift in CHF  / Credit in CHF      (positive or empty)
    3  Lastschrift in CHF / Debit in CHF       (negative or empty)
    4  Label              / Label              (ignored)
    5  Kategorie          / Category           (ignored)
    6  Valuta             / Value              (ignored)
    7  Saldo in CHF       / Balance in CHF     (sparse; present = running balance after tx)

Sign convention: debit values are already negative. credit + debit gives the
correctly signed transaction amount.

Each transaction is emitted single-legged (account posting only). The counter
posting is filled by the pipeline's fixes function and/or smart_importer.

The closing balance is taken from the first transaction row that carries a
non-empty balance column. A Balance directive is emitted on date + 1 day.

Quickstart:
    Download the CSV from PostFinance -> Movements -> Export -> CSV

run_imports_pipeline.py wiring example:

    from drnukebean.importer.pfg import PFGImporter

    _pfg = PFGImporter(
        iban=cfg.PFG_IBAN,
        account='Assets:Bank:PFG:CHF',
    )

    pipelines = [
        ...
        {
            'name':        'pfg',
            'importer':    _pfg,
            'source_dir':  cfg.DOWNLOADS / 'pfg',
            'bean_output_file': cfg.LEDGER_DIR / 'PFG.bean',
            'fixes':       fixes_pfg,
            'predict':     True,
        },
    ]
"""

import csv
import functools
import re
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import beangulp
from beancount.core import data
from beancount.core.amount import Amount
from loguru import logger

from drnukebean.importer.util import remove_spaces

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_DATE_FMT = "%d.%m.%Y"
_DELIMITER = ";"
_HEADER_SKIP = 6  # rows before the DictReader column-header row

# Column names in DE and EN (passed as alternates to _col())
# Only strings matching this pattern are attempted as dates; everything else is
# silently skipped (covers blank rows and trailing disclaimer rows in the export).
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

_COL_DATE = ("Datum", "Date")
_COL_TEXT = ("Avisierungstext", "Notification text")
_COL_CREDIT = ("Gutschrift in CHF", "Credit in CHF")
_COL_DEBIT = ("Lastschrift in CHF", "Debit in CHF")
_COL_BALANCE = ("Saldo in CHF", "Balance in CHF")


# ---------------------------------------------------------------------------
# Header dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PFGHeader:
    date_from: Date
    date_to: Date
    iban: str
    currency: str


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _strip_pf_cell(s: str) -> str:
    """Strip the PostFinance header cell wrapper: ="VALUE" -> VALUE."""
    return s.strip("=").strip('"')


def _decimal_or_zero(s: str) -> Decimal:
    """Convert a PF amount string to Decimal without a float intermediate.

    Handles:
    - Swiss thousands separator (apostrophe): "1'234.56" -> Decimal("1234.56")
    - Empty strings -> Decimal("0")
    - Invalid values -> Decimal("0") with a warning
    """
    cleaned = s.strip().replace("'", "")
    if not cleaned:
        return Decimal("0")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        logger.warning("PFGImporter: could not parse amount {!r}; treating as 0", s)
        return Decimal("0")


def _parse_date(s: str) -> Date:
    """Parse a DD.MM.YYYY date string as used in PF exports."""
    return datetime.strptime(s.strip(), _DATE_FMT).date()


def _col(row: dict, *keys: str) -> str:
    """Return the value of the first key found in row, or empty string.

    Used to transparently support DE and EN column names side by side.
    """
    for k in keys:
        if k in row:
            return row[k]
    return ""


@functools.lru_cache(maxsize=64)
def _parse_header(filepath: str, encoding: str) -> _PFGHeader:
    """Read and cache the 5-row metadata header of a PF CSV file.

    Raises ValueError if the file cannot be parsed as a PF export.
    """
    with open(filepath, encoding=encoding) as f:
        reader = csv.reader(f, delimiter=_DELIMITER)
        try:
            rows = [next(reader) for _ in range(5)]
        except StopIteration as exc:
            raise ValueError(f"Cannot parse PF header in {filepath!r}: file too short") from exc

    try:
        date_from = _parse_date(_strip_pf_cell(rows[0][1]))
        date_to = _parse_date(_strip_pf_cell(rows[1][1]))
        iban = _strip_pf_cell(rows[3][1])
        currency = _strip_pf_cell(rows[4][1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Cannot parse PF header in {filepath!r}: {exc}") from exc

    return _PFGHeader(date_from=date_from, date_to=date_to, iban=iban, currency=currency)


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class PFGImporter(beangulp.Importer):
    """Beancount v3 importer for PostFinance giro account CSV exports.

    Args:
        iban:             Account IBAN as shown in the PF export header (spaces ignored).
        account:          Beancount account for this PF account, e.g. 'Assets:Bank:PFG:CHF'.
        balance_account:  Account for Balance directives. Defaults to `account`.
        currency:         Account currency, default 'CHF'.
        file_encoding:    File encoding, default 'utf-8'.
    """

    def __init__(
        self,
        iban: str,
        account: str,
        balance_account: str | None = None,
        currency: str = "CHF",
        file_encoding: str = "utf-8",
    ) -> None:
        self._iban = iban.replace(" ", "")
        self._account = account
        self._balance_account = balance_account if balance_account is not None else account
        self._currency = currency
        self._encoding = file_encoding

    # ------------------------------------------------------------------
    # beangulp interface
    # ------------------------------------------------------------------

    def identify(self, filepath: str) -> bool:
        """Return True when the file looks like a PF CSV export for this IBAN."""
        if Path(filepath).suffix.lower() != ".csv":
            return False
        try:
            header = _parse_header(filepath, self._encoding)
            return header.iban == self._iban
        except Exception:
            return False

    def account(self, filepath: str) -> str:
        return self._account

    @property
    def name(self) -> str:
        return f"pfg.{self._account}"

    def date(self, filepath: str) -> Date | None:
        """Return the statement from-date (lightweight — reads only the header)."""
        try:
            return _parse_header(filepath, self._encoding).date_from
        except Exception:
            return None

    def filename(self, filepath: str) -> str:
        """Suggest an archive filename: pfg_{date_from}_{last4iban}.csv"""
        try:
            header = _parse_header(filepath, self._encoding)
            return f"pfg_{header.date_from}_{self._iban[-4:]}.csv"
        except Exception:
            return "pfg.csv"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        """Parse the CSV and produce one Transaction per row, plus a Balance directive."""
        try:
            header = _parse_header(filepath, self._encoding)
        except ValueError as exc:
            logger.error("PFGImporter: {}", exc)
            return []

        if header.currency != self._currency:
            logger.error(
                "PFGImporter: currency mismatch — configured {!r}, file has {!r} in {}",
                self._currency,
                header.currency,
                filepath,
            )
            return []

        entries: data.Entries = []
        balance_emitted = False

        for lineno, row in self._iter_rows(filepath):
            # Skip rows that don't carry a DD.MM.YYYY date: blank separator rows
            # and the trailing disclaimer text rows in the PF export.
            raw_date = _col(row, *_COL_DATE).strip()
            if not _DATE_RE.match(raw_date):
                continue

            try:
                tx_date = _parse_date(raw_date)
            except ValueError as exc:
                meta = data.new_metadata(filepath, lineno)
                entries.append(
                    data.Note(
                        meta,
                        datetime.today().date(),
                        self._account,
                        f"Parse error (date): {exc}",
                        frozenset(),
                        frozenset(),
                    )
                )
                continue

            # Emit closing balance from the first row that carries a balance value
            if not balance_emitted:
                balance_entry = self._row_to_balance(filepath, row, lineno, tx_date)
                if balance_entry is not None:
                    entries.append(balance_entry)
                    balance_emitted = True

            try:
                entry = self._row_to_transaction(filepath, row, lineno, tx_date)
            except Exception as exc:
                meta = data.new_metadata(filepath, lineno)
                entries.append(
                    data.Note(
                        meta,
                        datetime.today().date(),
                        self._account,
                        f"Parse error: {exc}",
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
        """Yield (lineno, row_dict) pairs from the transaction section of the CSV.

        Skips the metadata header and the blank row between the column-header row
        and the first transaction. DictReader is used so column names are keys.
        """
        with open(filepath, encoding=self._encoding) as f:
            for _ in range(_HEADER_SKIP):
                next(f)
            reader = csv.DictReader(f, delimiter=_DELIMITER)
            # lineno starts at _HEADER_SKIP + 1 (column-header row) + 1 (data rows)
            yield from enumerate(reader, start=_HEADER_SKIP + 2)

    def _row_to_transaction(
        self, filepath: str, row: dict, lineno: int, tx_date: Date
    ) -> data.Transaction:
        """Convert a single CSV row dict into a beancount Transaction."""
        credit = _decimal_or_zero(_col(row, *_COL_CREDIT))
        debit = _decimal_or_zero(_col(row, *_COL_DEBIT))
        total = credit + debit  # debit is already negative in the PF export
        amount = Amount(total, self._currency)
        narration = remove_spaces(_col(row, *_COL_TEXT))
        meta = data.new_metadata(filepath, lineno)

        return data.Transaction(
            meta,
            tx_date,
            "*",
            "",
            narration,
            data.EMPTY_SET,
            data.EMPTY_SET,
            [data.Posting(self._account, amount, None, None, None, None)],
        )

    def _row_to_balance(
        self, filepath: str, row: dict, lineno: int, tx_date: Date
    ) -> data.Balance | None:
        """Return a Balance directive if this row carries a non-empty balance value."""
        raw = _col(row, *_COL_BALANCE).strip()
        if not raw:
            return None
        balance_amount = Amount(_decimal_or_zero(raw), self._currency)
        meta = data.new_metadata(filepath, lineno)
        return data.Balance(
            meta,
            tx_date + timedelta(days=1),
            self._balance_account,
            balance_amount,
            None,
            None,
        )
