"""Tests for FinPensionImporter (finpension.py).

Unit tests use inline CSV strings written to tmp_path — the filename is chosen
to match the default regex so fix_accounts() succeeds.

Integration tests run against the full anonymised fixture:
    tests/fixtures/finpension/finpension_S3a_Portfolio1_fixture.csv
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data

from drnukebean.importer.finpension import FinPensionImporter

# Filename matches the default regex so fix_accounts() and identify() work.
FIXTURE_NAMED = (
    Path(__file__).parent.parent
    / "fixtures"
    / "finpension"
    / "finpension_S3a_Portfolio1_fixture.csv"
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_ROOT = "Assets:Invest:FP:S3a:Portfolio1"

# All ISINs that appear in the fixture
_ISIN_LOOKUP: dict[str, str] = {
    "testisin1": "FUNDA",
    "testisin2": "FUNDB",
    "testisin3": "FUNDC",
    "testisin4": "FUNDD",
    "testisin5": "FUNDE",
    "testisin6": "FUNDF",
}

# Derived account shortcuts (match account helpers)
_CASH = f"{_ROOT}:CHF"
_FEES = f"{_ROOT.replace('Assets', 'Expenses')}:Fees:CHF"
_INCOME_INT = f"{_ROOT.replace('Assets', 'Income')}:Interest:CHF"
_INCOME_DIV_FUNDA = f"{_ROOT.replace('Assets', 'Income')}:FUNDA:Div"
_ASSET_FUNDA = f"{_ROOT}:FUNDA"
_ASSET_FUNDB = f"{_ROOT}:FUNDB"

# Filename that the default regex will match, yielding S3a / Portfolio1
_FNAME = "finpension_S3a_Portfolio1_transactions.csv"

# CSV header row
_HEADER = (
    "Date;Category;Asset Name;ISIN;Number of Shares;"
    "Asset Currency;Currency Rate;Asset Price in CHF;Cash Flow;Balance\n"
)

# ---------------------------------------------------------------------------
# CSV row builders
# ---------------------------------------------------------------------------


def _buy_row(
    date: str = "2025-03-01",
    asset: str = "fundA",
    isin: str = "testisin1",
    shares: str = "10.000000",
    price: str = "15.000000",
    cashflow: str = "-150.000000",
    balance: str = "100.000000",
) -> str:
    return f"{date};Buy;{asset};{isin};{shares};CHF;1.0000000000;{price};{cashflow};{balance}\n"


def _sell_row(
    date: str = "2025-06-01",
    asset: str = "fundA",
    isin: str = "testisin1",
    shares: str = "-5.000000",
    price: str = "20.000000",
    cashflow: str = "100.000000",
    balance: str = "200.000000",
) -> str:
    return f"{date};Sell;{asset};{isin};{shares};CHF;1.0000000000;{price};{cashflow};{balance}\n"


def _deposit_row(
    date: str = "2025-01-01",
    cashflow: str = "500.000000",
    balance: str = "500.000000",
) -> str:
    return f"{date};Deposit;;;;CHF;1.0000000000;;{cashflow};{balance}\n"


def _fee_row(
    date: str = "2025-04-07",
    cashflow: str = "-10.000000",
    balance: str = "90.000000",
    category: str = "Flat-rate administrative fee",
) -> str:
    return f"{date};{category};;;;CHF;1.0000000000;;{cashflow};{balance}\n"


def _interest_row(
    date: str = "2025-04-07",
    cashflow: str = "5.000000",
    balance: str = "95.000000",
) -> str:
    return f"{date};Interests;;;;CHF;1.0000000000;;{cashflow};{balance}\n"


def _dividend_row(
    date: str = "2025-05-01",
    asset: str = "fundA",
    isin: str = "testisin1",
    cashflow: str = "12.000000",
    balance: str = "107.000000",
) -> str:
    return f"{date};Dividend;{asset};{isin};;CHF;1.0000000000;;{cashflow};{balance}\n"


def _liquidation_row(
    date: str = "2025-01-29",
    asset: str = "fundD",
    isin: str = "testisin4",
    cashflow: str = "70.000000",
    balance: str = "170.000000",
) -> str:
    # No shares, no price — as exported by Finpension
    return (
        f"{date};Liquidation distribution;{asset};{isin};;"
        f"CHF;1.0000000000;;{cashflow};{balance}\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _importer(
    root: str = _ROOT,
    year: int | None = None,
    ignore_funds_transfers: bool = False,
) -> FinPensionImporter:
    return FinPensionImporter(
        root_account=root,
        isin_lookup=_ISIN_LOOKUP,
        year=year,
        ignore_funds_transfers=ignore_funds_transfers,
    )


def _write(tmp_path: Path, content: str, name: str = _FNAME) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8-sig")
    return str(p)


# ===========================================================================
# identify()
# ===========================================================================


class TestIdentify:
    def test_matching_filename_accepted(self, tmp_path):
        path = _write(tmp_path, _HEADER)
        assert _importer().identify(path) is True

    def test_pillar2_accepted(self, tmp_path):
        path = _write(tmp_path, _HEADER, "finpension_S2_Portfolio1.csv")
        assert _importer().identify(path) is True

    def test_pillar3a_accepted(self, tmp_path):
        path = _write(tmp_path, _HEADER, "finpension_S3a_Portfolio2.csv")
        assert _importer().identify(path) is True

    def test_case_insensitive(self, tmp_path):
        path = _write(tmp_path, _HEADER, "FINPENSION_s3a_PORTFOLIO1.csv")
        assert _importer().identify(path) is True

    def test_non_finpension_file_rejected(self, tmp_path):
        path = _write(tmp_path, _HEADER, "zkb_transactions.csv")
        assert _importer().identify(path) is False

    def test_missing_portfolio_rejected(self, tmp_path):
        path = _write(tmp_path, _HEADER, "finpension_S3a_only.csv")
        assert _importer().identify(path) is False


# ===========================================================================
# account() and fix_accounts()
# ===========================================================================


class TestAccountDerivation:
    def test_s3a_portfolio1_unchanged(self, tmp_path):
        path = _write(tmp_path, _HEADER, "finpension_S3a_Portfolio1.csv")
        assert _importer().account(path) == "Assets:Invest:FP:S3a:Portfolio1"

    def test_s2_substituted_in_root(self, tmp_path):
        path = _write(tmp_path, _HEADER, "finpension_S2_Portfolio1.csv")
        acc = _importer(root="Assets:Invest:FP:S3a:Portfolio1").account(path)
        assert acc == "Assets:Invest:FP:S2:Portfolio1"

    def test_portfolio_number_substituted(self, tmp_path):
        path = _write(tmp_path, _HEADER, "finpension_S3a_Portfolio2.csv")
        acc = _importer(root="Assets:Invest:FP:S3a:Portfolio1").account(path)
        assert acc == "Assets:Invest:FP:S3a:Portfolio2"

    def test_both_substituted(self, tmp_path):
        path = _write(tmp_path, _HEADER, "finpension_S2_Portfolio3.csv")
        acc = _importer(root="Assets:Invest:FP:S3a:Portfolio1").account(path)
        assert acc == "Assets:Invest:FP:S2:Portfolio3"

    def test_name_property(self):
        assert _importer().name == f"finpension.{_ROOT}"


# ===========================================================================
# date()
# ===========================================================================


class TestDate:
    def test_returns_latest_date_in_file(self, tmp_path):
        content = _HEADER + _buy_row(date="2025-01-01") + _buy_row(date="2025-06-15")
        path = _write(tmp_path, content)
        assert _importer().date(path) == datetime.date(2025, 6, 15)

    def test_respects_year_filter(self, tmp_path):
        content = _HEADER + _buy_row(date="2024-12-31") + _buy_row(date="2025-03-01")
        path = _write(tmp_path, content)
        assert _importer(year=2024).date(path) == datetime.date(2024, 12, 31)

    def test_empty_after_filter_returns_none(self, tmp_path):
        content = _HEADER + _buy_row(date="2024-05-01")
        path = _write(tmp_path, content)
        assert _importer(year=2025).date(path) is None

    def test_empty_body_returns_none(self, tmp_path):
        path = _write(tmp_path, _HEADER)
        assert _importer().date(path) is None


# ===========================================================================
# extract() — Buy
# ===========================================================================


class TestExtractBuy:
    @pytest.fixture
    def txn(self, tmp_path) -> data.Transaction:
        content = _HEADER + _buy_row(
            date="2025-03-01",
            isin="testisin1",
            shares="10.000000",
            price="15.000000",
            cashflow="-150.000000",
        )
        path = _write(tmp_path, content)
        entries = _importer().extract(path, [])
        return next(e for e in entries if isinstance(e, data.Transaction))

    def test_flag_is_cleared(self, txn):
        assert txn.flag == "*"

    def test_date(self, txn):
        assert txn.date == datetime.date(2025, 3, 1)

    def test_payee_is_isin(self, txn):
        assert txn.payee == "testisin1"

    def test_narration_contains_buy_and_symbol(self, txn):
        assert "BUY" in txn.narration
        assert "FUNDA" in txn.narration

    def test_narration_contains_price(self, txn):
        assert "15" in txn.narration

    def test_two_postings(self, txn):
        assert len(txn.postings) == 2

    def test_asset_posting_account(self, txn):
        assert txn.postings[0].account == _ASSET_FUNDA

    def test_asset_posting_units(self, txn):
        assert txn.postings[0].units.number == Decimal("10.000000")
        assert txn.postings[0].units.currency == "FUNDA"

    def test_asset_posting_has_price_annotation(self, txn):
        assert txn.postings[0].price is not None
        assert txn.postings[0].price.number == Decimal("15.000000")
        assert txn.postings[0].price.currency == "CHF"

    def test_asset_posting_no_cost(self, txn):
        assert txn.postings[0].cost is None

    def test_cash_posting_account(self, txn):
        assert txn.postings[1].account == _CASH

    def test_cash_posting_units(self, txn):
        assert txn.postings[1].units.number == Decimal("-150.000000")
        assert txn.postings[1].units.currency == "CHF"


# ===========================================================================
# extract() — Sell
# ===========================================================================


class TestExtractSell:
    @pytest.fixture
    def txn(self, tmp_path) -> data.Transaction:
        content = _HEADER + _sell_row(
            isin="testisin1",
            shares="-5.000000",
            price="20.000000",
            cashflow="100.000000",
        )
        path = _write(tmp_path, content)
        entries = _importer().extract(path, [])
        return next(e for e in entries if isinstance(e, data.Transaction))

    def test_flag_is_cleared(self, txn):
        assert txn.flag == "*"

    def test_narration_contains_sell(self, txn):
        assert "SELL" in txn.narration

    def test_asset_posting_negative_units(self, txn):
        assert txn.postings[0].units.number == Decimal("-5.000000")

    def test_price_annotation_on_asset_posting(self, txn):
        assert txn.postings[0].price.number == Decimal("20.000000")

    def test_cash_posting_positive(self, txn):
        assert txn.postings[1].units.number == Decimal("100.000000")

    def test_no_pnl_posting(self, txn):
        assert len(txn.postings) == 2


# ===========================================================================
# extract() — Portfolio Transaction (pillar 2 trade category)
# ===========================================================================


class TestExtractPortfolioTransaction:
    """'Portfolio Transaction' is the pillar-2 equivalent of Buy/Sell."""

    @pytest.fixture
    def sell_txn(self, tmp_path) -> data.Transaction:
        row = (
            "2025-04-15;Portfolio Transaction;"
            "UBS (CH) Index Fund 3 - Equities World ex CH NSL I-X-acc;"
            "testisin1;-0.393000;CHF;1.0000000000;1500.750000;589.794750;422.380289\n"
        )
        path = _write(tmp_path, _HEADER + row)
        entries = _importer().extract(path, [])
        return next(e for e in entries if isinstance(e, data.Transaction))

    def test_flag_is_cleared(self, sell_txn):
        assert sell_txn.flag == "*"

    def test_narration_contains_sell(self, sell_txn):
        assert "SELL" in sell_txn.narration

    def test_asset_posting_negative_units(self, sell_txn):
        assert sell_txn.postings[0].units.number == Decimal("-0.393000")

    def test_price_annotation(self, sell_txn):
        assert sell_txn.postings[0].price.number == Decimal("1500.750000")

    def test_cash_posting(self, sell_txn):
        assert sell_txn.postings[1].units.number == Decimal("589.794750")

    def test_not_logged_as_unknown(self, tmp_path, caplog):
        row = (
            "2025-04-15;Portfolio Transaction;"
            "UBS (CH) Index Fund 3;testisin1;10.000000;CHF;1.0000000000;100.0;-1000.0;500.0\n"
        )
        path = _write(tmp_path, _HEADER + row)
        with caplog.at_level("WARNING"):
            _importer().extract(path, [])
        assert "unknown category" not in caplog.text


# ===========================================================================
# extract() — Fee
# ===========================================================================


class TestExtractFee:
    @pytest.fixture
    def txn(self, tmp_path) -> data.Transaction:
        content = _HEADER + _fee_row(cashflow="-10.000000")
        path = _write(tmp_path, content)
        entries = _importer().extract(path, [])
        return next(e for e in entries if isinstance(e, data.Transaction))

    def test_payee_is_finpension(self, txn):
        assert txn.payee == "FinPension"

    def test_narration_is_category_string(self, txn):
        assert txn.narration == "Flat-rate administrative fee"

    def test_two_postings(self, txn):
        assert len(txn.postings) == 2

    def test_fees_posting_account(self, txn):
        assert txn.postings[0].account == _FEES

    def test_fees_posting_positive(self, txn):
        # cashflow is -10, so fees posting is -(-10) = +10
        assert txn.postings[0].units.number == Decimal("10.000000")

    def test_cash_posting_negative(self, txn):
        assert txn.postings[1].units.number == Decimal("-10.000000")

    def test_fee_category_variants(self, tmp_path):
        for cat in ("Flat-rate administration fee", "Implementation fees"):
            content = _HEADER + _fee_row(category=cat, cashflow="-5.000000")
            path = _write(tmp_path, content)
            entries = _importer().extract(path, [])
            txns = [e for e in entries if isinstance(e, data.Transaction)]
            assert len(txns) == 1
            assert txns[0].narration == cat


# ===========================================================================
# extract() — Interest
# ===========================================================================


class TestExtractInterest:
    @pytest.fixture
    def txn(self, tmp_path) -> data.Transaction:
        content = _HEADER + _interest_row(cashflow="5.000000")
        path = _write(tmp_path, content)
        entries = _importer().extract(path, [])
        return next(e for e in entries if isinstance(e, data.Transaction))

    def test_payee_is_finpension(self, txn):
        assert txn.payee == "FinPension"

    def test_narration_is_interest(self, txn):
        assert txn.narration == "Interest"

    def test_income_posting_account(self, txn):
        assert txn.postings[0].account == _INCOME_INT

    def test_income_posting_negative(self, txn):
        # cashflow is +5 -> income is -5 (credit)
        assert txn.postings[0].units.number == Decimal("-5.000000")

    def test_cash_posting_positive(self, txn):
        assert txn.postings[1].units.number == Decimal("5.000000")


# ===========================================================================
# extract() — Dividend
# ===========================================================================


class TestExtractDividend:
    @pytest.fixture
    def txn(self, tmp_path) -> data.Transaction:
        content = _HEADER + _dividend_row(isin="testisin1", asset="fundA", cashflow="12.000000")
        path = _write(tmp_path, content)
        entries = _importer().extract(path, [])
        return next(e for e in entries if isinstance(e, data.Transaction))

    def test_payee_is_isin(self, txn):
        assert txn.payee == "testisin1"

    def test_narration_contains_symbol_and_asset(self, txn):
        assert "FUNDA" in txn.narration
        assert "fundA" in txn.narration

    def test_income_posting_account(self, txn):
        assert txn.postings[0].account == _INCOME_DIV_FUNDA

    def test_income_posting_negative(self, txn):
        assert txn.postings[0].units.number == Decimal("-12.000000")

    def test_cash_posting_positive(self, txn):
        assert txn.postings[1].units.number == Decimal("12.000000")

    def test_isin_in_metadata(self, txn):
        assert txn.meta.get("isin") == "testisin1"

    def test_dividend_and_interest_distributions_category(self, tmp_path):
        row = (
            "2025-05-01;Dividend and Interest Distributions;fundA;testisin1;;"
            "CHF;1.0000000000;;8.000000;100.000000\n"
        )
        path = _write(tmp_path, _HEADER + row)
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        assert "FUNDA" in txns[0].narration


# ===========================================================================
# extract() — Deposit
# ===========================================================================


class TestExtractDeposit:
    def test_emitted_by_default(self, tmp_path):
        content = _HEADER + _deposit_row(cashflow="500.000000")
        path = _write(tmp_path, content)
        txns = [
            e
            for e in _importer(ignore_funds_transfers=False).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert len(txns) == 1

    def test_single_cash_posting(self, tmp_path):
        content = _HEADER + _deposit_row(cashflow="500.000000")
        path = _write(tmp_path, content)
        txn = next(
            e
            for e in _importer().extract(path, [])
            if isinstance(e, data.Transaction)
        )
        assert len(txn.postings) == 1
        assert txn.postings[0].account == _CASH
        assert txn.postings[0].units.number == Decimal("500.000000")

    def test_payee_is_self(self, tmp_path):
        content = _HEADER + _deposit_row()
        path = _write(tmp_path, content)
        txn = next(e for e in _importer().extract(path, []) if isinstance(e, data.Transaction))
        assert txn.payee == "self"

    def test_ignored_when_flag_set(self, tmp_path):
        content = _HEADER + _deposit_row()
        path = _write(tmp_path, content)
        txns = [
            e
            for e in _importer(ignore_funds_transfers=True).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert txns == []

    def test_transfer_vested_benefits_also_ignored(self, tmp_path):
        row = "2025-01-01;Transfer vested benefits;;;;CHF;1.0000000000;;1000.00;1000.00\n"
        path = _write(tmp_path, _HEADER + row)
        txns = [
            e
            for e in _importer(ignore_funds_transfers=True).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert txns == []


# ===========================================================================
# extract() — Liquidation distribution
# ===========================================================================


class TestExtractLiquidationDistribution:
    @pytest.fixture
    def txn(self, tmp_path) -> data.Transaction:
        content = _HEADER + _liquidation_row(
            isin="testisin4", asset="fundD", cashflow="70.000000"
        )
        path = _write(tmp_path, content)
        entries = _importer().extract(path, [])
        return next(e for e in entries if isinstance(e, data.Transaction))

    def test_flag_is_exclamation(self, txn):
        assert txn.flag == "!"

    def test_single_cash_posting(self, txn):
        assert len(txn.postings) == 1
        assert txn.postings[0].account == _CASH

    def test_cash_posting_amount(self, txn):
        assert txn.postings[0].units.number == Decimal("70.000000")

    def test_narration_contains_symbol(self, txn):
        assert "FUNDD" in txn.narration

    def test_narration_contains_liquidation(self, txn):
        assert "Liquidation distribution" in txn.narration

    def test_payee_is_isin(self, txn):
        assert txn.payee == "testisin4"


# ===========================================================================
# extract() — Year filter
# ===========================================================================


class TestExtractYearFilter:
    def test_only_matching_year_extracted(self, tmp_path):
        content = (
            _HEADER
            + _buy_row(date="2024-12-31")
            + _buy_row(date="2025-01-15")
            + _buy_row(date="2026-01-01")
        )
        path = _write(tmp_path, content)
        txns = [
            e for e in _importer(year=2025).extract(path, []) if isinstance(e, data.Transaction)
        ]
        assert len(txns) == 1
        assert txns[0].date == datetime.date(2025, 1, 15)

    def test_no_year_filter_extracts_all(self, tmp_path):
        content = (
            _HEADER
            + _buy_row(date="2023-06-01")
            + _buy_row(date="2025-01-01")
        )
        path = _write(tmp_path, content)
        txns = [
            e for e in _importer(year=None).extract(path, []) if isinstance(e, data.Transaction)
        ]
        assert len(txns) == 2

    def test_empty_result_when_year_has_no_rows(self, tmp_path):
        content = _HEADER + _buy_row(date="2024-06-01")
        path = _write(tmp_path, content)
        entries = _importer(year=2025).extract(path, [])
        assert entries == []


# ===========================================================================
# Balance directives
# ===========================================================================


class TestBalanceDirective:
    def test_balance_emitted(self, tmp_path):
        content = _HEADER + _buy_row(date="2025-03-01", balance="250.000000")
        path = _write(tmp_path, content)
        balances = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert len(balances) == 1

    def test_balance_date_is_next_day(self, tmp_path):
        content = _HEADER + _buy_row(date="2025-03-01", balance="250.000000")
        path = _write(tmp_path, content)
        bal = next(e for e in _importer().extract(path, []) if isinstance(e, data.Balance))
        assert bal.date == datetime.date(2025, 3, 2)

    def test_balance_amount(self, tmp_path):
        content = _HEADER + _buy_row(date="2025-03-01", balance="250.000000")
        path = _write(tmp_path, content)
        bal = next(e for e in _importer().extract(path, []) if isinstance(e, data.Balance))
        assert bal.amount.number == Decimal("250.000000")
        assert bal.amount.currency == "CHF"

    def test_balance_account_is_liquidity(self, tmp_path):
        content = _HEADER + _buy_row(date="2025-03-01", balance="250.000000")
        path = _write(tmp_path, content)
        bal = next(e for e in _importer().extract(path, []) if isinstance(e, data.Balance))
        assert bal.account == _CASH

    def test_balance_uses_last_date_only(self, tmp_path):
        # Two rows; only the later one should produce a balance
        content = (
            _HEADER
            + _buy_row(date="2025-01-01", balance="100.000000")
            + _buy_row(date="2025-06-01", balance="200.000000")
        )
        path = _write(tmp_path, content)
        balances = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert len(balances) == 1
        assert balances[0].amount.number == Decimal("200.000000")
        assert balances[0].date == datetime.date(2025, 6, 2)

    def test_balance_respects_year_filter(self, tmp_path):
        # Last row overall is 2026, but year=2025 -> balance from 2025-12-01
        content = (
            _HEADER
            + _buy_row(date="2025-12-01", balance="300.000000")
            + _buy_row(date="2026-01-01", balance="400.000000")
        )
        path = _write(tmp_path, content)
        balances = [
            e for e in _importer(year=2025).extract(path, []) if isinstance(e, data.Balance)
        ]
        assert len(balances) == 1
        assert balances[0].amount.number == Decimal("300.000000")

    def test_no_balance_when_no_rows(self, tmp_path):
        content = _HEADER + _buy_row(date="2024-01-01", balance="100.000000")
        path = _write(tmp_path, content)
        balances = [
            e for e in _importer(year=2025).extract(path, []) if isinstance(e, data.Balance)
        ]
        assert balances == []


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_unknown_category_skipped_no_crash(self, tmp_path):
        row = "2025-01-01;UnknownCategory;;;;CHF;1.0000000000;;10.000000;100.000000\n"
        path = _write(tmp_path, _HEADER + row)
        entries = _importer().extract(path, [])
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert txns == []

    def test_unknown_isin_in_trade_skipped(self, tmp_path):
        row = "2025-01-01;Buy;fundX;UNKNOWN_ISIN;5.0;CHF;1.0;10.0;-50.0;50.0\n"
        path = _write(tmp_path, _HEADER + row)
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns == []

    def test_unknown_isin_in_dividend_skipped(self, tmp_path):
        row = "2025-01-01;Dividend;fundX;UNKNOWN_ISIN;;CHF;1.0;;;10.0;110.0\n"
        path = _write(tmp_path, _HEADER + row)
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns == []

    def test_multiple_rows_on_last_date_each_get_balance(self, tmp_path):
        # Two trades on the same last date -> two balance entries
        content = (
            _HEADER
            + _buy_row(date="2025-06-01", balance="100.000000")
            + _sell_row(date="2025-06-01", balance="150.000000")
        )
        path = _write(tmp_path, content)
        balances = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert len(balances) == 2
        assert all(b.date == datetime.date(2025, 6, 2) for b in balances)

    def test_filename_method_returns_original_name(self, tmp_path):
        path = _write(tmp_path, _HEADER)
        assert _importer().filename(path) == _FNAME


# ===========================================================================
# Integration — full fixture
# ===========================================================================


@pytest.fixture(scope="module")
def _imp() -> FinPensionImporter:
    return FinPensionImporter(
        root_account=_ROOT,
        isin_lookup=_ISIN_LOOKUP,
        year=None,
        ignore_funds_transfers=False,
    )


@pytest.fixture(scope="module")
def _all_entries(_imp) -> list:
    return _imp.extract(str(FIXTURE_NAMED), [])


@pytest.fixture(scope="module")
def _transactions(_all_entries) -> list[data.Transaction]:
    return [e for e in _all_entries if isinstance(e, data.Transaction)]


@pytest.fixture(scope="module")
def _balances(_all_entries) -> list[data.Balance]:
    return [e for e in _all_entries if isinstance(e, data.Balance)]


class TestIntegration:
    def test_identify_fixture_directly(self):
        imp = FinPensionImporter(root_account=_ROOT, isin_lookup=_ISIN_LOOKUP)
        assert imp.identify(str(FIXTURE_NAMED)) is True

    def test_no_unknown_entries(self, _all_entries):
        # Every entry must be a Transaction or Balance — no unexpected types
        for e in _all_entries:
            assert isinstance(e, (data.Transaction, data.Balance))

    def test_exactly_one_balance(self, _balances):
        # Fixture's last date (2026-04-07) has one row -> one balance
        assert len(_balances) == 1

    def test_balance_date_is_day_after_last(self, _balances):
        assert _balances[0].date == datetime.date(2026, 4, 8)

    def test_year_filter_2025_transaction_count(self):
        imp = FinPensionImporter(root_account=_ROOT, isin_lookup=_ISIN_LOOKUP, year=2025)
        entries = imp.extract(str(FIXTURE_NAMED), [])
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        # 39 data rows in 2025 (all categories, deposits included)
        assert len(txns) == 39

    def test_year_filter_2025_ignore_deposits(self):
        imp = FinPensionImporter(
            root_account=_ROOT,
            isin_lookup=_ISIN_LOOKUP,
            year=2025,
            ignore_funds_transfers=True,
        )
        entries = imp.extract(str(FIXTURE_NAMED), [])
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        # 39 rows minus 12 deposits = 27
        assert len(txns) == 27

    def test_year_2025_balance_on_dec25(self):
        imp = FinPensionImporter(root_account=_ROOT, isin_lookup=_ISIN_LOOKUP, year=2025)
        entries = imp.extract(str(FIXTURE_NAMED), [])
        bals = [e for e in entries if isinstance(e, data.Balance)]
        assert len(bals) == 1
        assert bals[0].date == datetime.date(2025, 12, 25)

    def test_liquidation_distribution_flagged(self, _transactions):
        # One liquidation distribution in the fixture (2025-01-29, testisin4)
        liq = [
            t for t in _transactions if "Liquidation distribution" in (t.narration or "")
        ]
        assert len(liq) == 1
        assert liq[0].flag == "!"
        assert len(liq[0].postings) == 1

    def test_buy_postings_have_price_no_cost(self, _transactions):
        buys = [t for t in _transactions if t.narration and "BUY" in t.narration]
        assert len(buys) > 0
        for txn in buys:
            asset_posting = txn.postings[0]
            assert asset_posting.price is not None
            assert asset_posting.cost is None

    def test_sell_two_postings_no_pnl(self, _transactions):
        sells = [t for t in _transactions if t.narration and "SELL" in t.narration]
        assert len(sells) > 0
        for txn in sells:
            assert len(txn.postings) == 2

    def test_dividends_have_isin_in_meta(self, _transactions):
        divs = [t for t in _transactions if t.narration and "Dividend" in t.narration]
        assert len(divs) > 0
        for txn in divs:
            assert "isin" in txn.meta

    def test_fees_narration_is_category_string(self, _transactions):
        fees = [t for t in _transactions if t.payee == "FinPension" and t.narration != "Interest"]
        assert len(fees) > 0
        for txn in fees:
            assert "fee" in txn.narration.lower() or "fees" in txn.narration.lower()

    def test_first_buy_spot_check(self, _transactions):
        # 2021-11-30; Buy; fundA; testisin1; 27.14; CHF; ...; 16.56; -27.93; 91.10
        hit = [
            t
            for t in _transactions
            if t.date == datetime.date(2021, 11, 30) and "FUNDA" in (t.narration or "")
            and "BUY" in (t.narration or "")
        ]
        assert len(hit) == 1
        txn = hit[0]
        assert txn.postings[0].units.number == Decimal("27.140000")
        assert txn.postings[0].units.currency == "FUNDA"
        assert txn.postings[0].price.number == Decimal("16.560000")
        assert txn.postings[1].units.number == Decimal("-27.930000")
        assert txn.postings[1].units.currency == "CHF"
