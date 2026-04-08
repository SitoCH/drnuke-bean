"""Tests for PFGImporter (pfg.py).

Unit tests build minimal PF CSV strings inline and write them to tmp_path.
Integration tests parse the full real fixture:
    tests/fixtures/pfg/statement.csv

Fixture facts (used in integration assertions):
  - Statement period:   01.10.2023 - 31.10.2023
  - IBAN:               CH1234567890123456789
  - Currency:           CHF
  - Transactions:       37
  - Closing balance:    2.50 CHF  (first data row, on 2023-10-31 -> directive date 2023-11-01)
  - First debit row:    31.10.2023, -63.94 CHF, "PREIS FÜR BANKPAKET SMART 09.2023"
  - Only credit:        23.10.2023, +0.65 CHF, "GUTSCHRIFT  ABSENDER:  ZÜRICH MITTEILUNGEN:  "
                        (narration should be space-normalised by remove_spaces)
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data

from drnukebean.importer.pfg import (
    PFGImporter,
    _col,
    _decimal_or_zero,
    _parse_date,
    _parse_header,
    _PFGHeader,
    _strip_pf_cell,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pfg" / "statement.csv"

_IBAN = "CH1234567890123456789"
_ACCOUNT = "Assets:Bank:PFG:CHF"
_ENCODING = "utf-8"


# ---------------------------------------------------------------------------
# CSV building helpers
# ---------------------------------------------------------------------------


def _pfg_csv(
    *,
    iban: str = _IBAN,
    currency: str = "CHF",
    date_from: str = "01.10.2023",
    date_to: str = "31.10.2023",
    rows: str = "",
) -> str:
    """Return a minimal but structurally valid PF CSV string."""
    return (
        f'Datum von:;="{date_from}"\n'
        f'Datum bis:;="{date_to}"\n'
        'Kategorie:;="Alle"\n'
        f'Konto:;="{iban}"\n'
        f'Währung:;="{currency}"\n'
        "\n"
        "Datum;Avisierungstext;Gutschrift in CHF;Lastschrift in CHF;"
        "Label;Kategorie;Valuta;Saldo in CHF\n"
        "\n"
        f"{rows}"
    )


def _row(
    date: str = "15.10.2023",
    text: str = "Test transaction",
    credit: str = "",
    debit: str = "",
    balance: str = "",
) -> str:
    """Return a single transaction row in PF CSV format."""
    return f'{date};"{text}";{credit};{debit};;Category;{date};{balance}\n'


def _write(tmp_path: Path, content: str, name: str = "statement.csv") -> str:
    p = tmp_path / name
    p.write_text(content, encoding=_ENCODING)
    return str(p)


def _importer(*, balance_account: str | None = None) -> PFGImporter:
    return PFGImporter(
        iban=_IBAN,
        account=_ACCOUNT,
        balance_account=balance_account,
    )


def _units(txn: data.Transaction) -> data.Amount:
    """Return the units of the first posting, asserting it is not None."""
    units = txn.postings[0].units
    assert units is not None
    return units


# ===========================================================================
# Module-level helpers
# ===========================================================================


class TestStripPfCell:
    def test_strips_equals_and_quotes(self):
        assert _strip_pf_cell('="01.10.2023"') == "01.10.2023"

    def test_strips_quotes_only(self):
        assert _strip_pf_cell('"hello"') == "hello"

    def test_plain_value_unchanged(self):
        assert _strip_pf_cell("CHF") == "CHF"


class TestDecimalOrZero:
    def test_empty_string_returns_zero(self):
        assert _decimal_or_zero("") == Decimal("0")

    def test_whitespace_returns_zero(self):
        assert _decimal_or_zero("   ") == Decimal("0")

    def test_plain_positive(self):
        assert _decimal_or_zero("84.75") == Decimal("84.75")

    def test_negative_value(self):
        assert _decimal_or_zero("-63.94") == Decimal("-63.94")

    def test_swiss_apostrophe_thousands(self):
        assert _decimal_or_zero("1'234.56") == Decimal("1234.56")

    def test_no_float_precision_loss(self):
        # Decimal("2.5") must not become Decimal("2.50000000...1") via float
        assert _decimal_or_zero("2.5") == Decimal("2.5")

    def test_invalid_string_returns_zero(self):
        assert _decimal_or_zero("not-a-number") == Decimal("0")


class TestParseDate:
    def test_parses_ddmmyyyy(self):
        assert _parse_date("31.10.2023") == datetime.date(2023, 10, 31)

    def test_strips_whitespace(self):
        assert _parse_date("  01.10.2023  ") == datetime.date(2023, 10, 1)


class TestCol:
    def test_returns_first_matching_key(self):
        row = {"Datum": "2023-10-01", "Date": "ignored"}
        assert _col(row, "Datum", "Date") == "2023-10-01"

    def test_falls_back_to_second_key(self):
        row = {"Date": "2023-10-01"}
        assert _col(row, "Datum", "Date") == "2023-10-01"

    def test_returns_empty_when_no_key_matches(self):
        assert _col({}, "Datum", "Date") == ""


class TestParseHeader:
    def test_returns_pfgheader_dataclass(self, tmp_path):
        path = _write(tmp_path, _pfg_csv())
        header = _parse_header(path, _ENCODING)
        assert isinstance(header, _PFGHeader)

    def test_date_from(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(date_from="01.10.2023"))
        assert _parse_header(path, _ENCODING).date_from == datetime.date(2023, 10, 1)

    def test_date_to(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(date_to="31.10.2023"))
        assert _parse_header(path, _ENCODING).date_to == datetime.date(2023, 10, 31)

    def test_iban(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(iban=_IBAN))
        assert _parse_header(path, _ENCODING).iban == _IBAN

    def test_currency(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(currency="EUR"))
        assert _parse_header(path, _ENCODING).currency == "EUR"

    def test_raises_on_malformed_file(self, tmp_path):
        path = _write(tmp_path, "not a pf file\n")
        with pytest.raises(ValueError):
            _parse_header(path, _ENCODING)


# ===========================================================================
# identify()
# ===========================================================================


class TestIdentify:
    def test_match(self, tmp_path):
        path = _write(tmp_path, _pfg_csv())
        assert _importer().identify(path) is True

    def test_wrong_iban(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(iban="CH9999999999999999999"))
        assert _importer().identify(path) is False

    def test_wrong_extension(self, tmp_path):
        p = tmp_path / "statement.xml"
        p.write_text(_pfg_csv(), encoding=_ENCODING)
        assert _importer().identify(str(p)) is False

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("", encoding=_ENCODING)
        assert _importer().identify(str(p)) is False

    def test_iban_spaces_stripped(self, tmp_path):
        # Constructor should normalise IBAN with spaces
        path = _write(tmp_path, _pfg_csv(iban=_IBAN))
        imp = PFGImporter(iban="CH12 3456 7890 1234 5678 9", account=_ACCOUNT)
        assert imp.identify(path) is True


# ===========================================================================
# account() / name / date() / filename()
# ===========================================================================


class TestMetadata:
    def test_account(self, tmp_path):
        path = _write(tmp_path, _pfg_csv())
        assert _importer().account(path) == _ACCOUNT

    def test_name_property(self, tmp_path):
        assert _importer().name == f"pfg.{_ACCOUNT}"

    def test_date_returns_date_from(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(date_from="01.10.2023"))
        assert _importer().date(path) == datetime.date(2023, 10, 1)

    def test_date_returns_none_on_bad_file(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("garbage", encoding=_ENCODING)
        assert _importer().date(str(p)) is None

    def test_filename_format(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(date_from="01.10.2023"))
        last4 = _IBAN[-4:]
        assert _importer().filename(path) == f"pfg_2023-10-01_{last4}.csv"

    def test_filename_fallback_on_bad_file(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("garbage", encoding=_ENCODING)
        assert _importer().filename(str(p)) == "pfg.csv"


# ===========================================================================
# Balance directive
# ===========================================================================


class TestBalance:
    def test_balance_emitted_from_first_row_with_value(self, tmp_path):
        # First row has balance; second row does not
        rows = _row(date="31.10.2023", debit="-100.00", balance="500.00")
        rows += _row(date="30.10.2023", debit="-50.00", balance="")
        path = _write(tmp_path, _pfg_csv(rows=rows))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert len(bals) == 1

    def test_balance_date_is_day_after_transaction(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(date="31.10.2023", balance="500.00")))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].date == datetime.date(2023, 11, 1)

    def test_balance_amount(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(balance="1'234.50")))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].amount.number == Decimal("1234.50")
        assert bals[0].amount.currency == "CHF"

    def test_balance_account_defaults_to_account(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(balance="100.00")))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].account == _ACCOUNT

    def test_balance_account_override(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(balance="100.00")))
        imp = PFGImporter(iban=_IBAN, account=_ACCOUNT, balance_account="Assets:Bank:PFG")
        bals = [e for e in imp.extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].account == "Assets:Bank:PFG"

    def test_no_balance_when_all_rows_sparse(self, tmp_path):
        rows = _row(balance="") + _row(balance="")
        path = _write(tmp_path, _pfg_csv(rows=rows))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert len(bals) == 0

    def test_balance_from_second_row_when_first_is_sparse(self, tmp_path):
        # First row has no balance; second row does -> balance taken from second
        rows = _row(date="31.10.2023", balance="")
        rows += _row(date="30.10.2023", balance="800.00")
        path = _write(tmp_path, _pfg_csv(rows=rows))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert len(bals) == 1
        assert bals[0].date == datetime.date(2023, 10, 31)
        assert bals[0].amount.number == Decimal("800.00")


# ===========================================================================
# Transactions — amounts
# ===========================================================================


class TestTransactionAmounts:
    def test_debit_amount_negative(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(debit="-75.30")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert _units(txns[0]).number == Decimal("-75.30")

    def test_credit_amount_positive(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(credit="84.75")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert _units(txns[0]).number == Decimal("84.75")

    def test_credit_and_debit_sum(self, tmp_path):
        # Edge case: both populated (unusual but handled by credit + debit)
        path = _write(tmp_path, _pfg_csv(rows=_row(credit="10.00", debit="-3.00")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert _units(txns[0]).number == Decimal("7.00")

    def test_amount_currency_matches_config(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(debit="-10.00")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert _units(txns[0]).currency == "CHF"

    def test_posting_account_is_configured_account(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(debit="-10.00")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].postings[0].account == _ACCOUNT

    def test_posting_is_single_legged(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(debit="-10.00")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert len(txns[0].postings) == 1


# ===========================================================================
# Transactions — dates and narrations
# ===========================================================================


class TestTransactionFields:
    def test_transaction_date(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(date="15.10.2023")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].date == datetime.date(2023, 10, 15)

    def test_narration_from_text_column(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(text="KAUF MIGROS ZÜRICH")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "KAUF MIGROS ZÜRICH"

    def test_narration_whitespace_normalised(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row(text="GUTSCHRIFT  ABSENDER:  ZÜRICH")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "GUTSCHRIFT ABSENDER: ZÜRICH"

    def test_payee_is_empty(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row()))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].payee == ""

    def test_flag_is_star(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=_row()))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].flag == "*"

    def test_multiple_rows_produce_multiple_transactions(self, tmp_path):
        rows = _row(date="31.10.2023") + _row(date="30.10.2023") + _row(date="29.10.2023")
        path = _write(tmp_path, _pfg_csv(rows=rows))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert len(txns) == 3


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_currency_mismatch_returns_empty(self, tmp_path):
        # File has EUR, importer configured for CHF
        path = _write(tmp_path, _pfg_csv(currency="EUR"))
        assert _importer().extract(path, []) == []

    def test_blank_rows_are_skipped(self, tmp_path):
        # _pfg_csv() already includes a blank row after the header; no extra Notes expected
        path = _write(tmp_path, _pfg_csv(rows=_row()))
        entries = _importer().extract(path, [])
        notes = [e for e in entries if isinstance(e, data.Note)]
        assert len(notes) == 0

    def test_disclaimer_rows_are_skipped(self, tmp_path):
        # Rows whose date column doesn't match DD.MM.YYYY are silently skipped
        rows = _row() + "Disclaimer:\n" + "Some legal text\n"
        path = _write(tmp_path, _pfg_csv(rows=rows))
        entries = _importer().extract(path, [])
        notes = [e for e in entries if isinstance(e, data.Note)]
        assert len(notes) == 0

    def test_empty_statement_no_transactions(self, tmp_path):
        path = _write(tmp_path, _pfg_csv(rows=""))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns == []

    def test_identify_returns_false_on_non_csv_extension(self, tmp_path):
        p = tmp_path / "statement.pdf"
        p.write_text(_pfg_csv(), encoding=_ENCODING)
        assert _importer().identify(str(p)) is False

    def test_bad_header_extract_returns_empty(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("not;a;pfg;file\n", encoding=_ENCODING)
        assert _importer().extract(str(p), []) == []


# ===========================================================================
# Language support: English column names
# ===========================================================================


class TestEnglishColumnNames:
    """Verify that EN column headers are handled identically to DE."""

    def _write_en(self, tmp_path: Path, rows: str = "") -> str:
        content = (
            f'Date from:;="01.10.2023"\n'
            f'Date to:;="31.10.2023"\n'
            'Booking type:;="All"\n'
            f'Account:;="{_IBAN}"\n'
            'Currency:;="CHF"\n'
            "\n"
            "Date;Notification text;Credit in CHF;Debit in CHF;"
            "Label;Category;Value;Balance in CHF\n"
            "\n"
            f"{rows}"
        )
        return _write(tmp_path, content)

    def _en_row(self, date="15.10.2023", text="Test EN", credit="", debit="", balance=""):
        return f'{date};"{text}";{credit};{debit};;Category;{date};{balance}\n'

    def test_en_identify(self, tmp_path):
        path = self._write_en(tmp_path)
        assert _importer().identify(path) is True

    def test_en_debit_amount(self, tmp_path):
        path = self._write_en(tmp_path, self._en_row(debit="-42.00"))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert _units(txns[0]).number == Decimal("-42.00")

    def test_en_narration(self, tmp_path):
        path = self._write_en(tmp_path, self._en_row(text="Card payment"))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "Card payment"

    def test_en_balance(self, tmp_path):
        path = self._write_en(tmp_path, self._en_row(balance="999.00"))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].amount.number == Decimal("999.00")


# ===========================================================================
# Integration tests — full fixture
# ===========================================================================


@pytest.fixture(scope="module")
def imp():
    return PFGImporter(iban=_IBAN, account=_ACCOUNT)


@pytest.fixture(scope="module")
def all_entries(imp):
    return imp.extract(str(FIXTURE), [])


@pytest.fixture(scope="module")
def transactions(all_entries):
    return [e for e in all_entries if isinstance(e, data.Transaction)]


@pytest.fixture(scope="module")
def balances(all_entries):
    return [e for e in all_entries if isinstance(e, data.Balance)]


class TestIntegration:
    def test_fixture_exists(self):
        assert FIXTURE.exists(), f"Fixture not found: {FIXTURE}"

    def test_identify_real_fixture(self, imp):
        assert imp.identify(str(FIXTURE)) is True

    def test_identify_wrong_iban_rejects_fixture(self):
        imp_wrong = PFGImporter(iban="CH9999999999999999999", account=_ACCOUNT)
        assert imp_wrong.identify(str(FIXTURE)) is False

    def test_date_returns_date_from(self, imp):
        assert imp.date(str(FIXTURE)) == datetime.date(2023, 10, 1)

    def test_filename_format(self, imp):
        last4 = _IBAN[-4:]
        assert imp.filename(str(FIXTURE)) == f"pfg_2023-10-01_{last4}.csv"

    def test_transaction_count(self, transactions):
        assert len(transactions) == 37

    def test_no_spurious_notes(self, all_entries):
        notes = [e for e in all_entries if isinstance(e, data.Note)]
        assert len(notes) == 0

    def test_balance_count(self, balances):
        assert len(balances) == 1

    def test_balance_amount(self, balances):
        assert balances[0].amount.number == Decimal("2.5")
        assert balances[0].amount.currency == "CHF"

    def test_balance_date(self, balances):
        # First transaction is 31.10.2023 -> balance directive on 01.11.2023
        assert balances[0].date == datetime.date(2023, 11, 1)

    def test_balance_account(self, balances):
        assert balances[0].account == _ACCOUNT

    def test_first_debit_transaction(self, transactions):
        # 31.10.2023, PREIS FÜR BANKPAKET SMART 09.2023, -63.94
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2023, 10, 31)
            and "BANKPAKET" in t.narration
        ]
        assert len(hits) == 1
        assert hits[0].postings[0].units.number == Decimal("-63.94")

    def test_credit_transaction(self, transactions):
        # 23.10.2023, credit of 0.65 — narration space-normalised
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2023, 10, 23)
            and t.postings[0].units.number == Decimal("0.65")
        ]
        assert len(hits) == 1
        narration = hits[0].narration
        # remove_spaces must have collapsed multiple spaces
        assert "  " not in narration
        assert "GUTSCHRIFT" in narration

    def test_all_postings_use_configured_account(self, transactions):
        assert all(t.postings[0].account == _ACCOUNT for t in transactions)

    def test_all_transactions_single_legged(self, transactions):
        assert all(len(t.postings) == 1 for t in transactions)

    def test_dates_within_statement_period(self, transactions):
        date_from = datetime.date(2023, 10, 1)
        date_to = datetime.date(2023, 10, 31)
        assert all(date_from <= t.date <= date_to for t in transactions)

    def test_multiple_transactions_same_date(self, transactions):
        # 28.10.2023 has 3 transactions in the fixture
        same_day = [t for t in transactions if t.date == datetime.date(2023, 10, 28)]
        assert len(same_day) == 3
