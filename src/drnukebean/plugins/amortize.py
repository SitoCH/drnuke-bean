"""
A beancount plugin that generalises both spreading and recurring into a single,
multi-leg-capable implementation.

Two modes, selected via the optional `p_amortize_mode` transaction meta key:

  "spread" (default)
    The cash-flow (asset/liability) legs stay on the original transaction date.
    All income/expense legs are deferred through per-posting buffer accounts and
    recognised gradually over N periods.  Equivalent to the legacy `spreading`
    plugin, but supports an arbitrary number of income/expense postings.

    Example: CHF 1 200 insurance paid in December, recognised as CHF 100/month.

        2026-12-01 * "Annual home insurance"
          Assets:Bank:ZKB:CHF          -1200.00 CHF
          Expenses:Insurance:Home        800.00 CHF
          Expenses:Insurance:Car         400.00 CHF
          p_amortize_start: "2026-01-01"
          p_amortize_frequency: "M"
          p_amortize_times: "12"

  "split"
    The original transaction is replaced by N sub-transactions. All posting
    amounts are scaled to 1/N; the last period absorbs rounding remainders.
    No buffer account is used.  Equivalent to the legacy `recurring` plugin.

    Example: model a year's tax liability as 12 equal monthly accruals.

        2025-01-01 * "Monthly tax accrual"
          Expenses:Tax:Income             833.00 CHF
          Liabilities:Tax:Accrued        -833.00 CHF
          p_amortize_start: "2025-01-01"
          p_amortize_frequency: "M"
          p_amortize_times: "12"

Required transaction meta keys:
  p_amortize_start      "YYYY-MM-DD"  start date of first period
  p_amortize_frequency  str           period length: D W M Q Y
  p_amortize_times      str(int)      number of periods

Optional transaction meta keys:
  p_amortize_mode       "spread" | "split"   default: "spread"

Plugin invocation (buffer_acc_base required for "spread" mode):
  plugin "drnukebean.plugins.amortize" "{'buffer_acc_base': 'Assets:Prepaid:'}"

Supported frequency strings:
  D  daily     W  weekly     M  monthly     Q  quarterly     Y  yearly
"""

import ast
import collections
import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from beancount.core import account as acc
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.number import D
from dateutil.relativedelta import relativedelta

__plugins__ = ["amortize"]

AmortizeError = collections.namedtuple("AmortizeError", "source message entry")

_FREQ_DELTAS: dict[str, relativedelta] = {
    "D": relativedelta(days=1),
    "W": relativedelta(weeks=1),
    "M": relativedelta(months=1),
    "Q": relativedelta(months=3),
    "Y": relativedelta(years=1),
}

_INCOME_EXPENSE_ROOTS = frozenset({"Income", "Expenses"})
_ASSET_LIABILITY_ROOTS = frozenset({"Assets", "Liabilities"})

_META_KEYS = frozenset(
    {"p_amortize_start", "p_amortize_frequency", "p_amortize_times", "p_amortize_mode"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date_range(start: datetime.date, freq: str, n: int) -> list[datetime.date]:
    delta = _FREQ_DELTAS.get(freq)
    if delta is None:
        raise ValueError(f"Unknown frequency: {freq!r}. Supported: {sorted(_FREQ_DELTAS)}")
    dates: list[datetime.date] = []
    current = start
    for _ in range(n):
        dates.append(current)
        current += delta
    return dates


def _get_quantize(currency: str, options_map: dict[str, Any]) -> Decimal:
    """Return the smallest unit for `currency` from the display context.

    Uses DisplayContext.ccontexts[currency].get_fractional_digits_common() which
    returns the number of fractional digits most commonly observed for that currency
    (e.g. 2 for CHF/EUR/USD).  Falls back to 0.01 when the display context is absent
    or the currency has not been seen yet.
    """
    dctx = options_map.get("dcontext")
    if dctx is not None:
        ccontexts = getattr(dctx, "ccontexts", None)
        if ccontexts is not None:
            cctx = ccontexts.get(currency)
            if cctx is not None:
                digits: int = cctx.get_fractional_digits_common()
                return Decimal(10) ** (-digits)
    return D("0.01")


def _split_amounts(value: Decimal, n: int, quant: Decimal) -> list[Decimal]:
    """Split `value` into `n` parts rounded to `quant`; last part absorbs remainder."""
    part = (value / n).quantize(quant, rounding=ROUND_HALF_UP)
    splits = [part] * (n - 1)
    splits.append(value - sum(splits))
    return splits


def _buffer_account(base: str, posting: data.Posting) -> str:
    # Beancount Posting stub has account as Optional; assert to narrow.
    assert posting.account is not None
    return base + "Amortize:" + acc.sans_root(posting.account)  # type: ignore[operator]


def _child_meta(entry: data.Transaction, n: int) -> dict[str, Any]:
    """Build meta for child transactions: strip amortize keys, add info string."""
    meta = {k: v for k, v in entry.meta.items() if k not in _META_KEYS}
    freq = entry.meta.get("p_amortize_frequency", "?")
    meta["p_amortize"] = (
        f"split into {n} chunks, {freq}, original date {entry.date.strftime(r'%Y-%m-%d')}"
    )
    return meta


# ---------------------------------------------------------------------------
# Mode: spread
# ---------------------------------------------------------------------------


def _spread_entry(
    entry: data.Transaction,
    buffer_acc_base: str,
    options_map: dict[str, Any],
) -> tuple[list[Any], list[data.Open], list[Any]]:
    """Transform one transaction into a modified original + N child transactions.

    The asset/liability legs are unchanged in the original transaction.
    Each income/expense leg is replaced by a buffer posting; N child transactions
    drain the buffers and reconstitute the income/expense postings pro-rata.
    """
    errors: list[Any] = []
    new_txns: list[Any] = []
    new_opens: list[data.Open] = []

    try:
        n = int(entry.meta["p_amortize_times"])
        freq = entry.meta["p_amortize_frequency"]
        start = datetime.date.fromisoformat(entry.meta["p_amortize_start"])
        dates = _date_range(start, freq, n)
    except (KeyError, ValueError) as exc:
        errors.append(AmortizeError(entry.meta, f"amortize (spread): {exc}", entry))
        return [entry], new_opens, errors

    income_postings = [p for p in entry.postings if acc.root(1, p.account) in _INCOME_EXPENSE_ROOTS]
    asset_postings = [p for p in entry.postings if acc.root(1, p.account) in _ASSET_LIABILITY_ROOTS]

    if not income_postings:
        errors.append(
            AmortizeError(
                entry.meta,
                "amortize (spread): no Income/Expense posting found; skipping.",
                entry,
            )
        )
        return [entry], new_opens, errors

    # Build buffer postings (same sign as the original expense posting so the
    # modified transaction remains balanced).
    buffer_postings: list[data.Posting] = []
    for p in income_postings:
        # Beancount Posting stubs have units/account as Optional; assert to narrow.
        assert p.units is not None
        buf_acc = _buffer_account(buffer_acc_base, p)
        buffer_postings.append(
            data.Posting(
                account=buf_acc,
                units=p.units,
                cost=None,
                price=None,
                flag=None,
                meta=None,
            )
        )
        new_opens.append(
            data.Open(
                data.new_metadata("<amortize>", 0),
                datetime.date(1970, 1, 1),
                buf_acc,
                [p.units.currency],
                None,
            )
        )

    # Modified original: asset legs unchanged, expense legs replaced by buffers
    new_txns.append(
        data.Transaction(
            meta=entry.meta,
            date=entry.date,
            flag=entry.flag,
            payee=entry.payee,
            narration=entry.narration,
            tags=entry.tags,
            links=entry.links,
            postings=asset_postings + buffer_postings,
        )
    )

    # Pre-compute split amounts per income/expense posting
    split_table: list[list[Decimal]] = []
    for p in income_postings:
        assert p.units is not None  # narrowing beancount Optional stub
        assert p.units.number is not None
        quant = _get_quantize(p.units.currency, options_map)
        split_table.append(_split_amounts(p.units.number, n, quant))

    child_meta = _child_meta(entry, n)

    for i, date in enumerate(dates):
        child_postings: list[data.Posting] = []
        for j, p in enumerate(income_postings):
            assert p.units is not None  # narrowing beancount Optional stub
            split = split_table[j][i]
            buf_acc = _buffer_account(buffer_acc_base, p)
            # Reconstituted income/expense posting (pro-rata)
            child_postings.append(
                data.Posting(
                    account=p.account,
                    units=Amount(split, p.units.currency),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None,
                )
            )
            # Corresponding buffer drain (opposite sign)
            child_postings.append(
                data.Posting(
                    account=buf_acc,
                    units=Amount(-split, p.units.currency),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None,
                )
            )
        new_txns.append(
            data.Transaction(
                meta=child_meta,
                date=date,
                flag="*",
                payee=entry.payee,
                narration=entry.narration,
                tags=entry.tags,
                links=entry.links,
                postings=child_postings,
            )
        )

    return new_txns, new_opens, errors


# ---------------------------------------------------------------------------
# Mode: split
# ---------------------------------------------------------------------------


def _split_entry(
    entry: data.Transaction,
    options_map: dict[str, Any],
) -> tuple[list[Any], list[Any]]:
    """Replace one transaction with N equal-amount sub-transactions."""
    errors: list[Any] = []

    try:
        n = int(entry.meta["p_amortize_times"])
        freq = entry.meta["p_amortize_frequency"]
        start = datetime.date.fromisoformat(entry.meta["p_amortize_start"])
        dates = _date_range(start, freq, n)
    except (KeyError, ValueError) as exc:
        errors.append(AmortizeError(entry.meta, f"amortize (split): {exc}", entry))
        return [entry], errors

    # Compute split amounts per posting; last period absorbs rounding remainder
    split_table: list[list[Decimal]] = []
    for p in entry.postings:
        assert p.units is not None  # narrowing beancount Optional stub
        assert p.units.number is not None
        quant = _get_quantize(p.units.currency, options_map)
        split_table.append(_split_amounts(p.units.number, n, quant))

    # Correct per-date balance residuals: for each date slot, the sum across all
    # postings must be zero (balanced transaction). Any non-zero residual from
    # independent per-posting rounding is absorbed into the last posting.
    for i in range(n):
        slot_sum = sum(split_table[j][i] for j in range(len(entry.postings)))
        if slot_sum != 0:
            split_table[-1][i] -= slot_sum

    child_meta = _child_meta(entry, n)

    new_txns: list[Any] = []
    for i, date in enumerate(dates):
        child_postings = [
            p._replace(units=p.units._replace(number=split_table[j][i]))  # type: ignore[union-attr]
            for j, p in enumerate(entry.postings)
        ]
        new_txns.append(
            data.Transaction(
                meta=child_meta,
                date=date,
                flag=entry.flag,
                payee=entry.payee,
                narration=entry.narration,
                tags=entry.tags,
                links=entry.links,
                postings=child_postings,
            )
        )

    return new_txns, errors


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def amortize(entries: list, options_map: dict[str, Any], config_str: str) -> tuple[list, list]:
    errors: list[Any] = []

    try:
        config_obj: dict[str, Any] = ast.literal_eval(config_str) if config_str.strip() else {}
    except (ValueError, SyntaxError) as exc:
        errors.append(
            AmortizeError(
                data.new_metadata("<amortize>", 0),
                f"Invalid configuration for amortize plugin: {exc}",
                None,
            )
        )
        return entries, errors

    if not isinstance(config_obj, dict):
        errors.append(
            AmortizeError(
                data.new_metadata("<amortize>", 0),
                "amortize plugin: configuration must be a dict.",
                None,
            )
        )
        return entries, errors

    buffer_acc_base: str = config_obj.get("buffer_acc_base", "")

    # Collect results separately so opens precede spread transactions in output
    other_entries: list[Any] = []
    opens: list[data.Open] = []
    spread_txns: list[Any] = []
    opened_accounts: set[str] = {e.account for e in entries if isinstance(e, data.Open)}

    for entry in entries:
        if not (isinstance(entry, data.Transaction) and "p_amortize_start" in entry.meta):
            other_entries.append(entry)
            continue

        mode = entry.meta.get("p_amortize_mode", "spread")

        if mode == "split":
            new_txns, errs = _split_entry(entry, options_map)
            spread_txns.extend(new_txns)
            errors.extend(errs)

        elif mode == "spread":
            if not buffer_acc_base:
                errors.append(
                    AmortizeError(
                        entry.meta,
                        "amortize (spread): 'buffer_acc_base' missing from plugin config.",
                        entry,
                    )
                )
                other_entries.append(entry)
                continue
            new_txns, new_opens, errs = _spread_entry(entry, buffer_acc_base, options_map)
            spread_txns.extend(new_txns)
            for o in new_opens:
                if o.account not in opened_accounts:
                    opens.append(o)
                    opened_accounts.add(o.account)
            errors.extend(errs)

        else:
            errors.append(
                AmortizeError(
                    entry.meta,
                    f"amortize: unknown mode {mode!r}. Use 'spread' or 'split'.",
                    entry,
                )
            )
            other_entries.append(entry)

    return other_entries + opens + spread_txns, errors
