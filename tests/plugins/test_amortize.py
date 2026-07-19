"""Tests for the amortize plugin (amortize.py).

Covers both "spread" and "split" modes, multi-leg transactions, rounding,
open-directive ordering, and error paths.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.number import D

from drnukebean.plugins.amortize import (
    AmortizeError,
    _buffer_account,
    _date_range,
    _get_quantize,
    _split_amounts,
    amortize,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONFIG_SPREAD = "{'buffer_acc_base': 'Assets:Prepaid:'}"
CONFIG_EMPTY = ""


def _meta(
    start: str,
    freq: str,
    times: int,
    mode: str | None = None,
    filename: str = "<test>",
    lineno: int = 0,
) -> dict:
    m = data.new_metadata(filename, lineno)
    m["p_amortize_start"] = start
    m["p_amortize_frequency"] = freq
    m["p_amortize_times"] = str(times)
    if mode is not None:
        m["p_amortize_mode"] = mode
    return m


def _txn(
    date: datetime.date,
    narration: str,
    postings: list[data.Posting],
    meta: dict,
) -> data.Transaction:
    return data.Transaction(
        meta=meta,
        date=date,
        flag="*",
        payee=None,
        narration=narration,
        tags=frozenset(),
        links=frozenset(),
        postings=postings,
    )


def _posting(account: str, amount: Decimal, currency: str = "CHF") -> data.Posting:
    return data.Posting(
        account=account,
        units=Amount(amount, currency),
        cost=None,
        price=None,
        flag=None,
        meta=None,
    )


def _run(entries: list, config: str = CONFIG_SPREAD) -> tuple[list, list]:
    return amortize(entries, {}, config)


def _num(posting: data.Posting) -> Decimal:
    """Postings built by _posting() above always carry a concrete Amount."""
    assert posting.units is not None and posting.units.number is not None
    return posting.units.number


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestDateRange:
    def test_monthly(self):
        dates = _date_range(datetime.date(2026, 1, 1), "M", 3)
        assert dates == [
            datetime.date(2026, 1, 1),
            datetime.date(2026, 2, 1),
            datetime.date(2026, 3, 1),
        ]

    def test_quarterly(self):
        dates = _date_range(datetime.date(2026, 1, 1), "Q", 4)
        assert dates == [
            datetime.date(2026, 1, 1),
            datetime.date(2026, 4, 1),
            datetime.date(2026, 7, 1),
            datetime.date(2026, 10, 1),
        ]

    def test_yearly(self):
        dates = _date_range(datetime.date(2024, 2, 29), "Y", 2)
        assert dates[0] == datetime.date(2024, 2, 29)
        assert dates[1] == datetime.date(2025, 2, 28)  # dateutil clamps leap day

    def test_unknown_freq_raises(self):
        with pytest.raises(ValueError, match="Unknown frequency"):
            _date_range(datetime.date(2026, 1, 1), "X", 3)


class TestSplitAmounts:
    def test_even_split(self):
        splits = _split_amounts(D("120.00"), 12, D("0.01"))
        assert all(s == D("10.00") for s in splits)
        assert sum(splits) == D("120.00")

    def test_rounding_remainder_in_last(self):
        splits = _split_amounts(D("0.13"), 12, D("0.01"))
        assert len(splits) == 12
        assert sum(splits) == D("0.13")
        # First 11 should be 0.01 each; last absorbs -0.02? No: 0.13/12 = 0.0108...
        # rounds up to 0.01 each, so 11 * 0.01 = 0.11, last = 0.02
        assert splits[-1] == D("0.02")

    def test_negative_value(self):
        splits = _split_amounts(D("-1200.00"), 12, D("0.01"))
        assert sum(splits) == D("-1200.00")
        assert all(s == D("-100.00") for s in splits)

    def test_exact_sum(self):
        for value_str in ("100.01", "333.33", "1.00", "0.13"):
            splits = _split_amounts(D(value_str), 12, D("0.01"))
            assert sum(splits) == D(value_str), f"Sum mismatch for {value_str}"


class TestGetQuantize:
    def test_fallback(self):
        assert _get_quantize("CHF", {}) == D("0.01")

    def test_with_dcontext(self):
        # Build a minimal mock matching beancount v3 DisplayContext API:
        #   dctx.ccontexts[currency].get_fractional_digits_common() -> int
        def _make_cctx(digits: int):
            class FakeCctx:
                def get_fractional_digits_common(self) -> int:
                    return digits

            return FakeCctx()

        class FakeDctx:
            ccontexts = {"JPY": _make_cctx(0), "BTC": _make_cctx(3)}

        assert _get_quantize("JPY", {"dcontext": FakeDctx()}) == D("1")
        assert _get_quantize("BTC", {"dcontext": FakeDctx()}) == D("0.001")
        assert _get_quantize("CHF", {"dcontext": FakeDctx()}) == D("0.01")  # fallback


class TestBufferAccount:
    def test_derivation(self):
        p = _posting("Expenses:Insurance:Home", D("800"))
        assert _buffer_account("Assets:Prepaid:", p) == "Assets:Prepaid:Amortize:Insurance:Home"


# ---------------------------------------------------------------------------
# Spread mode — simple 2-posting transaction
# ---------------------------------------------------------------------------


class TestSpreadSimple:
    def setup_method(self):
        meta = _meta("2026-01-01", "M", 12)
        self.txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Annual insurance",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-1200.00")),
                _posting("Expenses:Insurance:Home", D("1200.00")),
            ],
            meta=meta,
        )
        self.entries, self.errors = _run([self.txn])

    def test_no_errors(self):
        assert self.errors == []

    def test_entry_count(self):
        # 1 Open + 1 modified original + 12 children = 14
        assert len(self.entries) == 14

    def test_open_directive_first(self):
        opens = [e for e in self.entries if isinstance(e, data.Open)]
        assert len(opens) == 1
        assert opens[0].date == datetime.date(1970, 1, 1)
        assert opens[0].account == "Assets:Prepaid:Amortize:Insurance:Home"

    def test_modified_original_date(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        orig = txns[0]
        assert orig.date == datetime.date(2026, 1, 1)

    def test_modified_original_postings(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        orig = txns[0]
        accounts = {p.account for p in orig.postings}
        assert "Assets:Bank:ZKB:CHF" in accounts
        assert "Assets:Prepaid:Amortize:Insurance:Home" in accounts
        assert "Expenses:Insurance:Home" not in accounts

    def test_child_dates(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        children = txns[1:]
        assert len(children) == 12
        assert children[0].date == datetime.date(2026, 1, 1)
        assert children[-1].date == datetime.date(2026, 12, 1)

    def test_child_amounts_sum(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        children = txns[1:]
        total_expense = sum(
            _num(p)
            for txn in children
            for p in txn.postings
            if p.account == "Expenses:Insurance:Home"
        )
        assert total_expense == D("1200.00")

    def test_buffer_zeroes_out(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        buf_acc = "Assets:Prepaid:Amortize:Insurance:Home"
        total_buf = sum(_num(p) for txn in txns for p in txn.postings if p.account == buf_acc)
        assert total_buf == D("0")

    def test_child_meta_has_info_key(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        for child in txns[1:]:
            assert "p_amortize" in child.meta

    def test_child_meta_stripped(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        for child in txns[1:]:
            assert "p_amortize_start" not in child.meta
            assert "p_amortize_times" not in child.meta
            assert "p_amortize_frequency" not in child.meta


# ---------------------------------------------------------------------------
# Spread mode — multi-leg (2 expense postings)
# ---------------------------------------------------------------------------


class TestSpreadMultiLeg:
    def setup_method(self):
        meta = _meta("2026-01-01", "M", 12)
        self.txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Annual insurance",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-1200.00")),
                _posting("Expenses:Insurance:Home", D("800.00")),
                _posting("Expenses:Insurance:Car", D("400.00")),
            ],
            meta=meta,
        )
        self.entries, self.errors = _run([self.txn])

    def test_no_errors(self):
        assert self.errors == []

    def test_two_buffer_accounts_opened(self):
        opens = [e for e in self.entries if isinstance(e, data.Open)]
        buf_accounts = {o.account for o in opens}
        assert "Assets:Prepaid:Amortize:Insurance:Home" in buf_accounts
        assert "Assets:Prepaid:Amortize:Insurance:Car" in buf_accounts

    def test_modified_original_has_two_buffers(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        orig_postings = {p.account for p in txns[0].postings}
        assert "Assets:Prepaid:Amortize:Insurance:Home" in orig_postings
        assert "Assets:Prepaid:Amortize:Insurance:Car" in orig_postings
        assert "Expenses:Insurance:Home" not in orig_postings
        assert "Expenses:Insurance:Car" not in orig_postings

    def test_home_buffer_zeroes_out(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        buf_acc = "Assets:Prepaid:Amortize:Insurance:Home"
        total = sum(_num(p) for txn in txns for p in txn.postings if p.account == buf_acc)
        assert total == D("0")

    def test_car_buffer_zeroes_out(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        buf_acc = "Assets:Prepaid:Amortize:Insurance:Car"
        total = sum(_num(p) for txn in txns for p in txn.postings if p.account == buf_acc)
        assert total == D("0")

    def test_expense_totals(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        children = txns[1:]

        home_total = sum(
            _num(p)
            for txn in children
            for p in txn.postings
            if p.account == "Expenses:Insurance:Home"
        )
        car_total = sum(
            _num(p)
            for txn in children
            for p in txn.postings
            if p.account == "Expenses:Insurance:Car"
        )
        assert home_total == D("800.00")
        assert car_total == D("400.00")

    def test_each_child_balanced(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        for child in txns[1:]:
            total = sum(_num(p) for p in child.postings)
            assert total == D("0"), f"Child on {child.date} not balanced: {total}"


# ---------------------------------------------------------------------------
# Split mode
# ---------------------------------------------------------------------------


class TestSplitSimple:
    def setup_method(self):
        meta = _meta("2025-01-01", "M", 12, mode="split")
        self.txn = _txn(
            date=datetime.date(2025, 1, 1),
            narration="Monthly tax accrual",
            postings=[
                _posting("Expenses:Tax:Income", D("1200.00")),
                _posting("Liabilities:Tax:Accrued", D("-1200.00")),
            ],
            meta=meta,
        )
        self.entries, self.errors = _run([self.txn], config=CONFIG_EMPTY)

    def test_no_errors(self):
        assert self.errors == []

    def test_original_replaced_by_twelve(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        assert len(txns) == 12

    def test_no_open_directives(self):
        opens = [e for e in self.entries if isinstance(e, data.Open)]
        assert opens == []

    def test_dates(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        assert txns[0].date == datetime.date(2025, 1, 1)
        assert txns[-1].date == datetime.date(2025, 12, 1)

    def test_amounts_sum_to_original(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        expense_total = sum(
            _num(p) for txn in txns for p in txn.postings if p.account == "Expenses:Tax:Income"
        )
        assert expense_total == D("1200.00")

    def test_each_child_balanced(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        for child in txns:
            total = sum(_num(p) for p in child.postings)
            assert total == D("0"), f"Child on {child.date} not balanced: {total}"

    def test_child_meta_stripped(self):
        txns = [e for e in self.entries if isinstance(e, data.Transaction)]
        for child in txns:
            assert "p_amortize_start" not in child.meta
            assert "p_amortize_mode" not in child.meta
            assert "p_amortize" in child.meta


class TestSplitMultiLeg:
    def test_all_legs_split(self):
        meta = _meta("2025-01-01", "M", 3, mode="split")
        txn = _txn(
            date=datetime.date(2025, 1, 1),
            narration="Three-way split",
            postings=[
                _posting("Expenses:A", D("60.00")),
                _posting("Expenses:B", D("30.00")),
                _posting("Liabilities:X", D("-90.00")),
            ],
            meta=meta,
        )
        entries, errors = _run([txn], config=CONFIG_EMPTY)
        assert errors == []
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        assert len(txns) == 3
        for child in txns:
            total = sum(_num(p) for p in child.postings)
            assert total == D("0")


# ---------------------------------------------------------------------------
# Rounding edge cases
# ---------------------------------------------------------------------------


class TestRounding:
    def test_indivisible_amount_spread(self):
        """CHF 0.13 / 12: sum of splits must equal 0.13 exactly."""
        meta = _meta("2026-01-01", "M", 12)
        txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Tiny amount",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-0.13")),
                _posting("Expenses:Misc", D("0.13")),
            ],
            meta=meta,
        )
        entries, errors = _run([txn])
        assert errors == []
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        children = txns[1:]
        total = sum(
            _num(p) for txn in children for p in txn.postings if p.account == "Expenses:Misc"
        )
        assert total == D("0.13")

    def test_indivisible_amount_split(self):
        """CHF 0.13 / 12 in split mode: total must be preserved."""
        meta = _meta("2026-01-01", "M", 12, mode="split")
        txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Tiny amount split",
            postings=[
                _posting("Expenses:Misc", D("0.13")),
                _posting("Liabilities:X", D("-0.13")),
            ],
            meta=meta,
        )
        entries, errors = _run([txn], config=CONFIG_EMPTY)
        assert errors == []
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        total = sum(_num(p) for txn in txns for p in txn.postings if p.account == "Expenses:Misc")
        assert total == D("0.13")


# ---------------------------------------------------------------------------
# Open directive ordering
# ---------------------------------------------------------------------------


class TestOpenDirectiveOrdering:
    def test_opens_precede_transactions_in_output(self):
        meta = _meta("2026-01-01", "M", 2)
        txn = _txn(
            date=datetime.date(2026, 6, 1),
            narration="Test",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-100.00")),
                _posting("Expenses:Test", D("100.00")),
            ],
            meta=meta,
        )
        entries, _ = _run([txn])
        first_open = next(i for i, e in enumerate(entries) if isinstance(e, data.Open))
        first_txn = next(i for i, e in enumerate(entries) if isinstance(e, data.Transaction))
        assert first_open < first_txn

    def test_no_duplicate_opens_for_same_account(self):
        meta1 = _meta("2026-01-01", "M", 2)
        meta2 = _meta("2026-06-01", "M", 2)
        txn1 = _txn(
            date=datetime.date(2026, 1, 1),
            narration="First",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-100.00")),
                _posting("Expenses:Insurance:Home", D("100.00")),
            ],
            meta=meta1,
        )
        txn2 = _txn(
            date=datetime.date(2026, 6, 1),
            narration="Second",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-200.00")),
                _posting("Expenses:Insurance:Home", D("200.00")),
            ],
            meta=meta2,
        )
        entries, errors = _run([txn1, txn2])
        assert errors == []
        opens = [e for e in entries if isinstance(e, data.Open)]
        buf_opens = [o for o in opens if "Amortize" in o.account]
        assert len(buf_opens) == 1


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_buffer_acc_base_for_spread(self):
        meta = _meta("2026-01-01", "M", 3)
        txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Test",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-300.00")),
                _posting("Expenses:Test", D("300.00")),
            ],
            meta=meta,
        )
        entries, errors = _run([txn], config=CONFIG_EMPTY)
        assert len(errors) == 1
        assert isinstance(errors[0], AmortizeError)
        assert "buffer_acc_base" in errors[0].message

    def test_unknown_mode(self):
        meta = _meta("2026-01-01", "M", 3, mode="weekly")
        txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Test",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-300.00")),
                _posting("Expenses:Test", D("300.00")),
            ],
            meta=meta,
        )
        entries, errors = _run([txn])
        assert len(errors) == 1
        assert "unknown mode" in errors[0].message

    def test_bad_config_string(self):
        meta = _meta("2026-01-01", "M", 3)
        txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Test",
            postings=[_posting("Expenses:Test", D("100.00"))],
            meta=meta,
        )
        entries, errors = _run([txn], config="not valid python {{{")
        assert len(errors) == 1
        assert "Invalid configuration" in errors[0].message

    def test_bad_config_not_dict(self):
        entries, errors = _run([], config="[1, 2, 3]")
        assert len(errors) == 1
        assert "dict" in errors[0].message

    def test_bad_frequency(self):
        meta = _meta("2026-01-01", "X", 3)
        txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Test",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-300.00")),
                _posting("Expenses:Test", D("300.00")),
            ],
            meta=meta,
        )
        entries, errors = _run([txn])
        assert len(errors) == 1
        assert "Unknown frequency" in errors[0].message

    def test_no_income_expense_posting(self):
        meta = _meta("2026-01-01", "M", 3)
        txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Transfer only",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-300.00")),
                _posting("Assets:Bank:Neon:CHF", D("300.00")),
            ],
            meta=meta,
        )
        entries, errors = _run([txn])
        assert len(errors) == 1
        assert "Income/Expense" in errors[0].message

    def test_non_amortize_entries_pass_through(self):
        plain_txn = _txn(
            date=datetime.date(2026, 1, 1),
            narration="Plain transaction",
            postings=[
                _posting("Assets:Bank:ZKB:CHF", D("-50.00")),
                _posting("Expenses:Food", D("50.00")),
            ],
            meta=data.new_metadata("<test>", 0),
        )
        entries, errors = _run([plain_txn])
        assert errors == []
        assert len(entries) == 1
        assert entries[0] is plain_txn
