"""
A beancount plugin to create N copies of the same transaction, with all amounts
split evenly across the copies.

Typical use cases:
  - Monthly tax liability accrual: model the ongoing liability before the bill arrives.
  - Year-end securities P&L: distribute an annual figure across 12 monthly entries.

The original transaction is replaced by N sub-transactions, each dated at the
corresponding interval. All posting amounts are scaled to 1/N of the original;
the last period absorbs any rounding remainder.

A transaction subject to this plugin needs:
  1) any combination of postings (no account-type restriction)
  2) the following three meta-entries (with example values):
       recurring_frequency: "M"
       recurring_start: "2020-01-01"
       recurring_times: "12"

Supported frequency strings: D (daily), W (weekly), M (monthly), Q (quarterly), Y (yearly).

The plugin is called with no parameter:
  plugin "drnukebean.plugins.recurring"
"""

import datetime
from typing import Any

from beancount.core import data
from dateutil.relativedelta import relativedelta

__plugins__ = ["recurring"]

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


def recurring(entries: list, options_map: dict[str, Any], config_str: str) -> tuple[list, list]:
    errors: list = []
    new_entries: list = []

    for entry in entries:
        if isinstance(entry, data.Transaction) and "recurring_start" in entry.meta:
            start_date = datetime.date.fromisoformat(entry.meta["recurring_start"])
            frequency = entry.meta["recurring_frequency"]
            times = int(entry.meta["recurring_times"])

            date_range = _date_range(start_date, frequency, times)

            # Compute split amounts per posting; last period absorbs rounding remainder
            amounts: list[tuple[int, str, list]] = []
            for idx, p in enumerate(entry.postings):
                # Beancount Posting stubs have units/account as Optional; assert to narrow.
                assert p.units is not None
                assert p.units.number is not None
                assert p.account is not None
                amount_orig = p.units.number
                splits = [round(amount_orig / times, 2) for _ in range(times - 1)]
                splits.append(amount_orig - sum(splits))
                amounts.append((idx, p.account, splits))

            # Correct for per-date rounding imbalance across postings.
            # For each date slot, sum all postings' split amounts. A balanced
            # transaction must sum to zero; any non-zero residual is absorbed
            # into the last posting's split for that slot.
            rounding_errors = [sum(splits[i] for _, _, splits in amounts) for i in range(times)]
            if any(e != 0 for e in rounding_errors):
                last_idx, last_account, last_splits = amounts[-1]
                amounts[-1] = (
                    last_idx,
                    last_account,
                    [v - rounding_errors[i] for i, v in enumerate(last_splits)],
                )

            # Prepare child transaction meta: strip recurring keys, add info key
            dropkeys = {"recurring_start", "recurring_frequency", "recurring_times"}
            meta = {k: v for k, v in entry.meta.items() if k not in dropkeys}
            meta["recurring"] = (
                f"split amounts into {times} chunks, "
                f"{entry.meta['recurring_frequency']}, "
                f"original txn date {entry.date.strftime(r'%Y-%m-%d')}"
            )

            for idx_date, new_date in enumerate(date_range):
                new_txn = data.Transaction(
                    meta=meta,
                    date=new_date,
                    flag=entry.flag,
                    payee=entry.payee,
                    narration=entry.narration,
                    tags=entry.tags,
                    links=entry.links,
                    postings=[],
                )
                for idx, _account, splits in amounts:
                    posting = entry.postings[idx]
                    assert posting.units is not None
                    new_posting = posting._replace(
                        units=posting.units._replace(number=splits[idx_date])
                    )
                    new_txn.postings.append(new_posting)
                new_entries.append(new_txn)
        else:
            new_entries.append(entry)

    return new_entries, errors
