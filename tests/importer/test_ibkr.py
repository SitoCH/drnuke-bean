"""Unit tests for drnukebean.importer.ibkr (pure parsing, no network).

All tests use synthetic ibflex Types objects constructed directly — no real
FlexQuery XML is parsed here.  The module-level cached parser function
(_parse_flex_file) is mocked via pytest-mock so that extract() receives the
pre-built FlexQueryResponse without touching the filesystem parser.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data, inventory, position
from beancount.parser import printer
from ibflex.enums import CashAction
from loguru import logger

import drnukebean.importer.ibkr as ibkr_module
from drnukebean.importer.ibkr import IBKRImporter, _forex_currencies, _is_forex, _wht_div_type
from tests.conftest import (
    ACCOUNT,
    QUERY_NAME,
    REPORT_DATE,
    TRADE_DATE,
    make_buy_trade,
    make_cash_report_row,
    make_closed_lot,
    make_deposit,
    make_dividend,
    make_fee,
    make_forex_trade,
    make_interest,
    make_response,
    make_roc,
    make_sell_trade,
    make_wht,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ibkr"


# ---------------------------------------------------------------------------
# Helper to call extract() with a mocked _parse_flex_file
# ---------------------------------------------------------------------------


def _extract(importer: IBKRImporter, response, mocker, tmp_path: Path) -> list:
    """Patch _parse_flex_file and call importer.extract()."""
    xml_file = tmp_path / "test.xml"
    xml_file.write_text("<FlexQueryResponse/>")
    mocker.patch.object(ibkr_module, "_parse_flex_file", return_value=response)
    return importer.extract(str(xml_file), [])


# ===========================================================================
# identify()
# ===========================================================================


class TestIdentify:
    def test_matches_correct_queryname(self, importer):
        path = str(FIXTURES / "valid_flexquery.xml")
        assert importer.identify(path) is True

    def test_rejects_wrong_queryname(self, importer):
        path = str(FIXTURES / "wrong_queryname.xml")
        assert importer.identify(path) is False

    def test_matches_any_flexquery_when_no_queryname_set(self, importer_no_queryname):
        # wrong_queryname.xml is still a valid FlexQueryResponse
        path = str(FIXTURES / "wrong_queryname.xml")
        assert importer_no_queryname.identify(path) is True

    def test_rejects_non_flexquery_xml(self, importer):
        path = str(FIXTURES / "not_flexquery.xml")
        assert importer.identify(path) is False

    def test_rejects_non_xml_file(self, importer, tmp_path):
        p = tmp_path / "data.txt"
        p.write_text("not xml")
        assert importer.identify(str(p)) is False


# ===========================================================================
# account() and date()
# ===========================================================================


class TestAccountAndDate:
    def test_account_returns_configured_account(self, importer, tmp_path):
        xml = tmp_path / "f.xml"
        xml.write_text("<FlexQueryResponse/>")
        assert importer.account(str(xml)) == ACCOUNT

    def test_date_returns_report_close_date(self, importer, mocker, tmp_path):
        response = make_response(cash_report=(make_cash_report_row(),))
        xml = tmp_path / "f.xml"
        xml.write_text("<FlexQueryResponse/>")
        mocker.patch.object(ibkr_module, "_parse_flex_file", return_value=response)
        assert importer.date(str(xml)) == REPORT_DATE

    def test_date_skips_base_summary_row(self, importer, mocker, tmp_path):
        from ibflex.Types import CashReportCurrency

        summary = CashReportCurrency(currency="BASE_SUMMARY", toDate=datetime.date(2024, 2, 1))
        real = make_cash_report_row(to_date=REPORT_DATE)
        response = make_response(cash_report=(summary, real))
        xml = tmp_path / "f.xml"
        xml.write_text("<FlexQueryResponse/>")
        mocker.patch.object(ibkr_module, "_parse_flex_file", return_value=response)
        assert importer.date(str(xml)) == REPORT_DATE


# ===========================================================================
# Balances
# ===========================================================================


class TestBalances:
    def test_single_currency_balance(self, importer, mocker, tmp_path):
        response = make_response(cash_report=(make_cash_report_row("CHF", "10000.00"),))
        entries = _extract(importer, response, mocker, tmp_path)

        balances = [e for e in entries if isinstance(e, data.Balance)]
        assert len(balances) == 1
        bal = balances[0]
        assert bal.account == f"{ACCOUNT}:CHF"
        assert bal.amount.number == Decimal("10000.00")
        assert bal.amount.currency == "CHF"
        # Balance date is toDate + 1 day
        assert bal.date == REPORT_DATE + datetime.timedelta(days=1)

    def test_base_summary_row_skipped(self, importer, mocker, tmp_path):
        from ibflex.Types import CashReportCurrency

        summary = CashReportCurrency(
            currency="BASE_SUMMARY", endingCash=Decimal("99.00"), toDate=REPORT_DATE
        )
        real = make_cash_report_row("CHF", "10000.00")
        response = make_response(cash_report=(summary, real))
        entries = _extract(importer, response, mocker, tmp_path)

        balances = [e for e in entries if isinstance(e, data.Balance)]
        assert len(balances) == 1
        assert balances[0].amount.currency == "CHF"

    def test_multi_currency_balances(self, importer, mocker, tmp_path):
        response = make_response(
            cash_report=(
                make_cash_report_row("CHF", "10000.00"),
                make_cash_report_row("USD", "5000.00"),
            )
        )
        entries = _extract(importer, response, mocker, tmp_path)
        balances = [e for e in entries if isinstance(e, data.Balance)]
        currencies = {b.amount.currency for b in balances}
        assert currencies == {"CHF", "USD"}


# ===========================================================================
# Stock trades — buy
# ===========================================================================


class TestBuyTrade:
    def test_buy_produces_transaction(self, importer, mocker, tmp_path):
        response = make_response(trades=(make_buy_trade(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1

    def test_buy_date_is_datetime_date(self, importer, mocker, tmp_path):
        response = make_response(trades=(make_buy_trade(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert isinstance(txn.date, datetime.date)
        assert txn.date == TRADE_DATE

    def test_buy_narration_contains_buy_and_price(self, importer, mocker, tmp_path):
        response = make_response(trades=(make_buy_trade(trade_price="100.00"),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert txn.narration is not None
        assert "BUY" in txn.narration
        assert "100" in txn.narration

    def test_buy_asset_posting_has_cost_spec(self, importer, mocker, tmp_path):
        # importer fixture has no transactionID_labeled_since: unlabeled BUYs
        # keep the asserted raw-price basis
        response = make_response(trades=(make_buy_trade(symbol="VT", currency="USD"),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        asset_postings = [p for p in txn.postings if p.account == f"{ACCOUNT}:VT"]
        assert len(asset_postings) == 1
        cost = asset_postings[0].cost
        assert isinstance(cost, position.CostSpec)
        assert cost.number_per == Decimal("100.00")
        assert cost.currency == "USD"
        assert cost.date == TRADE_DATE
        assert cost.label is None

    def test_buy_four_postings(self, importer, mocker, tmp_path):
        # asset, liquidity (proceeds), liquidity (commission), fees
        response = make_response(trades=(make_buy_trade(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert len(txn.postings) == 4

    def test_buy_symbol_remap_applied(self, mocker, tmp_path):
        imp = IBKRImporter(account=ACCOUNT, query_name=QUERY_NAME, symbol_map={"VT": "VT3"})
        response = make_response(trades=(make_buy_trade(symbol="VT"),))
        entries = _extract(imp, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        accounts = [p.account for p in txn.postings]
        assert any("VT3" in a for a in accounts)
        assert not any(a.endswith(":VT") for a in accounts)

    def test_buy_exchange_suffix_stripped(self, importer, mocker, tmp_path):
        response = make_response(trades=(make_buy_trade(symbol="VWRL.BATS"),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        accounts = [p.account for p in txn.postings]
        assert any("VWRL" in a for a in accounts)
        assert not any("BATS" in a for a in accounts)


# ===========================================================================
# Stock trades — sell
# ===========================================================================


class TestSellTrade:
    def test_sell_with_matching_lot(self, importer, mocker, tmp_path):
        sell = make_sell_trade(symbol="VT", quantity="-5", proceeds="549.50")
        lot = make_closed_lot(symbol="VT", quantity="-5")
        response = make_response(trades=(sell, lot))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1

    def test_sell_narration_contains_sell(self, importer, mocker, tmp_path):
        sell = make_sell_trade()
        lot = make_closed_lot()
        response = make_response(trades=(sell, lot))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert txn.narration is not None
        assert "SELL" in txn.narration

    def test_sell_lot_posting_has_cost_spec_with_open_date(self, importer, mocker, tmp_path):
        sell = make_sell_trade(symbol="VT", quantity="-5")
        lot = make_closed_lot(
            symbol="VT", quantity="5", open_date_time=datetime.datetime(2023, 6, 1, 9, 0)
        )
        response = make_response(trades=(sell, lot))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        # Find the asset lot posting (negative quantity of the symbol)
        lot_postings = [
            p
            for p in txn.postings
            if p.account == f"{ACCOUNT}:VT"
            and p.units is not None
            and p.units.number is not None
            and p.units.number < 0
        ]
        assert len(lot_postings) == 1
        cost = lot_postings[0].cost
        assert isinstance(cost, position.CostSpec)
        assert cost.date == datetime.date(2023, 6, 1)
        # the reducing side never asserts a basis number
        assert cost.number_per is None

    def test_sell_has_pnl_posting(self, importer, mocker, tmp_path):
        sell = make_sell_trade(symbol="VT", quantity="-5")
        lot = make_closed_lot(symbol="VT", quantity="-5")
        response = make_response(trades=(sell, lot))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        pnl_accounts = [p.account for p in txn.postings if "PnL" in p.account]
        assert len(pnl_accounts) == 1

    def test_multi_lot_sell_distinct_labels(self, mocker, tmp_path):
        imp = IBKRImporter(
            account=ACCOUNT,
            query_name=QUERY_NAME,
            transactionID_labeled_since=datetime.date(2023, 1, 1),
        )
        sell = make_sell_trade(symbol="VT", quantity="-10", proceeds="1099.00")
        lot1 = make_closed_lot(symbol="VT", quantity="5", transaction_id="784510001")
        lot2 = make_closed_lot(symbol="VT", quantity="5", transaction_id="784510002")
        response = make_response(trades=(sell, lot1, lot2))
        entries = _extract(imp, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        lot_postings = [
            p
            for p in txn.postings
            if p.account == f"{ACCOUNT}:VT"
            and p.units is not None
            and p.units.number is not None
            and p.units.number < 0
        ]
        assert len(lot_postings) == 2
        labels = set()
        for p in lot_postings:
            assert isinstance(p.cost, position.CostSpec)
            labels.add(p.cost.label)
        assert labels == {"784510001", "784510002"}

    def test_labeled_output_books_correct_lot(self, mocker, tmp_path):
        """cost_spec.bean NVDA scenario: two same-day lots, the labeled SELL
        must book the intended lot under FIFO, not the FIFO-tiebreak one."""
        imp = IBKRImporter(
            account=ACCOUNT,
            query_name=QUERY_NAME,
            account_map={"U1234567": {"root": ACCOUNT}},
            transactionID_labeled_since=datetime.date(2028, 1, 1),
        )
        buy1 = make_buy_trade(
            symbol="NVDA",
            quantity="10",
            trade_price="100.00",
            proceeds="-1000.00",
            commission="-5.00",
            trade_date=datetime.date(2028, 6, 15),
            date_time=datetime.datetime(2028, 6, 15, 9, 30),
            transaction_id="784510001",
        )
        buy2 = make_buy_trade(
            symbol="NVDA",
            quantity="10",
            trade_price="110.00",
            proceeds="-1100.00",
            commission="-5.00",
            trade_date=datetime.date(2028, 6, 15),
            date_time=datetime.datetime(2028, 6, 15, 14, 15),
            transaction_id="784510002",
        )
        sell = make_sell_trade(
            symbol="NVDA",
            quantity="-10",
            trade_price="200.00",
            proceeds="2000.00",
            commission="-5.00",
            trade_date=datetime.date(2028, 7, 15),
            date_time=datetime.datetime(2028, 7, 15, 10, 0),
        )
        # IBKR reports lot 2 (the 110.00 one) as closed
        lot = make_closed_lot(
            symbol="NVDA",
            quantity="10",
            trade_price="110.00",
            open_date_time=datetime.datetime(2028, 6, 15, 14, 15),
            transaction_id="784510002",
        )
        response = make_response(trades=(buy1, buy2, sell, lot))
        entries = _extract(imp, response, mocker, tmp_path)
        assert len(entries) == 3

        opens = "\n".join(
            [
                f'2028-01-01 open {ACCOUNT}:NVDA NVDA "FIFO"',
                f"2028-01-01 open {ACCOUNT}:USD",
                "2028-01-01 open Income:Invest:IBKR:NVDA:PnL",
                "2028-01-01 open Expenses:Invest:IBKR:Fees:USD",
            ]
        )
        text = opens + "\n\n" + "\n".join(printer.format_entry(e) for e in entries)
        loaded, errors, _ = loader.load_string(text)
        assert errors == [], "Load errors:\n" + "\n".join(str(e) for e in errors)

        sell_txn = next(
            e
            for e in loaded
            if isinstance(e, data.Transaction) and e.date == datetime.date(2028, 7, 15)
        )
        pnl = next(p for p in sell_txn.postings if "PnL" in p.account)
        assert pnl.units is not None
        assert pnl.units.number == Decimal("-900.00")

        # lot 1 (basis 100.00, label 784510001) must remain open and untouched
        inv = inventory.Inventory()
        for e in loaded:
            if isinstance(e, data.Transaction):
                for p in e.postings:
                    if p.account == f"{ACCOUNT}:NVDA":
                        # after booking every posting has units and a resolved Cost
                        assert p.units is not None
                        assert not isinstance(p.cost, position.CostSpec)
                        inv.add_amount(p.units, p.cost)
        positions = inv.get_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.units.number == Decimal("10")
        assert pos.units.currency == "NVDA"
        assert pos.cost is not None
        assert pos.cost.number == Decimal("100.00")
        assert pos.cost.label == "784510001"


# ===========================================================================
# CostSpec labeling (transactionID_labeled_since)
# ===========================================================================


class TestLabeledSinceConfig:
    def test_date_stored_unchanged(self):
        threshold = datetime.date(2024, 1, 1)
        imp = IBKRImporter(account=ACCOUNT, transactionID_labeled_since=threshold)
        assert imp._labeled_since == threshold

    def test_invalid_string_raises_type_error(self):
        with pytest.raises(TypeError):
            IBKRImporter(
                account=ACCOUNT,
                transactionID_labeled_since="01.02.2024",  # pyright: ignore[reportArgumentType]
            )

    def test_datetime_raises_type_error(self):
        with pytest.raises(TypeError):
            IBKRImporter(
                account=ACCOUNT,
                transactionID_labeled_since=datetime.datetime(2024, 1, 1),
            )

    def test_unset_warns_at_init(self):
        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            IBKRImporter(account=ACCOUNT)
        finally:
            logger.remove(sink_id)
        assert any("transactionID_labeled_since" in m for m in messages)

    def test_set_does_not_warn_at_init(self):
        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            IBKRImporter(account=ACCOUNT, transactionID_labeled_since=datetime.date(2024, 1, 1))
        finally:
            logger.remove(sink_id)
        assert not any("transactionID_labeled_since" in m for m in messages)


class TestCostSpecLabeling:
    """Behavior matrix: threshold unset / acquisition date >= threshold /
    acquisition date < threshold, each with and without a transactionID."""

    # BUY acquisition date is TRADE_DATE = 2024-01-15
    @pytest.mark.parametrize(
        "labeled_since,transaction_id,exp_label,exp_meta_id,exp_number_per",
        [
            (None, "784510001", None, "784510001", Decimal("100.00")),
            (None, None, None, None, Decimal("100.00")),
            (datetime.date(2024, 1, 1), "784510001", "784510001", None, None),
            (datetime.date(2024, 1, 1), None, None, None, Decimal("100.00")),
            (datetime.date(2024, 2, 1), "784510001", None, "784510001", Decimal("100.00")),
            (datetime.date(2024, 2, 1), None, None, None, Decimal("100.00")),
        ],
    )
    def test_buy_matrix(
        self,
        mocker,
        tmp_path,
        labeled_since,
        transaction_id,
        exp_label,
        exp_meta_id,
        exp_number_per,
    ):
        imp = IBKRImporter(
            account=ACCOUNT, query_name=QUERY_NAME, transactionID_labeled_since=labeled_since
        )
        response = make_response(trades=(make_buy_trade(transaction_id=transaction_id),))
        entries = _extract(imp, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        posting = next(p for p in txn.postings if p.account == f"{ACCOUNT}:VT")
        cost = posting.cost
        assert isinstance(cost, position.CostSpec)
        assert cost.label == exp_label
        assert cost.number_per == exp_number_per
        assert cost.currency == "USD"
        assert cost.date == TRADE_DATE
        assert (posting.meta or {}).get("transactionID") == exp_meta_id

    # SELL lot acquisition date is OPEN_DT.date() = 2023-06-01
    @pytest.mark.parametrize(
        "labeled_since,transaction_id,exp_label,exp_meta_id",
        [
            (None, "784510001", None, "784510001"),
            (None, None, None, None),
            (datetime.date(2023, 1, 1), "784510001", "784510001", None),
            (datetime.date(2023, 1, 1), None, None, None),
            (datetime.date(2024, 1, 1), "784510001", None, "784510001"),
            (datetime.date(2024, 1, 1), None, None, None),
        ],
    )
    def test_sell_matrix(
        self, mocker, tmp_path, labeled_since, transaction_id, exp_label, exp_meta_id
    ):
        imp = IBKRImporter(
            account=ACCOUNT, query_name=QUERY_NAME, transactionID_labeled_since=labeled_since
        )
        sell = make_sell_trade(symbol="VT", quantity="-5")
        lot = make_closed_lot(symbol="VT", quantity="5", transaction_id=transaction_id)
        response = make_response(trades=(sell, lot))
        entries = _extract(imp, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        posting = next(
            p
            for p in txn.postings
            if p.account == f"{ACCOUNT}:VT"
            and p.units is not None
            and p.units.number is not None
            and p.units.number < 0
        )
        cost = posting.cost
        assert isinstance(cost, position.CostSpec)
        assert cost.label == exp_label
        # the reducing side never asserts a basis number
        assert cost.number_per is None
        assert cost.date == datetime.date(2023, 6, 1)
        assert (posting.meta or {}).get("transactionID") == exp_meta_id

    def test_post_threshold_missing_transaction_id_warns(self, mocker, tmp_path):
        imp = IBKRImporter(
            account=ACCOUNT,
            query_name=QUERY_NAME,
            transactionID_labeled_since=datetime.date(2024, 1, 1),
        )
        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            response = make_response(trades=(make_buy_trade(transaction_id=None),))
            entries = _extract(imp, response, mocker, tmp_path)
        finally:
            logger.remove(sink_id)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        posting = next(p for p in txn.postings if p.account == f"{ACCOUNT}:VT")
        cost = posting.cost
        assert isinstance(cost, position.CostSpec)
        assert cost.label is None
        assert cost.number_per == Decimal("100.00")
        assert any("no transactionID" in m for m in messages)


# ===========================================================================
# Forex trades
# ===========================================================================


class TestForexTrade:
    def test_forex_produces_transaction(self, importer, mocker, tmp_path):
        response = make_response(trades=(make_forex_trade(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1

    def test_forex_has_two_liquidity_postings(self, importer, mocker, tmp_path):
        # USD.CHF: postings for USD, CHF, and commission legs
        response = make_response(trades=(make_forex_trade(symbol="USD.CHF"),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        liq_postings = [p for p in txn.postings if p.account.startswith(f"{ACCOUNT}:")]
        currencies = {p.account.split(":")[-1] for p in liq_postings}
        assert "USD" in currencies
        assert "CHF" in currencies

    def test_forex_narration_contains_buysell_and_quantity(self, importer, mocker, tmp_path):
        response = make_response(trades=(make_forex_trade(symbol="USD.CHF"),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        # Should mention the primary currency amount
        assert txn.narration is not None
        assert "USD" in txn.narration or "1000" in txn.narration


# ===========================================================================
# Dividends
# ===========================================================================


class TestDividends:
    def test_dividend_without_wht(self, importer, mocker, tmp_path):
        response = make_response(cash_transactions=(make_dividend(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        txn = txns[0]
        assert txn.narration is not None
        assert "Dividend" in txn.narration

    def test_dividend_with_wht_three_postings(self, importer, mocker, tmp_path):
        # div + wht: income posting, WHT posting, liquidity posting = 3 legs
        div = make_dividend(symbol="VT", currency="USD", amount="87.00")
        wht = make_wht(symbol="VT", currency="USD", amount="-13.05")
        response = make_response(cash_transactions=(div, wht))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        assert len(txns[0].postings) == 3

    def test_dividend_wht_account_contains_symbol(self, importer, mocker, tmp_path):
        div = make_dividend(symbol="VT", currency="USD", amount="87.00")
        wht = make_wht(symbol="VT", currency="USD", amount="-13.05")
        response = make_response(cash_transactions=(div, wht))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        wht_postings = [p for p in txn.postings if "WHT" in p.account]
        assert len(wht_postings) == 1
        assert "VT" in wht_postings[0].account

    def test_dividend_isin_in_metadata(self, importer, mocker, tmp_path):
        div = make_dividend(description="VT (US9229083632) CASH DIVIDEND USD 0.87 PER SHARE")
        response = make_response(cash_transactions=(div,))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert txn.meta.get("isin") == "US9229083632"

    def test_dividend_per_share_in_metadata(self, importer, mocker, tmp_path):
        div = make_dividend(description="VT (US9229083632) CASH DIVIDEND USD 0.8700 PER SHARE")
        response = make_response(cash_transactions=(div,))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert txn.meta.get("per_share") == "0.8700"

    def test_dividend_income_account_derived_from_root(self, importer, mocker, tmp_path):
        div = make_dividend(symbol="VT", currency="USD")
        response = make_response(cash_transactions=(div,))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        income_postings = [p for p in txn.postings if "Income" in p.account]
        assert len(income_postings) == 1
        assert "VT" in income_postings[0].account

    def test_dividend_explicit_div_account(self, mocker, tmp_path):
        imp = IBKRImporter(
            account=ACCOUNT, query_name=QUERY_NAME, div_account="Income:Dividends:All"
        )
        div = make_dividend(symbol="VT")
        response = make_response(cash_transactions=(div,))
        entries = _extract(imp, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        income_postings = [p for p in txn.postings if p.account == "Income:Dividends:All"]
        assert len(income_postings) == 1

    def test_return_of_capital(self, importer, mocker, tmp_path):
        roc = make_roc(symbol="VT")
        response = make_response(cash_transactions=(roc,))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        # ROC goes through dividend path — just verify no crash and metadata
        assert txns[0].meta.get("isin") == "Return of Capital"


# ===========================================================================
# Fees
# ===========================================================================


class TestFees:
    def test_fee_produces_transaction(self, importer, mocker, tmp_path):
        response = make_response(cash_transactions=(make_fee(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1

    def test_fee_narration_contains_month(self, importer, mocker, tmp_path):
        fee = make_fee(description="Minimum Fee Jan 2024")
        response = make_response(cash_transactions=(fee,))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert txn.narration is not None
        assert "Jan 2024" in txn.narration

    def test_fee_has_expense_and_liquidity_postings(self, importer, mocker, tmp_path):
        response = make_response(cash_transactions=(make_fee(currency="CHF"),))
        entries = _extract(importer, response, mocker, tmp_path)
        txn = next(e for e in entries if isinstance(e, data.Transaction))
        assert len(txn.postings) == 2
        expense_p = [p for p in txn.postings if "Expenses" in p.account]
        assert len(expense_p) == 1
        assert "CHF" in expense_p[0].account


# ===========================================================================
# Interest
# ===========================================================================


class TestInterest:
    def test_interest_received(self, importer, mocker, tmp_path):
        response = make_response(cash_transactions=(make_interest(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        income_postings = [p for p in txns[0].postings if "Income" in p.account]
        assert len(income_postings) == 1


# ===========================================================================
# Deposits
# ===========================================================================


class TestDeposits:
    def test_deposit_with_deposit_from_emits_two_legs(self, importer, mocker, tmp_path):
        # importer fixture has deposit_from='Assets:Bank:ZKB:CHF' for U1234567
        response = make_response(cash_transactions=(make_deposit(),))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        assert txns[0].flag == "*"
        assert len(txns[0].postings) == 2
        deposit_postings = [p for p in txns[0].postings if p.account == "Assets:Bank:ZKB:CHF"]
        assert len(deposit_postings) == 1

    def test_deposit_without_deposit_from_emits_single_leg_with_flag(self, mocker, tmp_path):
        imp = IBKRImporter(
            account=ACCOUNT,
            query_name=QUERY_NAME,
            account_map={"U1234567": {"root": ACCOUNT}},
        )
        response = make_response(cash_transactions=(make_deposit(),))
        entries = _extract(imp, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        assert txns[0].flag == "!"
        assert len(txns[0].postings) == 1
        assert txns[0].postings[0].account == f"{ACCOUNT}:CHF"

    def test_deposit_no_map_emits_single_leg_with_flag(self, mocker, tmp_path):
        # single-account mode (no account_map): deposit_from is always None
        imp = IBKRImporter(account=ACCOUNT, query_name=QUERY_NAME)
        response = make_response(cash_transactions=(make_deposit(),))
        entries = _extract(imp, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        assert txns[0].flag == "!"
        assert len(txns[0].postings) == 1


# ===========================================================================
# Account name helpers
# ===========================================================================


class TestAccountHelpers:
    def test_liquidity_account(self, importer):
        assert importer._liquidity_account("USD", ACCOUNT) == f"{ACCOUNT}:USD"

    def test_asset_account(self, importer):
        assert importer._asset_account("VT", ACCOUNT) == f"{ACCOUNT}:VT"

    def test_wht_account_default_derivation(self, importer):
        assert importer._wht_account_name("VT", ACCOUNT) == "Expenses:Invest:IBKR:WHT:VT"

    def test_wht_account_explicit(self):
        imp = IBKRImporter(account=ACCOUNT, wht_account="Expenses:WHT")
        assert imp._wht_account_name("VT", ACCOUNT) == "Expenses:WHT:VT"

    def test_fees_account_default_derivation(self, importer):
        assert importer._fees_account_name("CHF", ACCOUNT) == "Expenses:Invest:IBKR:Fees:CHF"

    def test_fees_account_explicit(self):
        imp = IBKRImporter(account=ACCOUNT, fees_account="Expenses:Fees")
        assert imp._fees_account_name("CHF", ACCOUNT) == "Expenses:Fees"

    def test_pnl_account_derivation(self, importer):
        assert importer._pnl_account("VT", ACCOUNT) == "Income:Invest:IBKR:VT:PnL"

    def test_div_income_account_derivation(self, importer):
        assert importer._div_income_account("USD", "VT", ACCOUNT) == "Income:Invest:IBKR:VT:Div"


# ===========================================================================
# Account map / _resolve_account
# ===========================================================================


class TestResolveAccount:
    def test_no_map_returns_default_account(self, importer):
        # single-account mode: any ID returns self._account
        imp = IBKRImporter(account=ACCOUNT, query_name=QUERY_NAME)
        assert imp._resolve_account("U12345") == ACCOUNT

    def test_map_returns_mapped_root(self):
        imp = IBKRImporter(
            account=ACCOUNT,
            account_map={"U12345": {"root": "Assets:Invest:IBKR:Trading"}},
        )
        assert imp._resolve_account("U12345") == "Assets:Invest:IBKR:Trading"

    def test_map_raises_for_unmapped_id(self):
        imp = IBKRImporter(
            account=ACCOUNT,
            account_map={"U12345": {"root": "Assets:Invest:IBKR:Trading"}},
        )
        with pytest.raises(RuntimeError, match="U99999"):
            imp._resolve_account("U99999")

    def test_resolve_deposit_from_present(self):
        imp = IBKRImporter(
            account=ACCOUNT,
            account_map={"U12345": {"root": ACCOUNT, "deposit_from": "Assets:Bank:ZKB:CHF"}},
        )
        assert imp._resolve_deposit_from("U12345") == "Assets:Bank:ZKB:CHF"

    def test_resolve_deposit_from_absent(self):
        imp = IBKRImporter(
            account=ACCOUNT,
            account_map={"U12345": {"root": ACCOUNT}},
        )
        assert imp._resolve_deposit_from("U12345") is None

    def test_resolve_deposit_from_no_map(self):
        imp = IBKRImporter(account=ACCOUNT)
        assert imp._resolve_deposit_from("U12345") is None

    def test_invalid_account_map_missing_root_raises(self):
        with pytest.raises(ValueError, match="missing required key 'root'"):
            IBKRImporter(account=ACCOUNT, account_map={"U12345": {"deposit_from": "X"}})

    def test_invalid_account_map_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unknown keys"):
            IBKRImporter(account=ACCOUNT, account_map={"U12345": {"root": ACCOUNT, "typo": "X"}})

    def test_map_routes_different_statements_to_different_roots(self, mocker, tmp_path):
        account_a = "Assets:Invest:IBKR:Trading"
        account_b = "Assets:Invest:IBKR:Pension"
        imp = IBKRImporter(
            account=ACCOUNT,
            query_name=QUERY_NAME,
            account_map={
                "U1111": {"root": account_a},
                "U2222": {"root": account_b},
            },
        )

        from ibflex import Types

        from tests.conftest import (
            STMT_FROM,
            STMT_GENERATED,
            STMT_TO,
            make_buy_trade,
            make_cash_report_row,
        )

        def make_stmt(account_id, symbol):
            return Types.FlexStatement(
                accountId=account_id,
                fromDate=STMT_FROM,
                toDate=STMT_TO,
                period="LastMonth",
                whenGenerated=STMT_GENERATED,
                Trades=(make_buy_trade(symbol=symbol),),
                CashTransactions=(),
                CashReport=(make_cash_report_row(),),
            )

        response = Types.FlexQueryResponse(
            queryName=QUERY_NAME,
            type="AF",
            FlexStatements=(
                make_stmt("U1111", "VT"),
                make_stmt("U2222", "EUNK"),
            ),
        )

        xml_file = tmp_path / "test.xml"
        xml_file.write_text("<FlexQueryResponse/>")
        mocker.patch.object(ibkr_module, "_parse_flex_file", return_value=response)
        entries = imp.extract(str(xml_file), [])

        txns = [e for e in entries if isinstance(e, data.Transaction)]
        vt_txn = next(t for t in txns if any("VT" in p.account for p in t.postings))
        eunk_txn = next(t for t in txns if any("EUNK" in p.account for p in t.postings))

        assert any(p.account == f"{account_a}:VT" for p in vt_txn.postings)
        assert any(p.account == f"{account_b}:EUNK" for p in eunk_txn.postings)


# ===========================================================================
# Module-level helpers
# ===========================================================================


class TestHelpers:
    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("USD.CHF", True),
            ("EUR.USD", True),
            ("VT", False),
            ("VWRL", False),
            ("USD.CH", False),  # second part too short
            ("US.CHFF", False),  # first part too short
        ],
    )
    def test_is_forex(self, symbol, expected):
        assert _is_forex(symbol) == expected

    def test_forex_currencies_split(self):
        prim, sec = _forex_currencies("USD.CHF")
        assert prim == "USD"
        assert sec == "CHF"

    def test_forex_currencies_invalid_raises(self):
        with pytest.raises(ValueError):
            _forex_currencies("NOTFOREX")

    @pytest.mark.parametrize(
        "description,expected",
        [
            ("VT payment in lieu of dividend", CashAction.PAYMENTINLIEU),
            ("VT PAYMENT IN LIEU OF DIVIDEND", CashAction.PAYMENTINLIEU),
            ("VT Cash Dividend", CashAction.DIVIDEND),
            ("VT DIVIDEND", CashAction.DIVIDEND),
            ("US tax on credit int", "interest"),
            ("unrecognised description", None),
        ],
    )
    def test_wht_div_type(self, description, expected):
        assert _wht_div_type(description) == expected

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("VWRL.BATS", "VWRL"),  # exchange suffix stripped
            ("VTz", "VT"),  # trailing z stripped
            ("VWRL", "VWRL"),  # unchanged
            # z-strip only removes trailing z; in "VTz.BATS" the z is not trailing,
            # so suffix-strip produces "VTz" (not "VT")
            ("VTz.BATS", "VTz"),
        ],
    )
    def test_map_symbol(self, importer, symbol, expected):
        assert importer._map_symbol(symbol) == expected

    def test_map_symbol_explicit_remap_takes_priority(self):
        imp = IBKRImporter(account=ACCOUNT, symbol_map={"VWRL": "VWRL3"})
        assert imp._map_symbol("VWRL") == "VWRL3"


# ===========================================================================
# Error resilience
# ===========================================================================


class TestErrorResilience:
    def test_extract_returns_empty_on_parse_failure(self, importer, mocker, tmp_path):
        xml = tmp_path / "bad.xml"
        xml.write_text("<FlexQueryResponse/>")
        mocker.patch.object(ibkr_module, "_parse_flex_file", side_effect=ValueError("bad"))
        entries = importer.extract(str(xml), [])
        assert entries == []

    def test_unrecognised_cash_action_skipped(self, importer, mocker, tmp_path):
        from ibflex.enums import CashAction
        from ibflex.Types import CashTransaction

        # COMMADJ is not handled — should log a warning and produce no entry
        unknown = CashTransaction(
            type=CashAction.COMMADJ,
            symbol="VT",
            currency="USD",
            amount=Decimal("5.00"),
            reportDate=REPORT_DATE,
        )
        response = make_response(cash_transactions=(unknown,))
        entries = _extract(importer, response, mocker, tmp_path)
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 0
