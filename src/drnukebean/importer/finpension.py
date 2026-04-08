"""
Beancount v3 importer for Finpension CSV exports (pillar 2 & 3a).

Finpension always exports the full transaction history since account opening.
Use the ``year`` parameter to restrict extraction to a single calendar year.

Expected CSV columns (semicolon-delimited, UTF-8-BOM):
    Date; Category; Asset Name; ISIN; Number of Shares; Asset Currency;
    Currency Rate; Asset Price in CHF; Cash Flow; Balance

Holdings are treated as currency-like positions: trades use a ``price``
annotation rather than cost-basis lots. No PnL auto-complete posting is emitted.

run_imports.py wiring example::

    from drnukebean.importer.finpension import FinPensionImporter

    _fp = FinPensionImporter(
        root_account="Assets:Invest:FP:S3a:Portfolio1",
        isin_lookup={"CH0012345678": "FUNDA", "CH0098765432": "FUNDB"},
        year=2025,
    )

    pipelines = [
        {
            "name": "finpension",
            "importer": _fp,
            "source_dir": cfg.DOWNLOADS / "finpension",
            "bean_output_file": cfg.LEDGER_DIR / "FinPension.bean",
        },
    ]
"""

import csv
import re
from datetime import date, timedelta
from pathlib import Path

import beangulp
from beancount.core import amount, data
from beancount.core.number import D
from loguru import logger

# ---------------------------------------------------------------------------
# CSV column names
# ---------------------------------------------------------------------------

_COL_DATE = "Date"
_COL_CATEGORY = "Category"
_COL_ASSET_NAME = "Asset Name"
_COL_ISIN = "ISIN"
_COL_SHARES = "Number of Shares"
_COL_CURRENCY = "Asset Currency"
_COL_ASSET_PRICE = "Asset Price in CHF"
_COL_CASHFLOW = "Cash Flow"
_COL_BALANCE = "Balance"

# ---------------------------------------------------------------------------
# Category sets (pillar 2 and 3a use different strings for the same concept)
# ---------------------------------------------------------------------------

_CATEGORIES_TRADE = frozenset({"Buy", "Sell", "Portfolio Transaction"})
_CATEGORIES_DEPOSIT = frozenset({"Deposit", "Transfer vested benefits"})
_CATEGORIES_FEE = frozenset(
    {
        "Flat-rate administrative fee",
        "Flat-rate administration fee",
        "Implementation fees",
    }
)
_CATEGORIES_DIVIDEND = frozenset({"Dividend", "Dividend and Interest Distributions"})


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class FinPensionImporter(beangulp.Importer):
    """Beancount v3 importer for Finpension CSV exports.

    Args:
        root_account:           Root account, e.g. ``Assets:Invest:FP:S3a:Portfolio1``.
                                Must contain the pillar placeholder (``S2``, ``S3``, or
                                ``S3a``) and portfolio placeholder (``Portfolio<N>``) that
                                the regex will substitute from the filename.
        isin_lookup:            Mapping of ISIN -> ticker symbol, e.g.
                                ``{"CH0012345678": "FUNDA"}``.
        deposit_account:        Unused when ``ignore_funds_transfers=True``. Kept for
                                compatibility; the deposit transaction uses a single
                                cash posting that smart_importer can complete.
        div_suffix:             Sub-account suffix for dividend income.
        interest_suffix:        Sub-account suffix for interest income.
        fees_suffix:            Sub-account suffix for fee expenses.
        file_encoding:          CSV encoding. Finpension exports use UTF-8-BOM.
        regex:                  Pattern to extract (pillar, portfolio) from the filename.
        year:                   If set, only rows from this calendar year are extracted.
                                ``None`` imports the full history.
        ignore_funds_transfers: If ``True``, Deposit and Transfer rows are silently
                                dropped. If ``False`` (default), a single-posting
                                transaction is emitted for smart_importer to complete.
    """

    def __init__(
        self,
        root_account: str,
        isin_lookup: dict[str, str],
        deposit_account: str = "",
        div_suffix: str = "Div",
        interest_suffix: str = "Interest",
        fees_suffix: str = "Fees",
        file_encoding: str = "utf-8-sig",
        regex: str = r"finpension_(S[23][a]?)_(Portfolio\d)",
        year: int | None = None,
        ignore_funds_transfers: bool = False,
    ) -> None:
        self.root_account = root_account
        self.isin_lookup = isin_lookup
        self.deposit_account = deposit_account
        self.div_suffix = div_suffix
        self.interest_suffix = interest_suffix
        self.fees_suffix = fees_suffix
        self.file_encoding = file_encoding
        self.flag = "*"
        self.regex = regex
        self.year = year
        self.ignore_funds_transfers = ignore_funds_transfers
        self.main_account: str = root_account  # overwritten by fix_accounts

    # ------------------------------------------------------------------
    # beangulp interface
    # ------------------------------------------------------------------

    def identify(self, filepath: str) -> bool:
        result = bool(re.search(self.regex, filepath, re.IGNORECASE))
        logger.info(
            f"identify assertion for finpension importer and file '{filepath}': {result}"
        )
        return result

    def account(self, filepath: str) -> str:
        self.fix_accounts(filepath)
        return self.main_account

    @property
    def name(self) -> str:
        return f"finpension.{self.root_account}"

    def filename(self, filepath: str) -> str:
        return Path(filepath).name

    def date(self, filepath: str) -> date | None:
        rows = self._read_rows(filepath)
        filtered = self._filter_year(rows)
        if not filtered:
            return None
        return max(self._parse_date(r[_COL_DATE]) for r in filtered)

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        self.fix_accounts(filepath)
        rows = self._read_rows(filepath)
        filtered = self._filter_year(rows)

        entries: list = []
        for row in filtered:
            category = row[_COL_CATEGORY].strip()
            if category in _CATEGORIES_TRADE or category == "Liquidation distribution":
                entry = self._handle_trade(row)
            elif category in _CATEGORIES_DEPOSIT:
                entry = self._handle_deposit(row)
            elif category in _CATEGORIES_FEE:
                entry = self._handle_fee(row)
            elif category == "Interests":
                entry = self._handle_interest(row)
            elif category in _CATEGORIES_DIVIDEND:
                entry = self._handle_dividend(row)
            else:
                logger.warning(
                    f"finpension: unknown category '{category}', skipping row on {row[_COL_DATE]}"
                )
                entry = None

            if entry is not None:
                entries.append(entry)

        entries.extend(self._make_balances(filtered))
        return entries

    # ------------------------------------------------------------------
    # Account helpers (kept from v2, read self.main_account)
    # ------------------------------------------------------------------

    def getLiquidityAccount(self, currency: str) -> str:
        return ":".join([self.main_account, currency])

    def getDivIncomeAcconut(self, currency: str, symbol: str) -> str:
        return ":".join(
            [self.main_account.replace("Assets", "Income"), symbol, self.div_suffix]
        )

    def getInterestIncomeAcconut(self, currency: str) -> str:
        return ":".join(
            [self.main_account.replace("Assets", "Income"), self.interest_suffix, currency]
        )

    def getAssetAccount(self, symbol: str) -> str:
        return ":".join([self.main_account, symbol])

    def getFeesAccount(self, currency: str) -> str:
        return ":".join(
            [self.main_account.replace("Assets", "Expenses"), self.fees_suffix, currency]
        )

    # ------------------------------------------------------------------
    # fix_accounts (adapted from v2: filepath str instead of file object)
    # ------------------------------------------------------------------

    def fix_accounts(self, filepath: str) -> None:
        try:
            pillar, portfolio = re.search(self.regex, filepath, re.IGNORECASE).groups()
        except AttributeError as e:
            logger.error(
                f"could not extract pillar and/or portfolio from filename {filepath} "
                f"with regex pattern {self.regex}."
            )
            raise AttributeError(e) from e
        new_account = re.sub(r"S[23]a?", pillar, self.root_account)
        self.main_account = re.sub(r"Portfolio\d", portfolio, new_account)

    # ------------------------------------------------------------------
    # Transaction handlers
    # ------------------------------------------------------------------

    def _handle_trade(self, row: dict) -> data.Transaction | None:
        """Handle Buy, Sell, and Liquidation distribution rows.

        For Liquidation distribution the shares field is empty; in that case
        only the cash leg is emitted and the transaction is flagged '!' for
        manual inspection.
        """
        currency = row[_COL_CURRENCY].strip()
        isin = row[_COL_ISIN].strip()
        symbol = self.isin_lookup.get(isin)
        if symbol is None:
            logger.error(
                f"Could not fetch isin {isin} from supplied ISINs "
                f"{list(self.isin_lookup.keys())}"
            )
            return None

        shares_str = row[_COL_SHARES].strip()
        proceeds = amount.Amount(D(row[_COL_CASHFLOW]), currency)
        asset_name = row[_COL_ASSET_NAME].strip()

        if shares_str:
            quantity = amount.Amount(D(shares_str), symbol)
            price = amount.Amount(D(row[_COL_ASSET_PRICE].strip()), "CHF")
            buy_sell = "BUY" if quantity.number > 0 else "SELL"
            narration = " ".join(
                [buy_sell, quantity.to_string(), "@", price.to_string() + ";", asset_name]
            )
            postings = [
                data.Posting(self.getAssetAccount(symbol), quantity, None, price, None, None),
                data.Posting(
                    self.getLiquidityAccount(currency), proceeds, None, None, None, None
                ),
            ]
            flag = self.flag
        else:
            # Liquidation distribution: shares not reported in CSV
            narration = f"Liquidation distribution {symbol}; {asset_name}"
            postings = [
                data.Posting(
                    self.getLiquidityAccount(currency), proceeds, None, None, None, None
                ),
            ]
            flag = "!"

        return data.Transaction(
            data.new_metadata("Buy", 0),
            self._parse_date(row[_COL_DATE]),
            flag,
            isin,
            narration,
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _handle_deposit(self, row: dict) -> data.Transaction | None:
        if self.ignore_funds_transfers:
            return None
        currency = row[_COL_CURRENCY].strip()
        amount_ = amount.Amount(D(row[_COL_CASHFLOW]), currency)
        postings = [
            data.Posting(self.getLiquidityAccount(currency), amount_, None, None, None, None),
        ]
        meta = data.new_metadata("deposit/withdrawel", 0)
        return data.Transaction(
            meta,
            self._parse_date(row[_COL_DATE]),
            self.flag,
            "self",
            "deposit / withdrawal",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _handle_fee(self, row: dict) -> data.Transaction:
        currency = row[_COL_CURRENCY].strip()
        amount_ = amount.Amount(D(row[_COL_CASHFLOW]), currency)
        postings = [
            data.Posting(self.getFeesAccount(currency), -amount_, None, None, None, None),
            data.Posting(self.getLiquidityAccount(currency), amount_, None, None, None, None),
        ]
        meta = data.new_metadata(__file__, 0, {})
        return data.Transaction(
            meta,
            self._parse_date(row[_COL_DATE]),
            self.flag,
            "FinPension",
            row[_COL_CATEGORY].strip(),
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _handle_interest(self, row: dict) -> data.Transaction:
        currency = row[_COL_CURRENCY].strip()
        amount_ = amount.Amount(D(row[_COL_CASHFLOW]), currency)
        postings = [
            data.Posting(
                self.getInterestIncomeAcconut(currency), -amount_, None, None, None, None
            ),
            data.Posting(self.getLiquidityAccount(currency), amount_, None, None, None, None),
        ]
        meta = data.new_metadata("Interest", 0)
        return data.Transaction(
            meta,
            self._parse_date(row[_COL_DATE]),
            self.flag,
            "FinPension",
            "Interest",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _handle_dividend(self, row: dict) -> data.Transaction | None:
        currency = row[_COL_CURRENCY].strip()
        isin = row[_COL_ISIN].strip()
        symbol = self.isin_lookup.get(isin)
        if symbol is None:
            logger.error(
                f"Could not fetch isin {isin} from supplied ISINs "
                f"{list(self.isin_lookup.keys())}"
            )
            return None
        amount_div = amount.Amount(D(row[_COL_CASHFLOW]), currency)
        postings = [
            data.Posting(
                self.getDivIncomeAcconut(currency, symbol), -amount_div, None, None, None, None
            ),
            data.Posting(self.getLiquidityAccount(currency), amount_div, None, None, None, None),
        ]
        meta = data.new_metadata("dividend", 0, {"isin": isin})
        return data.Transaction(
            meta,
            self._parse_date(row[_COL_DATE]),
            self.flag,
            isin,
            f"Dividend {symbol}; {row[_COL_ASSET_NAME].strip()}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            postings,
        )

    def _make_balances(self, rows: list[dict]) -> list[data.Balance]:
        if not rows:
            return []
        max_date = max(self._parse_date(r[_COL_DATE]) for r in rows)
        last_rows = [r for r in rows if self._parse_date(r[_COL_DATE]) == max_date]
        balances = []
        for row in last_rows:
            balance_str = row[_COL_BALANCE].strip()
            if not balance_str:
                continue
            currency = row[_COL_CURRENCY].strip()
            amt = amount.Amount(D(balance_str), currency)
            meta = data.new_metadata("balance", 0)
            balances.append(
                data.Balance(
                    meta,
                    max_date + timedelta(days=1),
                    self.getLiquidityAccount(currency),
                    amt,
                    None,
                    None,
                )
            )
        return balances

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_rows(self, filepath: str) -> list[dict]:
        with open(filepath, encoding=self.file_encoding) as f:
            reader = csv.DictReader(f, delimiter=";")
            return list(reader)

    def _filter_year(self, rows: list[dict]) -> list[dict]:
        if self.year is None:
            return rows
        return [r for r in rows if self._parse_date(r[_COL_DATE]).year == self.year]

    @staticmethod
    def _parse_date(date_str: str) -> date:
        return date.fromisoformat(date_str.strip())
