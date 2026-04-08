"""Shared pytest fixtures for drnukebean tests."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from ibflex import Types
from ibflex.enums import BuySell, CashAction

from drnukebean.importer.ibkr import IBKRImporter

# ---------------------------------------------------------------------------
# Constants used across test modules
# ---------------------------------------------------------------------------

ACCOUNT = "Assets:Invest:IBKR"
QUERY_NAME = "TestQuery"

REPORT_DATE = datetime.date(2024, 1, 31)
TRADE_DATE = datetime.date(2024, 1, 15)
TRADE_DT = datetime.datetime(2024, 1, 15, 10, 30, 0)
OPEN_DT = datetime.datetime(2023, 6, 1, 9, 0, 0)

STMT_FROM = datetime.date(2024, 1, 1)
STMT_TO = datetime.date(2024, 1, 31)
STMT_GENERATED = datetime.datetime(2024, 2, 1, 12, 0, 0)

# ---------------------------------------------------------------------------
# Importer fixture
# ---------------------------------------------------------------------------


_DEPOSIT_FROM = "Assets:Bank:ZKB:CHF"


@pytest.fixture
def importer() -> IBKRImporter:
    return IBKRImporter(
        account=ACCOUNT,
        query_name=QUERY_NAME,
        currency="CHF",
        account_map={"U1234567": {"root": ACCOUNT, "deposit_from": _DEPOSIT_FROM}},
    )


@pytest.fixture
def importer_no_queryname() -> IBKRImporter:
    return IBKRImporter(account=ACCOUNT, currency="CHF")


# ---------------------------------------------------------------------------
# FlexQueryResponse builder helpers
# ---------------------------------------------------------------------------


def make_statement(
    trades: tuple = (),
    cash_transactions: tuple = (),
    cash_report: tuple = (),
) -> Types.FlexStatement:
    return Types.FlexStatement(
        accountId="U1234567",
        fromDate=STMT_FROM,
        toDate=STMT_TO,
        period="LastMonth",
        whenGenerated=STMT_GENERATED,
        Trades=trades,
        CashTransactions=cash_transactions,
        CashReport=cash_report,
    )


def make_response(
    trades: tuple = (),
    cash_transactions: tuple = (),
    cash_report: tuple = (),
    query_name: str = QUERY_NAME,
) -> Types.FlexQueryResponse:
    return Types.FlexQueryResponse(
        queryName=query_name,
        type="AF",
        FlexStatements=(make_statement(trades, cash_transactions, cash_report),),
    )


def make_cash_report_row(
    currency: str = "CHF",
    ending_cash: str = "10000.00",
    to_date: datetime.date = REPORT_DATE,
) -> Types.CashReportCurrency:
    return Types.CashReportCurrency(
        currency=currency,
        endingCash=Decimal(ending_cash),
        toDate=to_date,
    )


def make_buy_trade(
    symbol: str = "VT",
    currency: str = "USD",
    quantity: str = "10",
    trade_price: str = "100.00",
    proceeds: str = "-1000.50",
    commission: str = "-1.00",
    commission_currency: str = "USD",
    trade_date: datetime.date = TRADE_DATE,
    date_time: datetime.datetime = TRADE_DT,
) -> Types.Trade:
    return Types.Trade(
        symbol=symbol,
        currency=currency,
        quantity=Decimal(quantity),
        tradePrice=Decimal(trade_price),
        proceeds=Decimal(proceeds),
        ibCommission=Decimal(commission),
        ibCommissionCurrency=commission_currency,
        tradeDate=trade_date,
        dateTime=date_time,
        buySell=BuySell.BUY,
        levelOfDetail="EXECUTION",
    )


def make_sell_trade(
    symbol: str = "VT",
    currency: str = "USD",
    quantity: str = "-5",
    trade_price: str = "110.00",
    proceeds: str = "549.50",
    commission: str = "-1.00",
    commission_currency: str = "USD",
    trade_date: datetime.date = TRADE_DATE,
    date_time: datetime.datetime = TRADE_DT,
) -> Types.Trade:
    return Types.Trade(
        symbol=symbol,
        currency=currency,
        quantity=Decimal(quantity),
        tradePrice=Decimal(trade_price),
        proceeds=Decimal(proceeds),
        ibCommission=Decimal(commission),
        ibCommissionCurrency=commission_currency,
        tradeDate=trade_date,
        dateTime=date_time,
        buySell=BuySell.SELL,
        levelOfDetail="EXECUTION",
    )


def make_closed_lot(
    symbol: str = "VT",
    currency: str = "USD",
    quantity: str = "5",  # positive: shares consumed from this lot
    trade_price: str = "100.00",
    open_date_time: datetime.datetime = OPEN_DT,
) -> Types.Trade:
    return Types.Trade(
        symbol=symbol,
        currency=currency,
        quantity=Decimal(quantity),
        tradePrice=Decimal(trade_price),
        openDateTime=open_date_time,
        buySell=BuySell.BUY,
        levelOfDetail="CLOSED_LOT",
    )


def make_forex_trade(
    symbol: str = "USD.CHF",
    quantity: str = "1000.00",
    proceeds: str = "-880.00",
    trade_price: str = "0.88",
    commission: str = "-2.00",
    commission_currency: str = "CHF",
    trade_date: datetime.date = TRADE_DATE,
    date_time: datetime.datetime = TRADE_DT,
) -> Types.Trade:
    return Types.Trade(
        symbol=symbol,
        currency="USD",
        quantity=Decimal(quantity),
        tradePrice=Decimal(trade_price),
        proceeds=Decimal(proceeds),
        ibCommission=Decimal(commission),
        ibCommissionCurrency=commission_currency,
        tradeDate=trade_date,
        dateTime=date_time,
        buySell=BuySell.BUY,
        levelOfDetail="EXECUTION",
    )


def make_dividend(
    symbol: str = "VT",
    currency: str = "USD",
    amount: str = "87.00",
    description: str = "VT (US9229083632) CASH DIVIDEND USD 0.8700 PER SHARE (Ordinary Dividend)",
    report_date: datetime.date = REPORT_DATE,
) -> Types.CashTransaction:
    return Types.CashTransaction(
        type=CashAction.DIVIDEND,
        symbol=symbol,
        currency=currency,
        amount=Decimal(amount),
        description=description,
        reportDate=report_date,
    )


def make_wht(
    symbol: str = "VT",
    currency: str = "USD",
    amount: str = "-13.05",
    description: str = "VT (US9229083632) CASH DIVIDEND USD 0.8700 PER SHARE - US TAX",
    report_date: datetime.date = REPORT_DATE,
) -> Types.CashTransaction:
    return Types.CashTransaction(
        type=CashAction.WHTAX,
        symbol=symbol,
        currency=currency,
        amount=Decimal(amount),
        description=description,
        reportDate=report_date,
    )


def make_roc(
    symbol: str = "VT",
    currency: str = "USD",
    amount: str = "50.00",
    report_date: datetime.date = REPORT_DATE,
) -> Types.CashTransaction:
    return Types.CashTransaction(
        type=CashAction.DIVIDEND,
        symbol=symbol,
        currency=currency,
        amount=Decimal(amount),
        description="VT Return of Capital",
        reportDate=report_date,
    )


def make_fee(
    currency: str = "CHF",
    amount: str = "-10.00",
    description: str = "Minimum Fee Jan 2024",
    report_date: datetime.date = REPORT_DATE,
) -> Types.CashTransaction:
    return Types.CashTransaction(
        type=CashAction.FEES,
        symbol=None,
        currency=currency,
        amount=Decimal(amount),
        description=description,
        reportDate=report_date,
    )


def make_interest(
    currency: str = "CHF",
    amount: str = "5.00",
    description: str = "Credit Interest for Jan 2024",
    report_date: datetime.date = REPORT_DATE,
) -> Types.CashTransaction:
    return Types.CashTransaction(
        type=CashAction.BROKERINTRCVD,
        symbol=None,
        currency=currency,
        amount=Decimal(amount),
        description=description,
        reportDate=report_date,
    )


def make_deposit(
    currency: str = "CHF",
    amount: str = "5000.00",
    report_date: datetime.date = REPORT_DATE,
) -> Types.CashTransaction:
    return Types.CashTransaction(
        type=CashAction.DEPOSITWITHDRAW,
        symbol=None,
        currency=currency,
        amount=Decimal(amount),
        description="Electronic Fund Transfer",
        reportDate=report_date,
    )
