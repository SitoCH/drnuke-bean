"""
A beancount plugin to spread out transactions over a time period. I.e. spread
out a yearly bill or account report over months.

The original cash flow (asset/liability leg) stays on the transaction date.
The P&L (income/expense legs) is routed through an intermediate buffer account
and recognized gradually over N periods.

A transaction subject to this plugin needs:
  1) one income/expenses leg
  2) one asset/liabilities leg
  3) the following three meta-entries (with example values):
       p_spreading_frequency: "M"
       p_spreading_start: "2020-01-01"
       p_spreading_times: "12"

Supported frequency strings: D (daily), W (weekly), M (monthly), Q (quarterly), Y (yearly).

The plugin must be called with a parameter 'liability_acc_base':
  plugin "drnukebean.plugins.spreading" "{'liability_acc_base': 'Assets:Liabilities:'}"
which is the stem of the account that hosts the intermediately spread-out balance.
"""

import ast
import collections
import datetime
from typing import Any

from beancount.core import account as acc
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.data import Transaction
from dateutil.relativedelta import relativedelta

__plugins__ = ["spreading"]

SpreadingError = collections.namedtuple("SpreadingError", "source message entry")

_FREQ_DELTAS: dict[str, relativedelta] = {
    "D": relativedelta(days=1),
    "W": relativedelta(weeks=1),
    "M": relativedelta(months=1),
    "Q": relativedelta(months=3),
    "Y": relativedelta(years=1),
}


def _date_range(start: datetime.date, freq: str, n: int) -> list[datetime.date]:
    delta = _FREQ_DELTAS.get(freq)
    if delta is None:
        raise ValueError(f"Unknown frequency: {freq!r}. Supported: {list(_FREQ_DELTAS)}")
    dates: list[datetime.date] = []
    current = start
    for _ in range(n):
        dates.append(current)
        current += delta
    return dates


def spreading(entries: list, options_map: dict[str, Any], config_str: str) -> tuple[list, list]:
    new_entries: list = []
    errors: list = []
    opened_accounts: list[str] = [e.account for e in entries if isinstance(e, data.Open)]

    try:
        config_obj = ast.literal_eval(config_str) if config_str.strip() else {}
    except (ValueError, SyntaxError) as exc:
        errors.append(
            SpreadingError(
                data.new_metadata("<spreading>", 0),
                f"Invalid configuration for spreading plugin: {exc}",
                None,
            )
        )
        return entries, errors

    if not isinstance(config_obj, dict):
        errors.append(
            SpreadingError(
                data.new_metadata("<spreading>", 0),
                "Invalid configuration for spreading plugin; expected a dict.",
                None,
            )
        )
        return entries, errors

    if "liability_acc_base" not in config_obj:
        errors.append(
            SpreadingError(
                data.new_metadata("<spreading>", 0),
                "spreading plugin: 'liability_acc_base' is missing in the parameters; skipping.",
                None,
            )
        )
        return entries, errors

    for entry in entries:
        if isinstance(entry, Transaction) and "p_spreading_start" in entry.meta:
            spread_entries, spread_errors, open_directive = spread(entry, config_obj)
            new_entries.extend(spread_entries)
            if open_directive.account not in opened_accounts:
                opened_accounts.append(open_directive.account)
                new_entries.append(open_directive)
            errors.extend(spread_errors)
        else:
            new_entries.append(entry)

    return new_entries, errors


def spread(entry: Transaction, config_obj: dict[str, Any]) -> tuple[list, list, data.Open]:
    """Compute the spread version of a transaction.

    The asset/liability leg stays on the original date. The income/expense leg
    is replaced by a buffer account posting, which is then drained over N child
    transactions spread across the requested date range.
    """
    entries: list = []
    errors: list = []

    asset_posting = get_asset(entry)
    income_posting = get_income(entry)
    # Beancount Posting stubs have units/account as Optional; assert to narrow type.
    assert income_posting.account is not None
    assert income_posting.units is not None
    assert asset_posting.units is not None
    claim_account = (
        config_obj["liability_acc_base"] + "Spreading:" + acc.sans_root(income_posting.account)
    )
    open_directive = data.Open(
        data.new_metadata("<spreading>", 0),
        datetime.date(1970, 1, 1),
        claim_account,
        [income_posting.units.currency],
        None,
    )

    units = asset_posting.units
    assert units.number is not None  # Amount.number stub is Optional; assert to narrow
    value = units.number
    currency = units.currency
    amount = Amount(value, currency)

    # Buffer posting for the modified original transaction
    claim_posting = data.Posting(
        account=claim_account, units=-amount, cost=None, price=None, flag=None, meta=None
    )

    # Modified original: cash flow preserved, P&L replaced by buffer
    trans_orig = data.Transaction(
        meta=entry.meta,
        date=entry.date,
        flag=entry.flag,
        payee=entry.payee,
        narration=entry.narration,
        tags=entry.tags,
        links=entry.links,
        postings=[claim_posting, asset_posting],
    )
    entries.append(trans_orig)

    # Spread child transactions
    n_divides = int(entry.meta["p_spreading_times"])
    try:
        dates = _date_range(
            datetime.date.fromisoformat(entry.meta["p_spreading_start"]),
            entry.meta["p_spreading_frequency"],
            n_divides,
        )
    except (ValueError, KeyError) as exc:
        errors.append(SpreadingError(entry.meta, f"spreading plugin: {exc}", entry))
        return entries, errors, open_directive

    # Amounts: last period absorbs rounding remainder
    splits = [round(value / n_divides, 2) for _ in range(n_divides - 1)]
    splits.append(value - sum(splits))

    dropkeys = {"p_spreading_times", "p_spreading_start", "p_spreading_frequency"}
    base_meta = {k: v for k, v in entry.meta.items() if k not in dropkeys}
    base_meta["p_spreading"] = (
        f"split {value} into {n_divides} chunks, "
        f"{entry.meta['p_spreading_frequency']}, "
        f"original date {entry.date.strftime(r'%Y-%m-%d')}"
    )

    for date, split in zip(dates, splits, strict=True):
        split_amount = Amount(split, currency)
        pnl = data.Posting(
            account=income_posting.account,
            units=-split_amount,
            cost=None,
            price=None,
            flag=None,
            meta=None,
        )
        claim = data.Posting(
            account=claim_account,
            units=split_amount,
            cost=None,
            price=None,
            flag=None,
            meta=None,
        )
        trans = data.Transaction(
            meta=base_meta,
            date=date,
            flag="*",
            payee=entry.payee,
            narration=entry.narration,
            tags=entry.tags,
            links=entry.links,
            postings=[pnl, claim],
        )
        entries.append(trans)

    return entries, errors, open_directive


def get_income(entry: Transaction) -> data.Posting:
    """Return the first income/expense posting of a transaction."""
    for post in entry.postings:
        if acc.root(1, post.account) in ("Income", "Expenses"):
            return post
    raise ValueError(f"entry did not have an Income/Expense posting: {entry}")


def get_asset(entry: Transaction) -> data.Posting:
    """Return the first asset/liability posting of a transaction."""
    for post in entry.postings:
        if acc.root(1, post.account) in ("Assets", "Liabilities"):
            return post
    raise ValueError(f"entry did not have an Asset/Liability posting: {entry}")
