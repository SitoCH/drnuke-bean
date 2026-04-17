"""Tests for the unrealized_gains plugin."""

import datetime

from beancount import loader
from beancount.core import data as bdata
from beancount.core.number import D

from drnukebean.plugins.unrealized_gains import (
    FLAG_UNREALIZED,
    add_unrealized_gains,
    get_unrealized_entries,
)


def _load(ledger_str: str):
    entries, errors, options_map = loader.load_string(ledger_str)
    assert not errors, errors
    return entries, options_map


# ---------------------------------------------------------------------------
# Shared ledger strings
# Prices are placed on month-end dates so they are visible to the end-of-month
# inventory snapshots the plugin uses.
# ---------------------------------------------------------------------------

_BASIC = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Assets:Invest:VT VT
2020-01-01 open Equity:Opening USD

2020-01-15 * "Buy VT"
  Assets:Invest:VT  10 VT {100.00 USD}
  Assets:Cash      -1000.00 USD

2020-01-31 price VT 110.00 USD
"""

_TWO_MONTHS = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Assets:Invest:VT VT

2020-01-15 * "Buy VT"
  Assets:Invest:VT  10 VT {100.00 USD}
  Assets:Cash      -1000.00 USD

2020-01-31 price VT 110.00 USD
2020-02-29 price VT 120.00 USD
"""

# Position sold mid-February; clear should land in February (same period as sale).
_SELL_ALL = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Assets:Invest:VT VT
2020-01-01 open Income:Capital:Gains USD

2020-01-15 * "Buy VT"
  Assets:Invest:VT  10 VT {100.00 USD}
  Assets:Cash      -1000.00 USD

2020-01-31 price VT 110.00 USD

2020-02-15 * "Sell VT"
  Assets:Invest:VT  -10 VT {100.00 USD} @ 110.00 USD
  Assets:Cash        1100.00 USD
  Income:Capital:Gains  -100.00 USD

2020-02-29 price VT 115.00 USD
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_basic_unrealized_gain():
    entries, options_map = _load(_BASIC)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert not errors
    unrealized = get_unrealized_entries(entries)
    assert len(unrealized) == 1

    entry = unrealized[0]
    assert entry.date == datetime.date(2020, 1, 31)
    assert entry.flag == FLAG_UNREALIZED

    # Posting[0]: Equity offset (+pnl); Posting[1]: Income (-pnl = income credit).
    equity_p, income_p = entry.postings[0], entry.postings[1]
    assert equity_p.account == "Equity:Invest:VT:Unrealized"
    assert equity_p.units.number == D("100.00")
    assert equity_p.units.currency == "USD"
    assert income_p.account == "Income:Invest:VT:Unrealized"
    assert income_p.units.number == D("-100.00")
    assert income_p.units.currency == "USD"

    # No reversal postings (no previous entry exists).
    assert len(entry.postings) == 2


def test_delta_across_months():
    entries, options_map = _load(_TWO_MONTHS)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert not errors
    unrealized = get_unrealized_entries(entries)
    assert len(unrealized) == 2

    jan_entry = unrealized[0]
    feb_entry = unrealized[1]

    assert jan_entry.date == datetime.date(2020, 1, 31)
    assert feb_entry.date == datetime.date(2020, 2, 29)

    # January: PnL = 10*(110-100) = 100; no prior entry to reverse.
    assert jan_entry.postings[0].units.number == D("100.00")
    assert len(jan_entry.postings) == 2

    # February: total PnL = 10*(120-100) = 200; reversal of January's 100 appended.
    assert feb_entry.postings[0].units.number == D("200.00")
    assert feb_entry.postings[2].units.number == D("-100.00")  # reversal equity
    assert feb_entry.postings[3].units.number == D("100.00")  # reversal income
    assert len(feb_entry.postings) == 4

    # Net equity effect for February = +200 - 100 = +100 (the delta).
    equity_net = sum(
        p.units.number
        for p in feb_entry.postings
        if "VT:Unrealized" in p.account and p.account.startswith("Equity")
    )
    assert equity_net == D("100.00")


def test_sell_clears_in_same_month():
    """Clear entry for a sold position lands in the same month as the sale."""
    entries, options_map = _load(_SELL_ALL)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert not errors
    unrealized = get_unrealized_entries(entries)

    # January entry (holding open) + February clear (position sold on Feb 15).
    assert len(unrealized) == 2

    jan_entry, feb_entry = unrealized[0], unrealized[1]

    assert jan_entry.date == datetime.date(2020, 1, 31)
    assert feb_entry.date == datetime.date(2020, 2, 29)
    assert feb_entry.narration.startswith("Clear unrealized")

    # Equity side negates the January equity posting.
    assert feb_entry.postings[0].units.number == D("-100.00")
    assert feb_entry.postings[0].account == "Equity:Invest:VT:Unrealized"
    # Income side goes to the realized gains account so it nets against the sell.
    assert feb_entry.postings[1].units.number == D("100.00")
    assert feb_entry.postings[1].account == "Income:Capital:Gains"


def test_clear_error_no_income_account():
    """Error when the sell transaction has no income posting to net against."""
    ledger = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Assets:Invest:VT VT

2020-01-15 * "Buy VT"
  Assets:Invest:VT  10 VT {100.00 USD}
  Assets:Cash      -1000.00 USD

2020-01-31 price VT 110.00 USD

2020-02-15 * "Sell VT at cost (no income posting)"
  Assets:Invest:VT  -10 VT {100.00 USD} @ 100.00 USD
  Assets:Cash        1000.00 USD

2020-02-29 price VT 115.00 USD
"""
    entries, options_map = _load(ledger)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert errors
    assert "No income account found" in errors[0].message


def test_clear_error_multiple_income_accounts():
    """Error when the sell spans multiple distinct income accounts (ambiguous netting)."""
    ledger = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Assets:Invest:VT VT
2020-01-01 open Income:Capital:ShortTerm USD
2020-01-01 open Income:Capital:LongTerm USD

2020-01-15 * "Buy VT"
  Assets:Invest:VT  10 VT {100.00 USD}
  Assets:Cash      -1000.00 USD

2020-01-31 price VT 110.00 USD

2020-02-15 * "Sell VT split across two income accounts"
  Assets:Invest:VT    -10 VT {100.00 USD} @ 110.00 USD
  Assets:Cash          1100.00 USD
  Income:Capital:ShortTerm  -60.00 USD
  Income:Capital:LongTerm   -40.00 USD

2020-02-29 price VT 115.00 USD
"""
    entries, options_map = _load(ledger)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert errors
    assert "Multiple income accounts" in errors[0].message


def test_no_subaccount():
    entries, options_map = _load(_BASIC)
    entries, errors = add_unrealized_gains(entries, options_map, "")

    assert not errors
    unrealized = get_unrealized_entries(entries)
    assert len(unrealized) == 1

    equity_p = unrealized[0].postings[0]
    income_p = unrealized[0].postings[1]
    assert equity_p.account == "Equity:Invest:VT"
    assert income_p.account == "Income:Invest:VT"


def test_nav_unaffected():
    """Equity offset means Assets - Liabilities does not change."""
    entries, options_map = _load(_BASIC)
    entries_with, errors = add_unrealized_gains(entries, options_map, "Unrealized")
    assert not errors

    unrealized = get_unrealized_entries(entries_with)
    for entry in unrealized:
        for posting in entry.postings:
            assert not posting.account.startswith("Assets"), (
                f"Unexpected asset posting in unrealized entry: {posting.account}"
            )
            assert not posting.account.startswith("Liabilities"), (
                f"Unexpected liability posting in unrealized entry: {posting.account}"
            )


def test_auto_open_entries_created():
    entries, options_map = _load(_BASIC)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")
    assert not errors

    open_accounts = {e.account for e in entries if isinstance(e, bdata.Open)}
    assert "Equity:Invest:VT:Unrealized" in open_accounts
    assert "Income:Invest:VT:Unrealized" in open_accounts


def test_no_price_produces_error():
    ledger = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Assets:Invest:VT VT

2020-01-15 * "Buy VT"
  Assets:Invest:VT  10 VT {100.00 USD}
  Assets:Cash      -1000.00 USD
"""
    entries, options_map = _load(ledger)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert errors
    assert "could not be found" in errors[0].message
    assert not get_unrealized_entries(entries)


def test_no_cost_basis_skipped():
    ledger = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Equity:Opening USD

2020-01-01 * "Opening balance"
  Assets:Cash        5000.00 USD
  Equity:Opening    -5000.00 USD
"""
    entries, options_map = _load(ledger)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert not errors
    assert not get_unrealized_entries(entries)


def test_unchanged_pnl_no_duplicate():
    """Same price in consecutive months → only one entry generated."""
    ledger = """\
option "operating_currency" "USD"

2020-01-01 open Assets:Cash USD
2020-01-01 open Assets:Invest:VT VT

2020-01-15 * "Buy VT"
  Assets:Invest:VT  10 VT {100.00 USD}
  Assets:Cash      -1000.00 USD

2020-01-31 price VT 110.00 USD
2020-02-29 price VT 110.00 USD
"""
    entries, options_map = _load(ledger)
    entries, errors = add_unrealized_gains(entries, options_map, "Unrealized")

    assert not errors
    unrealized = get_unrealized_entries(entries)
    assert len(unrealized) == 1
