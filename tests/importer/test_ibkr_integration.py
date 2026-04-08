"""Integration tests: real obfuscated FlexQuery XML through ibflex parser.

_parse_flex_file is NOT mocked here.  These tests exercise the full chain:
  XML file -> ibflex.parser.parse -> IBKRImporter.extract -> beancount entries

Fixture: tests/fixtures/ibkr/flexquery.xml
  Two FlexStatements (two sub-accounts), January 2024:
    Statement 1 (U88776655):
      - APPL BUY 12 @ 153.25 USD
      - EUR.USD forex SELL -850
      - 3 EUR deposits/withdrawals

    Statement 2 (U99887766):
      - EUNK BUY 1230 @ 118.43 EUR
      - MSFT SELL -28 + CLOSED_LOT (opened 2023-12-04)
      - 4x APPL SELL batches (-75, -150, -1100, -540) + matching CLOSED_LOTs
      - APPL BUY 45, APPL BUY 20
      - 2x USD.EUR forex SELL (large + fractional)
      - 2x APPL dividend + WHT pairs (2024-01-23 and 2024-01-24)
      - 3 EUR deposits/withdrawals
      - USD credit interest
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data

from drnukebean.importer.ibkr import IBKRImporter
from tests.conftest import ACCOUNT

FIXTURE = Path(__file__).parent.parent / "fixtures" / "ibkr" / "flexquery.xml"
QUERY_NAME_REAL = "beancount flexquery"

# Account IDs present in flexquery.xml
_ACCOUNT_ID_1 = "U88776655"
_ACCOUNT_ID_2 = "U99887766"
_ACCOUNT_ROOT_1 = "Assets:Invest:IBKR:Trading"
_ACCOUNT_ROOT_2 = "Assets:Invest:IBKR:Pension"


# ---------------------------------------------------------------------------
# Module-scoped fixtures — parse the XML once for all tests
# ---------------------------------------------------------------------------


_DEPOSIT_FROM_REAL = "Assets:Bank:ZKB:EUR"


@pytest.fixture(scope="module")
def real_importer():
    return IBKRImporter(
        account=ACCOUNT,
        query_name=QUERY_NAME_REAL,
        currency="EUR",
        account_map={
            _ACCOUNT_ID_1: {"root": ACCOUNT, "deposit_from": _DEPOSIT_FROM_REAL},
            _ACCOUNT_ID_2: {"root": ACCOUNT, "deposit_from": _DEPOSIT_FROM_REAL},
        },
    )


@pytest.fixture(scope="module")
def entries(real_importer):
    """Parse the real fixture XML once; ibflex lru_cache keeps subsequent calls free."""
    return real_importer.extract(str(FIXTURE), [])


@pytest.fixture(scope="module")
def transactions(entries):
    return [e for e in entries if isinstance(e, data.Transaction)]


@pytest.fixture(scope="module")
def balances(entries):
    return [e for e in entries if isinstance(e, data.Balance)]


# ===========================================================================
# Identify and basic sanity
# ===========================================================================


class TestIdentifyAndSanity:
    def test_identify_matches_real_fixture(self, real_importer):
        assert real_importer.identify(str(FIXTURE)) is True

    def test_extract_produces_entries(self, entries):
        assert len(entries) > 0


# ===========================================================================
# Balances — two statements x two real currencies = 4 directives
# ===========================================================================


class TestBalances:
    def test_four_balance_directives(self, balances):
        # BASE_SUMMARY skipped; stmt1 EUR+USD + stmt2 EUR+USD
        assert len(balances) == 4

    def test_both_currencies_present(self, balances):
        assert {b.amount.currency for b in balances} == {"EUR", "USD"}

    def test_stmt1_eur_balance_amount(self, balances):
        hits = [b for b in balances if b.amount.number == Decimal("31.45")]
        assert len(hits) == 1
        assert hits[0].amount.currency == "EUR"

    def test_stmt2_usd_balance_amount(self, balances):
        # ibflex rounds CashReportCurrency.endingCash to 2 d.p. ("953.274810000" -> 953.27)
        hits = [
            b
            for b in balances
            if b.amount.currency == "USD" and b.amount.number == Decimal("953.27")
        ]
        assert len(hits) == 1

    def test_balance_date_is_day_after_period_end(self, balances):
        # toDate = 2024-01-31 -> balance date = 2024-02-01
        assert all(b.date == datetime.date(2024, 2, 1) for b in balances)


# ===========================================================================
# Stock buy trades
# ===========================================================================


class TestBuyTrades:
    def test_appl_buy_12_stmt1(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 17)
            and any(
                p.account == f"{ACCOUNT}:APPL"
                and p.units is not None
                and p.units.number == Decimal("12")
                for p in t.postings
            )
        ]
        assert len(hits) == 1

    def test_appl_buy_45_stmt2(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 22)
            and any(
                p.account == f"{ACCOUNT}:APPL"
                and p.units is not None
                and p.units.number == Decimal("45")
                for p in t.postings
            )
        ]
        assert len(hits) == 1

    def test_eunk_buy_1230_stmt2(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 15)
            and any(
                p.account == f"{ACCOUNT}:EUNK"
                and p.units is not None
                and p.units.number == Decimal("1230")
                for p in t.postings
            )
        ]
        assert len(hits) == 1

    def test_buy_has_cost_spec(self, transactions):
        # APPL BUY 12: asset posting should carry a CostSpec
        appl_buy = next(
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 17)
            and any(
                p.account == f"{ACCOUNT}:APPL"
                and p.units is not None
                and p.units.number == Decimal("12")
                for p in t.postings
            )
        )
        asset_p = next(p for p in appl_buy.postings if p.account == f"{ACCOUNT}:APPL")
        assert asset_p.cost is not None
        assert asset_p.cost.currency == "USD"


# ===========================================================================
# Stock sell trades
# ===========================================================================


class TestSellTrades:
    def test_msft_sell_present(self, transactions):
        hits = [
            t
            for t in transactions
            if any(
                p.account == f"{ACCOUNT}:MSFT" and p.units is not None and p.units.number < 0
                for p in t.postings
            )
        ]
        assert len(hits) == 1

    def test_msft_sell_lot_open_date(self, transactions):
        # CLOSED_LOT has openDateTime="2023-12-04" -> cost.date should be 2023-12-04
        msft_sell = next(
            t
            for t in transactions
            if any(
                p.account == f"{ACCOUNT}:MSFT" and p.units is not None and p.units.number < 0
                for p in t.postings
            )
        )
        msft_p = next(p for p in msft_sell.postings if p.account == f"{ACCOUNT}:MSFT")
        assert msft_p.cost is not None
        assert msft_p.cost.date == datetime.date(2023, 12, 4)

    def test_four_appl_sell_batches_on_2024_01_08(self, transactions):
        # Four separate SELL executions for APPL on the same day (same order ID)
        appl_sells = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 8)
            and any(
                p.account == f"{ACCOUNT}:APPL" and p.units is not None and p.units.number < 0
                for p in t.postings
            )
        ]
        assert len(appl_sells) == 4

    def test_all_sells_have_pnl_posting(self, transactions):
        sell_txns = [
            t
            for t in transactions
            if any(
                p.account in (f"{ACCOUNT}:MSFT", f"{ACCOUNT}:APPL")
                and p.units is not None
                and p.units.number < 0
                for p in t.postings
            )
        ]
        # 1 MSFT + 4 APPL = 5 sell transactions
        assert len(sell_txns) == 5
        for txn in sell_txns:
            assert any("PnL" in p.account for p in txn.postings), (
                f"Missing PnL posting in sell: {txn}"
            )


# ===========================================================================
# Forex trades
# ===========================================================================


class TestForexTrades:
    def test_eurusd_forex_stmt1(self, transactions):
        # EUR.USD SELL on 2024-01-17 -> postings in EUR and USD accounts
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 17)
            and any(p.account == f"{ACCOUNT}:EUR" for p in t.postings)
            and any(p.account == f"{ACCOUNT}:USD" for p in t.postings)
        ]
        assert len(hits) == 1

    def test_usdeur_large_forex_stmt2(self, transactions):
        # USD.EUR SELL -285000 on 2024-01-08
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 8)
            and any(p.account == f"{ACCOUNT}:USD" for p in t.postings)
            and any(p.account == f"{ACCOUNT}:EUR" for p in t.postings)
        ]
        assert len(hits) >= 1

    def test_usdeur_fractional_forex_stmt2(self, transactions):
        # USD.EUR SELL -0.38754321: tradeDate="2024-01-11", dateTime="2024-01-10;22:01:20"
        # Importer uses tradeDate, not dateTime, so transaction date is 2024-01-11
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 11)
            and any(p.account == f"{ACCOUNT}:USD" for p in t.postings)
            and any(p.account == f"{ACCOUNT}:EUR" for p in t.postings)
        ]
        assert len(hits) == 1


# ===========================================================================
# Dividends + WHT
# ===========================================================================


class TestDividends:
    def test_two_dividend_transactions(self, transactions):
        # One pair per reportDate: 2024-01-23 and 2024-01-24
        div_txns = [t for t in transactions if "Dividend" in t.narration]
        assert len(div_txns) == 2

    def test_dividend_dates(self, transactions):
        div_dates = {t.date for t in transactions if "Dividend" in t.narration}
        assert datetime.date(2024, 1, 23) in div_dates
        assert datetime.date(2024, 1, 24) in div_dates

    def test_each_dividend_has_three_postings(self, transactions):
        # income + WHT + liquidity = 3 legs per paired div+wht
        div_txns = [t for t in transactions if "Dividend" in t.narration]
        for txn in div_txns:
            assert len(txn.postings) == 3

    def test_dividend_wht_posting_present(self, transactions):
        div_txns = [t for t in transactions if "Dividend" in t.narration]
        for txn in div_txns:
            wht_postings = [p for p in txn.postings if "WHT" in p.account]
            assert len(wht_postings) == 1

    def test_dividend_income_account_contains_symbol(self, transactions):
        div_txns = [t for t in transactions if "Dividend" in t.narration]
        for txn in div_txns:
            income_postings = [p for p in txn.postings if "Income" in p.account]
            assert len(income_postings) == 1
            assert "APPL" in income_postings[0].account


# ===========================================================================
# Interest and deposits
# ===========================================================================


class TestInterestAndDeposits:
    def test_interest_on_2024_01_31(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 31)
            and any("Income" in p.account for p in t.postings)
        ]
        assert len(hits) >= 1

    def test_six_deposit_transactions(self, transactions):
        # stmt1: +650, +175, -125 EUR
        # stmt2: -231564.75, +125, +187234.56 EUR
        deposit_txns = [
            t for t in transactions if any(p.account == "Assets:Bank:ZKB:EUR" for p in t.postings)
        ]
        assert len(deposit_txns) == 6

    def test_withdrawal_produces_negative_liquidity(self, transactions):
        # stmt1: -125 EUR on 2024-01-23
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 23)
            and any(
                p.account == f"{ACCOUNT}:EUR"
                and p.units is not None
                and p.units.number == Decimal("-125")
                for p in t.postings
            )
        ]
        assert len(hits) == 1


# ===========================================================================
# account_map: two statements routed to distinct account roots
# ===========================================================================


@pytest.fixture(scope="module")
def mapped_importer():
    return IBKRImporter(
        account=ACCOUNT,
        query_name=QUERY_NAME_REAL,
        currency="EUR",
        account_map={
            _ACCOUNT_ID_1: {"root": _ACCOUNT_ROOT_1, "deposit_from": _DEPOSIT_FROM_REAL},
            _ACCOUNT_ID_2: {"root": _ACCOUNT_ROOT_2, "deposit_from": _DEPOSIT_FROM_REAL},
        },
    )


@pytest.fixture(scope="module")
def mapped_entries(mapped_importer):
    return mapped_importer.extract(str(FIXTURE), [])


@pytest.fixture(scope="module")
def mapped_transactions(mapped_entries):
    return [e for e in mapped_entries if isinstance(e, data.Transaction)]


@pytest.fixture(scope="module")
def mapped_balances(mapped_entries):
    return [e for e in mapped_entries if isinstance(e, data.Balance)]


class TestAccountMap:
    def test_no_entry_uses_default_account_root(self, mapped_entries):
        # All postings should reference _ACCOUNT_ROOT_1 or _ACCOUNT_ROOT_2, never
        # the plain ACCOUNT root.
        all_accounts = {
            p.account
            for e in mapped_entries
            if isinstance(e, (data.Transaction, data.Balance))
            for p in (e.postings if isinstance(e, data.Transaction) else [])
        }
        # Balance accounts
        all_accounts |= {e.account for e in mapped_entries if isinstance(e, data.Balance)}
        ibkr_accounts = {a for a in all_accounts if "IBKR" in a}
        assert not any(
            a.startswith(f"{ACCOUNT}:")
            and not (a.startswith(_ACCOUNT_ROOT_1) or a.startswith(_ACCOUNT_ROOT_2))
            for a in ibkr_accounts
        )

    def test_stmt1_appl_buy_routes_to_trading(self, mapped_transactions):
        # APPL BUY 12 on 2024-01-17 belongs to U88776655 -> _ACCOUNT_ROOT_1
        hits = [
            t
            for t in mapped_transactions
            if t.date == datetime.date(2024, 1, 17)
            and any(
                p.account == f"{_ACCOUNT_ROOT_1}:APPL"
                and p.units is not None
                and p.units.number == Decimal("12")
                for p in t.postings
            )
        ]
        assert len(hits) == 1

    def test_stmt2_eunk_buy_routes_to_pension(self, mapped_transactions):
        # EUNK BUY 1230 on 2024-01-15 belongs to U99887766 -> _ACCOUNT_ROOT_2
        hits = [
            t
            for t in mapped_transactions
            if t.date == datetime.date(2024, 1, 15)
            and any(
                p.account == f"{_ACCOUNT_ROOT_2}:EUNK"
                and p.units is not None
                and p.units.number == Decimal("1230")
                for p in t.postings
            )
        ]
        assert len(hits) == 1

    def test_balances_use_correct_roots(self, mapped_balances):
        roots = {b.account.rsplit(":", 1)[0] for b in mapped_balances}
        assert _ACCOUNT_ROOT_1 in roots
        assert _ACCOUNT_ROOT_2 in roots
        assert ACCOUNT not in roots

    def test_total_entry_count_unchanged(self, entries, mapped_entries):
        # account_map affects routing only; total entry count must be the same
        assert len(mapped_entries) == len(entries)
