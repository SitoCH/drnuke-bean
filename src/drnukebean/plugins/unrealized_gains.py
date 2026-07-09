"""Track unrealized gains from cost-basis holdings as monthly income transactions.

Ported from xentac's unrealized_periodic plugin to beancount v3.

Each month (on the last calendar day), injects a transaction recognising the
delta unrealized P&L.  The offset goes to an Equity account so that the
balance-sheet NAV is unaffected.  The previous month's entry is reversed inside
the same transaction so only the period delta flows through income.

Config string: "equity_subaccount[,income_subaccount]"
  - Single value: used for both equity and income (classic mode).
  - Two comma-separated values: equity subaccount and income subaccount are set
    independently (FVTPL mode — monthly deltas land in the same account as
    realized gains, eliminating the sell-date income spike).

Classic mode (subaccount="Unrealized"):
  Equity: Equity:Invest:IBKR:VT:Unrealized   <- balance offset, no NAV impact
  Income: Income:Invest:IBKR:VT:Unrealized   <- period income/loss

FVTPL mode (config="Unrealized,PnL"):
  Equity: Equity:Invest:IBKR:VT:Unrealized   <- balance offset, no NAV impact
  Income: Income:Invest:IBKR:VT:PnL          <- same account as realized gains

When a position is fully closed during a month, the clear entry is dated to the
last day of that same month (not the next month), so realized and unrealized
effects land in the same accounting period.  The clear always posts to the income
account discovered in the sell transaction, netting realized and reversed-unrealized
in one place.

Plugin invocation:
  plugin "drnukebean.plugins.unrealized_gains" "Unrealized"
  plugin "drnukebean.plugins.unrealized_gains" "Unrealized,PnL"

Default: "Unrealized".  Pass "" to book directly into the root accounts.
"""

__author__ = "drnuke"

import collections
import datetime
from decimal import Decimal

from beancount.core import account, data, getters
from beancount.core import inventory as inv_mod
from beancount.core import prices as prices_mod
from beancount.core.amount import Amount
from beancount.core.number import ZERO
from beancount.ops import summarize
from beancount.parser import options

__plugins__ = ("add_unrealized_gains",)

UnrealizedError = collections.namedtuple("UnrealizedError", "source message entry")

FLAG_UNREALIZED = "U"
_ONEDAY = datetime.timedelta(days=1)


def _next_month(date: datetime.date) -> datetime.date:
    if date.month == 12:
        return datetime.date(date.year + 1, 1, 1)
    return datetime.date(date.year, date.month + 1, 1)


def _last_day_of_month(date: datetime.date) -> datetime.date:
    return _next_month(datetime.date(date.year, date.month, 1)) - _ONEDAY


def _get_holdings(
    entries: list,
) -> dict[tuple[str, str, str], tuple[Decimal, Decimal]]:
    """Return {(account, currency, cost_currency): (total_units, total_book_value)}."""
    account_invs: dict[str, inv_mod.Inventory] = collections.defaultdict(inv_mod.Inventory)
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            account_invs[posting.account].add_position(posting)

    groups: dict[tuple[str, str, str], tuple[Decimal, Decimal]] = {}
    for acc_name, inv in account_invs.items():
        for pos in inv:
            if pos.cost is None:
                continue
            currency = pos.units.currency
            cost_currency = pos.cost.currency
            units = pos.units.number
            book_value = units * pos.cost.number
            key = (acc_name, currency, cost_currency)
            if key in groups:
                prev_units, prev_book = groups[key]
                groups[key] = (prev_units + units, prev_book + book_value)
            else:
                groups[key] = (units, book_value)
    return groups


def _matching_unrealized(
    entry: data.Transaction,
    equity_account: str,
    cost_currency: str,
    prev_currency: str,
) -> bool:
    return (
        any(p.account == equity_account for p in entry.postings)
        and entry.postings[0].units.currency == cost_currency
        and entry.meta.get("prev_currency") == prev_currency
    )


def _find_sell_income_account(
    entries: list,
    acc_name: str,
    currency: str,
    period_start: datetime.date,
    period_end: datetime.date,
    income_account_type: str,
    meta: dict,
) -> tuple[str | None, object | None]:
    """Return the unique income account used when selling `currency` from `acc_name` in the period.

    Errors if none found (sell transaction missing income posting) or if multiple distinct
    income accounts are involved (ambiguous — cannot net unrealized clear against realized gain).
    """
    income_prefix = income_account_type + ":"
    income_accounts: set[str] = set()
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        if not (period_start <= entry.date <= period_end):
            continue
        has_reduction = any(
            p.account == acc_name and p.units.currency == currency and p.units.number < 0
            for p in entry.postings
        )
        if not has_reduction:
            continue
        for p in entry.postings:
            if p.account.startswith(income_prefix):
                income_accounts.add(p.account)

    if not income_accounts:
        return None, UnrealizedError(
            meta,
            f"No income account found for sell of {currency} from {acc_name} "
            f"between {period_start} and {period_end} — cannot clear unrealized gain",
            None,
        )
    if len(income_accounts) > 1:
        return None, UnrealizedError(
            meta,
            f"Multiple income accounts found for sell of {currency} from {acc_name}: "
            f"{', '.join(sorted(income_accounts))} — cannot unambiguously clear unrealized gain",
            None,
        )
    return income_accounts.pop(), None


def _find_previous_unrealized(
    unrealized_entries: list,
    equity_account: str,
    cost_currency: str,
    prev_currency: str,
    include_clear: bool = False,
) -> data.Transaction | None:
    for entry in reversed(unrealized_entries):
        if _matching_unrealized(entry, equity_account, cost_currency, prev_currency):
            if not include_clear and entry.narration.startswith("Clear unrealized"):
                return None
            return entry
    return None


def _add_unrealized_gains_at_date(
    entries: list,
    unrealized_entries: list,
    income_account_type: str,
    equity_account_type: str,
    price_map: dict,
    date: datetime.date,
    meta: dict,
    equity_subaccount: str,
    income_subaccount: str,
) -> tuple[list, set, list]:
    errors: list = []
    entries_truncated = summarize.truncate(entries, date + _ONEDAY)
    holdings = _get_holdings(entries_truncated)

    holdings_with_currencies: set[tuple[str, str, str]] = set()
    new_entries: list = []

    for index, ((acc_name, currency, cost_currency), (total_units, book_value)) in enumerate(
        holdings.items()
    ):
        if currency == cost_currency:
            continue
        if total_units == ZERO:
            errors.append(
                UnrealizedError(
                    meta,
                    f"Units of {currency} in {acc_name} for cost {cost_currency} sum to zero",
                    None,
                )
            )
            continue

        price_date, price_number = prices_mod.get_price(price_map, (currency, cost_currency), date)
        if price_number is None:
            if total_units:
                errors.append(
                    UnrealizedError(
                        meta,
                        f"A valid price for {currency}/{cost_currency} could not be found",
                        None,
                    )
                )
            continue

        market_value = total_units * price_number
        pnl = market_value - book_value

        # Equity offset (no NAV impact) mirrors the asset account under Equity root.
        equity_account = account.join(equity_account_type, account.sans_root(acc_name))
        income_account = account.join(income_account_type, account.sans_root(acc_name))
        if equity_subaccount:
            equity_account = account.join(equity_account, equity_subaccount)
        if income_subaccount:
            income_account = account.join(income_account, income_subaccount)

        holdings_with_currencies.add((acc_name, cost_currency, currency))

        latest = _find_previous_unrealized(
            unrealized_entries, equity_account, cost_currency, currency
        )

        if latest and pnl == latest.postings[0].units.number:
            continue
        if pnl == ZERO and not latest:
            continue

        prev_pnl = latest.postings[0].units.number if latest else ZERO
        relative_pnl = pnl - prev_pnl
        gain_loss_str = "gain" if relative_pnl > ZERO else "loss"
        avg_cost = book_value / total_units
        narration = (
            f"Unrealized {gain_loss_str} for {total_units} units of {currency} "
            f"(price: {price_number:.4f} {cost_currency} as of {price_date}, "
            f"average cost: {avg_cost:.4f} {cost_currency})"
        )

        txn_meta = data.new_metadata(meta["filename"], 1000 + index)
        txn_meta["prev_currency"] = currency
        entry = data.Transaction(txn_meta, date, FLAG_UNREALIZED, None, narration, set(), set(), [])
        entry.postings.extend(
            [
                # Equity: debit on gain (positive pnl); offset keeps NAV neutral.
                data.Posting(equity_account, Amount(pnl, cost_currency), None, None, None, None),
                # Income: credit on gain (negative amount = income in beancount).
                data.Posting(income_account, Amount(-pnl, cost_currency), None, None, None, None),
            ]
        )
        if latest:
            for posting in latest.postings[:2]:
                entry.postings.append(
                    data.Posting(posting.account, -posting.units, None, None, None, None)
                )

        new_entries.append(entry)

    return new_entries, holdings_with_currencies, errors


def _make_clear_entries(
    entries: list,
    new_entries: list,
    closed_positions: set,
    equity_account_type: str,
    income_account_type: str,
    period_start: datetime.date,
    period_end: datetime.date,
    subaccount: str,
    meta: dict,
    current_holdings: set,
) -> tuple[list, list]:
    """Build clear entries for positions that closed during the period.

    For sales, the income side posts to the realized gains account discovered in
    the sell transaction so that realized and reversed-unrealized amounts net in
    one place.

    For transfers (currency still held somewhere else this month), both sides are
    reversed directly so no net income or equity effect remains.
    """
    clear_entries: list = []
    errors: list = []
    for acc_name, cost_currency, currency in closed_positions:
        equity_account = account.join(equity_account_type, account.sans_root(acc_name))
        if subaccount:
            equity_account = account.join(equity_account, subaccount)
        latest = _find_previous_unrealized(new_entries, equity_account, cost_currency, currency)
        if not latest:
            continue

        clear_meta = data.new_metadata(meta["filename"], 999)
        clear_meta["prev_currency"] = currency
        eq_p, inc_p = latest.postings[0], latest.postings[1]

        # A position that disappeared from one account but still exists in another
        # account this month is a transfer, not a sale.  Reverse both sides
        # symmetrically so neither income nor equity is affected.
        is_transfer = any(c == currency for (_, _, c) in current_holdings)
        if is_transfer:
            txn = data.Transaction(
                clear_meta,
                period_end,
                FLAG_UNREALIZED,
                None,
                f"Reverse unrealized gains/losses of {currency} — position transferred",
                set(),
                set(),
                [],
            )
            txn.postings.append(data.Posting(eq_p.account, -eq_p.units, None, None, None, None))
            txn.postings.append(data.Posting(inc_p.account, -inc_p.units, None, None, None, None))
            clear_entries.append(txn)
            continue

        realized_income_account, err = _find_sell_income_account(
            entries,
            acc_name,
            currency,
            period_start,
            period_end,
            income_account_type,
            meta,
        )
        if err:
            errors.append(err)
            continue

        txn = data.Transaction(
            clear_meta,
            period_end,
            FLAG_UNREALIZED,
            None,
            f"Clear unrealized gains/losses of {currency}",
            set(),
            set(),
            [],
        )
        # Equity side: negate the prior equity posting (clears balance-sheet offset).
        txn.postings.append(data.Posting(eq_p.account, -eq_p.units, None, None, None, None))
        # Income side: post to realized gains account so it nets against the sell.
        txn.postings.append(
            data.Posting(realized_income_account, -inc_p.units, None, None, None, None)
        )
        clear_entries.append(txn)

    return clear_entries, errors


def add_unrealized_gains(
    entries: list, options_map: dict, config: str = "Unrealized"
) -> tuple[list, list]:
    errors: list = []
    meta = data.new_metadata("<unrealized_gains>", 0)
    account_types = options.get_account_types(options_map)

    parts = [p.strip() for p in config.split(",", 1)] if config else ["", ""]
    equity_subaccount = parts[0]
    income_subaccount = parts[1] if len(parts) > 1 else parts[0]

    for label, sub in (("equity", equity_subaccount), ("income", income_subaccount)):
        if sub and not account.is_valid(account.join(account_types.assets, sub)):
            errors.append(UnrealizedError(meta, f"Invalid {label} subaccount name: {sub!r}", None))
            return entries, errors

    if not entries:
        return entries, errors

    price_map = prices_mod.build_price_map(entries)
    new_entries: list = []

    # Use last calendar day of each month so that mid-month sells and their
    # corresponding clears land in the same accounting period.
    date = _last_day_of_month(entries[0].date)
    last_date = _last_day_of_month(entries[-1].date)
    last_holdings_with_currencies: set | None = None
    prev_date: datetime.date | None = None

    while date <= last_date:
        date_entries, holdings_with_currencies, date_errors = _add_unrealized_gains_at_date(
            entries,
            new_entries,
            account_types.income,
            account_types.equity,
            price_map,
            date,
            meta,
            equity_subaccount,
            income_subaccount,
        )
        new_entries.extend(date_entries)
        errors.extend(date_errors)

        if last_holdings_with_currencies:
            period_start = (prev_date + _ONEDAY) if prev_date else entries[0].date
            closed = last_holdings_with_currencies - holdings_with_currencies
            clear_entries, clear_errors = _make_clear_entries(
                entries,
                new_entries,
                closed,
                account_types.equity,
                account_types.income,
                period_start,
                date,
                equity_subaccount,
                meta,
                holdings_with_currencies,
            )
            new_entries.extend(clear_entries)
            errors.extend(clear_errors)

        last_holdings_with_currencies = holdings_with_currencies
        prev_date = date
        date = _last_day_of_month(_next_month(date))

    if not new_entries:
        return entries, errors

    new_accounts = {p.account for e in new_entries for p in e.postings}
    existing_accounts = getters.get_account_open_close(entries)
    new_open_entries: list = []
    for idx, acc_ in enumerate(sorted(new_accounts)):
        if acc_ not in existing_accounts:
            m = data.new_metadata(meta["filename"], idx)
            new_open_entries.append(data.Open(m, new_entries[0].date, acc_, None, None))

    return entries + new_open_entries + new_entries, errors


def get_unrealized_entries(entries: list) -> list:
    """Return only the auto-generated unrealized gain/loss transactions."""
    return [e for e in entries if isinstance(e, data.Transaction) and e.flag == FLAG_UNREALIZED]
