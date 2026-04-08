"""Tests for SBBImporter (sbb.py).

Unit tests use inline CSV strings written to tmp_path — no fixture file needed.
Integration tests parse the full fixture: tests/fixtures/sbb/sbb_fixture.csv
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data

from drnukebean.importer.sbb import HALBTAX_PLUS, SBBImporter, _col, _parse_date

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sbb" / "sbb_fixture.csv"

_HALBTAX = "Assets:Prepaid:HalbtaxPlus"
_BANK = "Assets:Bank:ZKB:CHF"
_EXPENSES = "Expenses:Transport:SBB"

# ---------------------------------------------------------------------------
# CSV building blocks
# ---------------------------------------------------------------------------

_EN_HEADER = (
    "Tariff,Route,Via (optional),Price,Co-passenger(s),"
    "Travel date,Validity,Order date,Order number,Payment methods,Purchaser e-mail\n"
)
_DE_HEADER = (
    "Tarif,Strecke,Via (optional),Preis,Mitreisende,"
    "Reisedatum,Gültigkeit,Bestelldatum,Bestellnummer,Zahlungsmittel,E-Mail Käufer:in\n"
)


def _en_row(
    tariff: str = "ZVV 24h-Ticket",
    route: str = "Zürich HB -> Bülach",
    via: str = "Opfikon",
    price: str = "8.60",
    copassenger: str = "testuser",
    travel_date: str = "10.01.2026",
    validity: str = "10.01.2026 00:00 - 10.01.2026 23:59",
    order_date: str = "10.01.2026",
    order_number: str = "111222333444",
    payment: str = "Half Fare Card PLUS",
    email: str = "test@example.com",
) -> str:
    return (
        f"{tariff},{route},{via},{price},{copassenger},"
        f"{travel_date},{validity},{order_date},{order_number},{payment},{email}\n"
    )


def _de_row(
    tarif: str = "ZVV 24h-Ticket",
    strecke: str = "Zürich HB -> Bülach",
    via: str = "Opfikon",
    preis: str = "8.60",
    mitreisende: str = "testuser",
    reisedatum: str = "10.01.2026",
    gueltigkeit: str = "10.01.2026 00:00 - 10.01.2026 23:59",
    bestelldatum: str = "10.01.2026",
    bestellnummer: str = "111222333444",
    zahlungsmittel: str = "Halbtax PLUS",
    email: str = "test@example.com",
) -> str:
    return (
        f"{tarif},{strecke},{via},{preis},{mitreisende},"
        f"{reisedatum},{gueltigkeit},{bestelldatum},{bestellnummer},{zahlungsmittel},{email}\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _importer(*, bank: str = _BANK, expenses: str = _EXPENSES) -> SBBImporter:
    return SBBImporter(
        account_halbtax=_HALBTAX,
        account_bank=bank,
        account_expenses=expenses,
    )


def _write(tmp_path: Path, content: str, name: str = "sbb.csv") -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


# ===========================================================================
# _col() helper
# ===========================================================================


class TestColHelper:
    def test_first_key_found(self):
        assert _col({"Tarif": "A", "Tariff": "B"}, "Tarif", "Tariff") == "A"

    def test_second_key_fallback(self):
        assert _col({"Tariff": "B"}, "Tarif", "Tariff") == "B"

    def test_no_key_returns_empty_string(self):
        assert _col({"Other": "X"}, "Tarif", "Tariff") == ""

    def test_empty_row(self):
        assert _col({}, "Tarif") == ""


# ===========================================================================
# _parse_date() helper
# ===========================================================================


class TestParseDateHelper:
    def test_standard_format(self):
        assert _parse_date("10.01.2026") == datetime.date(2026, 1, 10)

    def test_strips_leading_trailing_whitespace(self):
        assert _parse_date("  29.03.2026  ") == datetime.date(2026, 3, 29)

    def test_end_of_year(self):
        assert _parse_date("31.12.2025") == datetime.date(2025, 12, 31)

    def test_bad_format_raises(self):
        with pytest.raises(ValueError):
            _parse_date("2026-01-10")

    def test_nonsense_raises(self):
        with pytest.raises(ValueError):
            _parse_date("not-a-date")


# ===========================================================================
# HALBTAX_PLUS constant
# ===========================================================================


class TestHalbtaxPlusConstant:
    def test_halbtax_plus_in_set(self):
        assert "Halbtax PLUS" in HALBTAX_PLUS

    def test_half_fare_card_plus_in_set(self):
        assert "Half Fare Card PLUS" in HALBTAX_PLUS

    def test_unknown_method_not_in_set(self):
        assert "VISA" not in HALBTAX_PLUS
        assert "Mastercard" not in HALBTAX_PLUS


# ===========================================================================
# identify()
# ===========================================================================


class TestIdentify:
    def test_en_headers_match(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row())
        assert _importer().identify(path) is True

    def test_de_headers_match(self, tmp_path):
        path = _write(tmp_path, _DE_HEADER + _de_row())
        assert _importer().identify(path) is True

    def test_wrong_content_rejected(self, tmp_path):
        path = _write(tmp_path, "Date,Description,Amount\n01.01.2026,Coffee,4.50\n")
        assert _importer().identify(path) is False

    def test_empty_file_rejected(self, tmp_path):
        path = _write(tmp_path, "")
        assert _importer().identify(path) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert _importer().identify(str(tmp_path / "nonexistent.csv")) is False

    def test_partial_en_headers_rejected(self, tmp_path):
        # Only one of the three fingerprint columns present
        path = _write(tmp_path, "Tariff,Something\n")
        assert _importer().identify(path) is False

    def test_utf8_bom_handled(self, tmp_path):
        # BOM-prefixed file must still be identified correctly
        p = tmp_path / "sbb_bom.csv"
        p.write_bytes(b"\xef\xbb\xbf" + (_EN_HEADER + _en_row()).encode("utf-8"))
        assert _importer().identify(str(p)) is True


# ===========================================================================
# account() / filename() / name
# ===========================================================================


class TestMetadata:
    def test_account_returns_halbtax(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER)
        assert _importer().account(path) == _HALBTAX

    def test_account_ignores_filepath(self):
        # account() should return the configured account regardless of path
        assert _importer().account(None) == _HALBTAX

    def test_filename_always_sbb_csv(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row())
        assert _importer().filename(path) == "sbb.csv"

    def test_name_contains_halbtax_account(self):
        assert _HALBTAX in _importer().name


# ===========================================================================
# date()
# ===========================================================================


class TestDate:
    def test_returns_max_order_date(self, tmp_path):
        content = (
            _EN_HEADER
            + _en_row(order_date="10.01.2026")
            + _en_row(order_date="29.03.2026")
            + _en_row(order_date="05.02.2026")
        )
        path = _write(tmp_path, content)
        assert _importer().date(path) == datetime.date(2026, 3, 29)

    def test_single_row(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(order_date="15.02.2026"))
        assert _importer().date(path) == datetime.date(2026, 2, 15)

    def test_empty_body_returns_none(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER)
        assert _importer().date(path) is None

    def test_de_header_order_date_parsed(self, tmp_path):
        path = _write(tmp_path, _DE_HEADER + _de_row(bestelldatum="20.03.2026"))
        assert _importer().date(path) == datetime.date(2026, 3, 20)


# ===========================================================================
# extract() — Halbtax PLUS payment
# ===========================================================================


class TestExtractHalbtax:
    @pytest.fixture
    def txn(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(payment="Half Fare Card PLUS"))
        return _importer().extract(path, [])[0]

    def test_produces_one_transaction(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(payment="Half Fare Card PLUS"))
        entries = _importer().extract(path, [])
        assert len(entries) == 1
        assert isinstance(entries[0], data.Transaction)

    def test_flag_is_cleared(self, txn):
        assert txn.flag == "*"

    def test_payee_is_sbb(self, txn):
        assert txn.payee == "SBB"

    def test_date_is_order_date(self, txn):
        assert txn.date == datetime.date(2026, 1, 10)

    def test_two_postings(self, txn):
        assert len(txn.postings) == 2

    def test_counter_posting_account_is_halbtax(self, txn):
        assert txn.postings[0].account == _HALBTAX

    def test_counter_posting_amount_negative(self, txn):
        assert txn.postings[0].units.number == Decimal("-8.60")
        assert txn.postings[0].units.currency == "CHF"

    def test_expense_posting_account(self, txn):
        assert txn.postings[1].account == _EXPENSES

    def test_expense_posting_amount_is_none(self, txn):
        assert txn.postings[1].units is None

    def test_halbtax_plus_spelling_also_works(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(payment="Halbtax PLUS"))
        txn = _importer().extract(path, [])[0]
        assert txn.postings[0].account == _HALBTAX

    def test_de_halbtax_plus(self, tmp_path):
        path = _write(tmp_path, _DE_HEADER + _de_row(zahlungsmittel="Halbtax PLUS"))
        txn = _importer().extract(path, [])[0]
        assert txn.postings[0].account == _HALBTAX


# ===========================================================================
# extract() — Bank payment (unknown payment method, bank configured)
# ===========================================================================


class TestExtractBank:
    @pytest.fixture
    def txn(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(payment="VISA", price="43.00"))
        return _importer(bank=_BANK).extract(path, [])[0]

    def test_flag_is_cleared(self, txn):
        assert txn.flag == "*"

    def test_two_postings(self, txn):
        assert len(txn.postings) == 2

    def test_counter_account_is_bank(self, txn):
        assert txn.postings[0].account == _BANK

    def test_counter_posting_amount_negative(self, txn):
        assert txn.postings[0].units.number == Decimal("-43.00")

    def test_expense_posting_account(self, txn):
        assert txn.postings[1].account == _EXPENSES

    def test_expense_posting_amount_is_none(self, txn):
        assert txn.postings[1].units is None


# ===========================================================================
# extract() — No bank configured (single-legged, flag '!')
# ===========================================================================


class TestExtractNoBank:
    @pytest.fixture
    def txn(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(payment="VISA", price="43.00"))
        return _importer(bank="").extract(path, [])[0]

    def test_flag_is_exclamation(self, txn):
        assert txn.flag == "!"

    def test_no_postings(self, txn):
        assert len(txn.postings) == 0


# ===========================================================================
# extract() — Metadata fields
# ===========================================================================


class TestExtractNarration:
    def test_narration_is_route_when_dates_equal(self, tmp_path):
        # travel_date == order_date -> no date suffix
        path = _write(
            tmp_path,
            _EN_HEADER
            + _en_row(
                route="Zürich HB -> Bülach", travel_date="10.01.2026", order_date="10.01.2026"
            ),
        )
        txn = _importer().extract(path, [])[0]
        assert txn.narration == "Zürich HB -> Bülach"

    def test_narration_appends_travel_date_when_different(self, tmp_path):
        # travel_date != order_date -> date appended after comma
        path = _write(
            tmp_path,
            _EN_HEADER
            + _en_row(
                route="Zürich HB -> München Hbf", travel_date="14.01.2026", order_date="10.01.2026"
            ),
        )
        txn = _importer().extract(path, [])[0]
        assert txn.narration == "Zürich HB -> München Hbf, 14.01.2026"

    def test_narration_no_date_suffix_when_same(self, tmp_path):
        # Ensure no trailing comma or date when dates match
        path = _write(
            tmp_path,
            _EN_HEADER
            + _en_row(route="Bern -> Zürich", travel_date="05.03.2026", order_date="05.03.2026"),
        )
        txn = _importer().extract(path, [])[0]
        assert txn.narration == "Bern -> Zürich"

    def test_empty_route_date_different_gives_tariff_with_date(self, tmp_path):
        # Day Pass: no route -> tariff used; travel_date differs -> date appended
        path = _write(
            tmp_path,
            _EN_HEADER
            + _en_row(
                tariff="ZVV 24h-Ticket",
                route="",
                via="",
                travel_date="07.03.2026",
                order_date="06.03.2026",
            ),
        )
        txn = _importer().extract(path, [])[0]
        assert txn.narration == "ZVV 24h-Ticket, 07.03.2026"

    def test_de_narration_date_appended(self, tmp_path):
        # DE column names; travel date differs -> suffix applied
        path = _write(
            tmp_path,
            _DE_HEADER
            + _de_row(strecke="Bern -> Zürich", reisedatum="05.03.2026", bestelldatum="10.01.2026"),
        )
        txn = _importer().extract(path, [])[0]
        assert txn.narration == "Bern -> Zürich, 05.03.2026"


# ===========================================================================
# extract() — Edge cases
# ===========================================================================


class TestExtractEdgeCases:
    def test_empty_route_uses_tariff_as_narration(self, tmp_path):
        # Day Pass rows have no Route -> tariff used as narration fallback
        path = _write(
            tmp_path,
            _EN_HEADER + _en_row(route="", via="", tariff="Day Pass for the Half Fare Travelcard"),
        )
        txn = _importer().extract(path, [])[0]
        assert txn.narration == "Day Pass for the Half Fare Travelcard"

    def test_price_with_apostrophe_separator(self, tmp_path):
        # Swiss locale uses apostrophe as thousands separator
        path = _write(tmp_path, _EN_HEADER + _en_row(price="1'234.00"))
        txn = _importer().extract(path, [])[0]
        assert txn.postings[0].units.number == Decimal("-1234.00")

    def test_bad_date_emits_note_not_crash(self, tmp_path):
        path = _write(
            tmp_path,
            _EN_HEADER + _en_row(order_date="not-a-date"),
        )
        entries = _importer().extract(path, [])
        assert len(entries) == 1
        assert isinstance(entries[0], data.Note)

    def test_bad_price_emits_note_not_crash(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(price="n/a"))
        entries = _importer().extract(path, [])
        assert len(entries) == 1
        assert isinstance(entries[0], data.Note)

    def test_note_references_halbtax_account(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(order_date="bad"))
        note = _importer().extract(path, [])[0]
        assert isinstance(note, data.Note)
        assert note.account == _HALBTAX

    def test_good_rows_after_bad_row_still_extracted(self, tmp_path):
        content = (
            _EN_HEADER
            + _en_row(order_date="bad-date")  # bad row -> Note
            + _en_row(order_date="15.01.2026")  # good row -> Transaction
        )
        path = _write(tmp_path, content)
        entries = _importer().extract(path, [])
        assert len(entries) == 2
        assert isinstance(entries[0], data.Note)
        assert isinstance(entries[1], data.Transaction)

    def test_transaction_date_uses_order_date_not_travel_date(self, tmp_path):
        path = _write(
            tmp_path,
            _EN_HEADER + _en_row(travel_date="01.01.2026", order_date="31.12.2025"),
        )
        txn = _importer().extract(path, [])[0]
        assert txn.date == datetime.date(2025, 12, 31)

    def test_empty_file_returns_empty_list(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER)
        assert _importer().extract(path, []) == []

    def test_whitespace_trimmed_from_route(self, tmp_path):
        path = _write(tmp_path, _EN_HEADER + _en_row(route="  Zürich -> Bern  "))
        txn = _importer().extract(path, [])[0]
        assert txn.narration == "Zürich -> Bern"


# ===========================================================================
# Integration — full fixture
# ===========================================================================


@pytest.fixture(scope="module")
def _real_importer() -> SBBImporter:
    return SBBImporter(
        account_halbtax=_HALBTAX,
        account_bank=_BANK,
        account_expenses=_EXPENSES,
    )


@pytest.fixture(scope="module")
def _all_entries(_real_importer) -> list:
    return _real_importer.extract(str(FIXTURE), [])


@pytest.fixture(scope="module")
def _transactions(_all_entries) -> list[data.Transaction]:
    return [e for e in _all_entries if isinstance(e, data.Transaction)]


class TestIntegration:
    def test_identify_en_fixture(self):
        assert _importer().identify(str(FIXTURE)) is True

    def test_identify_wrong_file_rejects_fixture(self, tmp_path):
        path = _write(tmp_path, "garbage,data\n1,2\n")
        assert _importer().identify(path) is False

    def test_date_returns_max_order_date(self):
        assert _importer().date(str(FIXTURE)) == datetime.date(2024, 3, 29)

    def test_transaction_count(self, _transactions):
        # Fixture has 32 data rows, all parseable
        assert len(_transactions) == 32

    def test_no_notes_emitted(self, _all_entries):
        notes = [e for e in _all_entries if isinstance(e, data.Note)]
        assert len(notes) == 0

    def test_halbtax_transaction_count(self, _transactions):
        # 24 rows have "Half Fare Card PLUS"
        halbtax_txns = [t for t in _transactions if t.postings[0].account == _HALBTAX]
        assert len(halbtax_txns) == 24

    def test_bank_transaction_count(self, _transactions):
        # 8 rows have unknown payment methods
        bank_txns = [t for t in _transactions if t.postings[0].account == _BANK]
        assert len(bank_txns) == 8

    def test_all_transactions_two_postings(self, _transactions):
        assert all(len(t.postings) == 2 for t in _transactions)

    def test_all_payees_are_sbb(self, _transactions):
        assert all(t.payee == "SBB" for t in _transactions)

    def test_all_currencies_chf(self, _transactions):
        for txn in _transactions:
            for posting in txn.postings:
                if posting.units is not None:
                    assert posting.units.currency == "CHF"

    def test_first_row_amount(self, _transactions):
        # First fixture row: order_date=29.03.2024, price=24.80, Half Fare Card PLUS
        hit = [t for t in _transactions if t.date == datetime.date(2024, 3, 29)]
        assert len(hit) == 1
        assert hit[0].postings[0].units.number == Decimal("-24.80")
        assert hit[0].postings[0].account == _HALBTAX

    def test_day_pass_row(self, _transactions):
        # Day Pass row: order_date=06.03.2024, price=86.00, empty route -> tariff used,
        # travel_date=07.03.2024 (differs) -> date appended
        hit = [t for t in _transactions if t.date == datetime.date(2024, 3, 6)]
        assert len(hit) == 1
        assert hit[0].postings[0].units.number == Decimal("-86.00")
        assert hit[0].narration == "Day Pass for the Half Fare Travelcard, 07.03.2024"

    def test_non_halbtax_row_uses_bank(self, _transactions):
        # Row on 15.03.2024: test_paymen_method -> bank account
        hit = [t for t in _transactions if t.date == datetime.date(2024, 3, 15)]
        assert len(hit) == 1
        assert hit[0].postings[0].account == _BANK

    def test_narration_no_extra_date_when_same(self, _transactions):
        # Row on 29.03.2024: travel_date == order_date -> no date suffix in narration
        hit = [t for t in _transactions if t.date == datetime.date(2024, 3, 29)]
        txn = hit[0]
        assert "," not in txn.narration
