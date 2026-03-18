"""Shared helpers used across drnukebean importers."""

import re

from beancount.core.amount import Amount


def remove_spaces(s: str) -> str:
    """Collapse internal whitespace and strip leading/trailing spaces.

    ' Hello   this is      my   ledger  ' -> 'Hello this is my ledger'
    Used to normalise bloated bank-statement payee and narration strings.
    """
    return re.sub(" +", " ", s.strip())


def amount_add(a: Amount, b: Amount) -> Amount:
    """Add two Amounts of the same currency."""
    if a.currency != b.currency:
        raise ValueError(
            f"Cannot add amounts of different currencies: {a.currency} and {b.currency}"
        )
    assert a.number is not None and b.number is not None
    return Amount(a.number + b.number, a.currency)


def minus(a: Amount) -> Amount:
    """Return the negated Amount (same currency, opposite sign)."""
    assert a.number is not None
    return Amount(-a.number, a.currency)
