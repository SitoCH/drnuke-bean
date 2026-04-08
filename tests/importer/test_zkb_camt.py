"""Tests for ZKBCamtImporter (zkb_camt.py).

Unit tests build minimal CAMT.053 XML inline (no fixture file needed).
Integration tests parse the full real fixture: tests/fixtures/zkb/camt053.xml
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount.core import data

from drnukebean.importer.zkb_camt import ZKBCamtImporter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "zkb" / "camt053.xml"

_NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"
_IBAN = "CH5604835012345678009"
_ACCOUNT = "Assets:Bank:ZKB:CHF"


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _camt(
    *,
    iban: str = _IBAN,
    bal: str = "",
    entries: str = "",
    frtodt: str = "<FrToDt><FrDtTm>2024-01-01T00:00:00</FrDtTm></FrToDt>",
) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="{_NS}">
  <BkToCstmrStmt>
    <Stmt>
      <Acct><Id><IBAN>{iban}</IBAN></Id></Acct>
      {frtodt}
      {bal}
      {entries}
    </Stmt>
  </BkToCstmrStmt>
</Document>"""


def _write(tmp_path: Path, xml: str, name: str = "stmt.xml") -> str:
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return str(p)


def _importer(*, extra_meta: bool = False, balance_account: str | None = None) -> ZKBCamtImporter:
    return ZKBCamtImporter(
        iban=_IBAN,
        account=_ACCOUNT,
        balance_account=balance_account,
        extra_meta=extra_meta,
    )


def _clbd(
    amount: str = "1000.00", ccy: str = "CHF", cdt_dbt: str = "CRDT", date: str = "2024-01-31"
) -> str:
    return f"""
    <Bal>
      <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
      <Amt Ccy="{ccy}">{amount}</Amt>
      <CdtDbtInd>{cdt_dbt}</CdtDbtInd>
      <Dt><Dt>{date}</Dt></Dt>
    </Bal>"""


def _ntry(
    *,
    amount: str = "100.00",
    ccy: str = "CHF",
    cdt_dbt: str = "CRDT",
    booking_date: str = "2024-01-15",
    txdtls: str = "",
    cardtx: str = "",
    addtl: str = "",
    acct_svcr_ref: str = "",
) -> str:
    svcr = f"<AcctSvcrRef>{acct_svcr_ref}</AcctSvcrRef>" if acct_svcr_ref else ""
    addtl_el = f"<AddtlNtryInf>{addtl}</AddtlNtryInf>" if addtl else ""
    ntry_dtls = f"<NtryDtls>{txdtls}</NtryDtls>" if txdtls else ""
    return f"""
    <Ntry>
      <Amt Ccy="{ccy}">{amount}</Amt>
      <CdtDbtInd>{cdt_dbt}</CdtDbtInd>
      <BookgDt><Dt>{booking_date}</Dt></BookgDt>
      {svcr}
      {addtl_el}
      {ntry_dtls}
      {cardtx}
    </Ntry>"""


def _txdtls(
    *,
    amount: str = "100.00",
    ccy: str = "CHF",
    cdt_dbt: str = "CRDT",
    payee_nm: str = "Test Payee",
    payee_iban: str = "",
    narration: str = "",
    structured_narration: str = "",
    qrr_ref: str = "",
    ref: str = "",
    side: str = "CRDT",  # controls Dbtr vs Cdtr element
) -> str:
    # Counterparty: Dbtr for incoming (CRDT), Cdtr for outgoing (DBIT)
    if side == "CRDT":
        pty_xml = f"<Dbtr><Pty><Nm>{payee_nm}</Nm></Pty></Dbtr>"
        acct_xml = f"<DbtrAcct><Id><IBAN>{payee_iban}</IBAN></Id></DbtrAcct>" if payee_iban else ""
    else:
        pty_xml = f"<Cdtr><Pty><Nm>{payee_nm}</Nm></Pty></Cdtr>"
        acct_xml = f"<CdtrAcct><Id><IBAN>{payee_iban}</IBAN></Id></CdtrAcct>" if payee_iban else ""
    rmt = ""
    if structured_narration or qrr_ref:
        qrr_el = f"<CdtrRefInf><Ref>{qrr_ref}</Ref></CdtrRefInf>" if qrr_ref else ""
        addtl_el = (
            f"<AddtlRmtInf>{structured_narration}</AddtlRmtInf>" if structured_narration else ""
        )
        rmt = f"<RmtInf><Strd>{qrr_el}{addtl_el}</Strd></RmtInf>"
    elif narration:
        rmt = f"<RmtInf><Ustrd>{narration}</Ustrd></RmtInf>"
    ref_el = f"<Refs><AcctSvcrRef>{ref}</AcctSvcrRef></Refs>" if ref else ""
    return f"""
    <TxDtls>
      {ref_el}
      <Amt Ccy="{ccy}">{amount}</Amt>
      <CdtDbtInd>{cdt_dbt}</CdtDbtInd>
      <RltdPties>{pty_xml}{acct_xml}</RltdPties>
      {rmt}
    </TxDtls>"""


def _cardtx(poi_id: str = "TERM001") -> str:
    return f"<CardTx><POI><Id><Id>{poi_id}</Id></Id></POI></CardTx>"


# ===========================================================================
# identify()
# ===========================================================================


class TestIdentify:
    def test_match(self, tmp_path):
        path = _write(tmp_path, _camt())
        assert _importer().identify(path) is True

    def test_wrong_iban(self, tmp_path):
        path = _write(tmp_path, _camt(iban="CH5699999999999999999"))
        assert _importer().identify(path) is False

    def test_wrong_namespace(self, tmp_path):
        xml = '<?xml version="1.0"?><Document xmlns="urn:wrong:ns"><BkToCstmrStmt/></Document>'
        path = _write(tmp_path, xml)
        assert _importer().identify(path) is False

    def test_non_xml_extension(self, tmp_path):
        p = tmp_path / "stmt.csv"
        p.write_text("not xml")
        assert _importer().identify(str(p)) is False

    def test_malformed_xml(self, tmp_path):
        path = _write(tmp_path, "<broken xml")
        assert _importer().identify(path) is False


# ===========================================================================
# account() / date() / filename()
# ===========================================================================


class TestMetadata:
    def test_account(self, tmp_path):
        path = _write(tmp_path, _camt())
        assert _importer().account(path) == _ACCOUNT

    def test_date_returns_from_date(self, tmp_path):
        path = _write(tmp_path, _camt())
        assert _importer().date(path) == datetime.date(2024, 1, 1)

    def test_filename_format(self, tmp_path):
        path = _write(tmp_path, _camt())
        last4 = _IBAN[-4:]
        assert _importer().filename(path) == f"zkb_camt_2024-01-01_{last4}.xml"


# ===========================================================================
# Balance directive
# ===========================================================================


class TestBalance:
    def test_balance_date_is_day_after_statement(self, tmp_path):
        path = _write(tmp_path, _camt(bal=_clbd(date="2024-01-31")))
        entries = _importer().extract(path, [])
        bals = [e for e in entries if isinstance(e, data.Balance)]
        assert len(bals) == 1
        assert bals[0].date == datetime.date(2024, 2, 1)

    def test_balance_amount_credit(self, tmp_path):
        path = _write(tmp_path, _camt(bal=_clbd(amount="12345.67", cdt_dbt="CRDT")))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].amount.number == Decimal("12345.67")
        assert bals[0].amount.currency == "CHF"

    def test_balance_amount_debit_is_negative(self, tmp_path):
        path = _write(tmp_path, _camt(bal=_clbd(amount="500.00", cdt_dbt="DBIT")))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].amount.number == Decimal("-500.00")

    def test_balance_account_override(self, tmp_path):
        path = _write(tmp_path, _camt(bal=_clbd()))
        imp = ZKBCamtImporter(iban=_IBAN, account=_ACCOUNT, balance_account="Assets:Bank:ZKB")
        bals = [e for e in imp.extract(path, []) if isinstance(e, data.Balance)]
        assert bals[0].account == "Assets:Bank:ZKB"

    def test_no_clbd_no_balance(self, tmp_path):
        # OPBD balance should be ignored
        opbd = _clbd().replace("<Cd>CLBD</Cd>", "<Cd>OPBD</Cd>")
        path = _write(tmp_path, _camt(bal=opbd))
        bals = [e for e in _importer().extract(path, []) if isinstance(e, data.Balance)]
        assert len(bals) == 0


# ===========================================================================
# Transfer transactions — amounts
# ===========================================================================


class TestTransferAmounts:
    def test_credit_amount_positive(self, tmp_path):
        td = _txdtls(amount="1000.00", cdt_dbt="CRDT", side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(cdt_dbt="CRDT", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].postings[0].units.number == Decimal("1000.00")

    def test_debit_amount_negative(self, tmp_path):
        td = _txdtls(amount="500.00", cdt_dbt="DBIT", side="DBIT")
        path = _write(tmp_path, _camt(entries=_ntry(cdt_dbt="DBIT", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].postings[0].units.number == Decimal("-500.00")

    def test_txdtls_amount_takes_precedence_over_ntry(self, tmp_path):
        # Ntry has 300.00, TxDtls has 150.00 — TxDtls wins
        td = _txdtls(amount="150.00", cdt_dbt="CRDT", side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(amount="300.00", cdt_dbt="CRDT", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].postings[0].units.number == Decimal("150.00")


# ===========================================================================
# Transfer transactions — payees
# ===========================================================================


class TestTransferPayees:
    def test_payee_from_debtor_on_credit(self, tmp_path):
        td = _txdtls(cdt_dbt="CRDT", side="CRDT", payee_nm="Incoming Corp")
        path = _write(tmp_path, _camt(entries=_ntry(cdt_dbt="CRDT", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].payee == "Incoming Corp"

    def test_payee_from_creditor_on_debit(self, tmp_path):
        td = _txdtls(cdt_dbt="DBIT", side="DBIT", payee_nm="Outgoing Corp")
        path = _write(tmp_path, _camt(entries=_ntry(cdt_dbt="DBIT", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].payee == "Outgoing Corp"

    def test_posting_account_is_configured_account(self, tmp_path):
        td = _txdtls(side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].postings[0].account == _ACCOUNT


# ===========================================================================
# Transfer transactions — narration priority
# ===========================================================================


class TestTransferNarrations:
    def test_structured_addtl_rmt_inf_wins(self, tmp_path):
        td = _txdtls(structured_narration="Structured note", narration="Ustrd note", side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(addtl="Ntry note", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "Structured note"

    def test_ustrd_used_when_no_structured(self, tmp_path):
        td = _txdtls(narration="Unstructured note", side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(addtl="Ntry note", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "Unstructured note"

    def test_ntry_addtl_fallback(self, tmp_path):
        # TxDtls has no RmtInf at all
        td = "<TxDtls><CdtDbtInd>CRDT</CdtDbtInd></TxDtls>"
        path = _write(tmp_path, _camt(entries=_ntry(addtl="Entry level note", txdtls=td)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "Entry level note"


# ===========================================================================
# Batch (multiple TxDtls in one Ntry)
# ===========================================================================


class TestBatch:
    def test_two_txdtls_yield_two_transactions(self, tmp_path):
        td1 = _txdtls(amount="100.00", cdt_dbt="DBIT", side="DBIT", narration="Pay 1")
        td2 = _txdtls(amount="200.00", cdt_dbt="DBIT", side="DBIT", narration="Pay 2")
        path = _write(tmp_path, _camt(entries=_ntry(cdt_dbt="DBIT", txdtls=td1 + td2)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert len(txns) == 2

    def test_batch_amounts_are_independent(self, tmp_path):
        td1 = _txdtls(amount="100.00", cdt_dbt="DBIT", side="DBIT")
        td2 = _txdtls(amount="200.00", cdt_dbt="DBIT", side="DBIT")
        path = _write(tmp_path, _camt(entries=_ntry(cdt_dbt="DBIT", txdtls=td1 + td2)))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        amounts = {t.postings[0].units.number for t in txns}
        assert amounts == {Decimal("-100.00"), Decimal("-200.00")}


# ===========================================================================
# Card transactions (CardTx path)
# ===========================================================================


class TestCardTransaction:
    def test_card_txn_amount_negative(self, tmp_path):
        path = _write(
            tmp_path,
            _camt(
                entries=_ntry(
                    amount="75.30", cdt_dbt="DBIT", cardtx=_cardtx("TERM001"), addtl="Groceries"
                )
            ),
        )
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        assert txns[0].postings[0].units.number == Decimal("-75.30")

    def test_card_txn_payee_from_poi_id(self, tmp_path):
        path = _write(
            tmp_path, _camt(entries=_ntry(cdt_dbt="DBIT", cardtx=_cardtx("SUPERMARKET01")))
        )
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].payee == "SUPERMARKET01"

    def test_card_txn_narration_from_addtl_ntry_inf(self, tmp_path):
        path = _write(
            tmp_path,
            _camt(entries=_ntry(cdt_dbt="DBIT", cardtx=_cardtx(), addtl="Purchase at Coop")),
        )
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "Purchase at Coop"


# ===========================================================================
# Generic fallback (no TxDtls, no CardTx)
# ===========================================================================


class TestGenericFallback:
    def test_generic_entry_produces_transaction(self, tmp_path):
        path = _write(tmp_path, _camt(entries=_ntry(addtl="Generic note")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert len(txns) == 1

    def test_generic_entry_narration(self, tmp_path):
        path = _write(tmp_path, _camt(entries=_ntry(addtl="Some generic info")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].narration == "Some generic info"

    def test_generic_entry_payee_is_empty(self, tmp_path):
        path = _write(tmp_path, _camt(entries=_ntry(addtl="note")))
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert txns[0].payee == ""


# ===========================================================================
# extra_meta flag
# ===========================================================================


class TestExtraMeta:
    def test_ref_populated_for_transfer(self, tmp_path):
        td = _txdtls(ref="REF0001", side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(txdtls=td)))
        txns = [
            e
            for e in _importer(extra_meta=True).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert txns[0].meta.get("ref") == "REF0001"

    def test_counterparty_iban_populated(self, tmp_path):
        td = _txdtls(payee_iban="CH5699999999999999999", side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(txdtls=td)))
        txns = [
            e
            for e in _importer(extra_meta=True).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert txns[0].meta.get("counterparty_iban") == "CH5699999999999999999"

    def test_qrr_ref_populated(self, tmp_path):
        td = _txdtls(qrr_ref="RF18539007547034", structured_narration="Rent", side="DBIT")
        path = _write(tmp_path, _camt(entries=_ntry(cdt_dbt="DBIT", txdtls=td)))
        txns = [
            e
            for e in _importer(extra_meta=True).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert txns[0].meta.get("qrr_ref") == "RF18539007547034"

    def test_extra_meta_false_leaves_no_ref(self, tmp_path):
        td = _txdtls(ref="REF9999", side="CRDT")
        path = _write(tmp_path, _camt(entries=_ntry(txdtls=td)))
        txns = [
            e
            for e in _importer(extra_meta=False).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert "ref" not in txns[0].meta

    def test_card_ref_populated(self, tmp_path):
        path = _write(
            tmp_path,
            _camt(entries=_ntry(cdt_dbt="DBIT", cardtx=_cardtx(), acct_svcr_ref="CARDREF01")),
        )
        txns = [
            e
            for e in _importer(extra_meta=True).extract(path, [])
            if isinstance(e, data.Transaction)
        ]
        assert txns[0].meta.get("ref") == "CARDREF01"


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_empty_statement_yields_only_balance(self, tmp_path):
        path = _write(tmp_path, _camt(bal=_clbd()))
        entries = _importer().extract(path, [])
        txns = [e for e in entries if isinstance(e, data.Transaction)]
        bals = [e for e in entries if isinstance(e, data.Balance)]
        assert len(txns) == 0
        assert len(bals) == 1

    def test_malformed_xml_extract_returns_empty(self, tmp_path):
        path = _write(tmp_path, "<broken")
        assert _importer().extract(path, []) == []

    def test_missing_stmt_returns_empty(self, tmp_path):
        xml = f'<?xml version="1.0"?><Document xmlns="{_NS}"><BkToCstmrStmt/></Document>'
        path = _write(tmp_path, xml)
        assert _importer().extract(path, []) == []

    def test_signed_unexpected_value_treated_as_credit(self, tmp_path):
        # CdtDbtInd = "XXXX" is not CRDT or DBIT — should log warning and treat as credit
        xml = _camt(entries=_ntry(cdt_dbt="XXXX", addtl="odd entry"))
        path = _write(tmp_path, xml)
        txns = [e for e in _importer().extract(path, []) if isinstance(e, data.Transaction)]
        assert len(txns) == 1
        assert txns[0].postings[0].units.number > 0  # treated as credit (positive)


# ===========================================================================
# Integration tests — full fixture parsed by ZKBCamtImporter
# ===========================================================================


@pytest.fixture(scope="module")
def real_importer():
    return ZKBCamtImporter(iban=_IBAN, account=_ACCOUNT, extra_meta=True)


@pytest.fixture(scope="module")
def entries(real_importer):
    return real_importer.extract(str(FIXTURE), [])


@pytest.fixture(scope="module")
def transactions(entries):
    return [e for e in entries if isinstance(e, data.Transaction)]


@pytest.fixture(scope="module")
def balances(entries):
    return [e for e in entries if isinstance(e, data.Balance)]


class TestIntegration:
    def test_identify_real_fixture(self):
        imp = ZKBCamtImporter(iban=_IBAN, account=_ACCOUNT)
        assert imp.identify(str(FIXTURE)) is True

    def test_identify_wrong_iban_rejects_fixture(self):
        imp = ZKBCamtImporter(iban="CH5699999999999999999", account=_ACCOUNT)
        assert imp.identify(str(FIXTURE)) is False

    def test_balance_amount(self, balances):
        assert len(balances) == 1
        assert balances[0].amount.number == Decimal("12345.67")
        assert balances[0].amount.currency == "CHF"

    def test_balance_date_is_day_after_period_end(self, balances):
        assert balances[0].date == datetime.date(2024, 2, 1)

    def test_transaction_count(self, transactions):
        # 1 + 1 + 2 (batch) + 1 (card) + 1 (generic) = 6
        assert len(transactions) == 6

    def test_credit_transfer(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 10) and t.payee == "Example Payee A"
        ]
        assert len(hits) == 1
        assert hits[0].postings[0].units.number == Decimal("1000.00")
        assert hits[0].narration == "Salary January"

    def test_debit_transfer(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 15) and t.payee == "Example Payee B"
        ]
        assert len(hits) == 1
        assert hits[0].postings[0].units.number == Decimal("-500.50")
        assert hits[0].narration == "Rent payment"  # structured AddtlRmtInf wins

    def test_debit_transfer_qrr_ref(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 15) and t.payee == "Example Payee B"
        ]
        assert hits[0].meta.get("qrr_ref") == "RF18539007547034"

    def test_debit_transfer_counterparty_iban(self, transactions):
        hits = [
            t
            for t in transactions
            if t.date == datetime.date(2024, 1, 15) and t.payee == "Example Payee B"
        ]
        assert hits[0].meta.get("counterparty_iban") == "CH5611111111111111111"

    def test_batch_produces_two_transactions(self, transactions):
        batch = [t for t in transactions if t.date == datetime.date(2024, 1, 20)]
        assert len(batch) == 2

    def test_batch_amounts(self, transactions):
        batch = [t for t in transactions if t.date == datetime.date(2024, 1, 20)]
        amounts = {t.postings[0].units.number for t in batch}
        assert amounts == {Decimal("-100.00"), Decimal("-200.00")}

    def test_card_transaction(self, transactions):
        hits = [t for t in transactions if t.date == datetime.date(2024, 1, 22)]
        assert len(hits) == 1
        assert hits[0].payee == "TERM001"
        assert hits[0].postings[0].units.number == Decimal("-75.30")
        assert hits[0].narration == "Groceries"

    def test_generic_transaction(self, transactions):
        hits = [t for t in transactions if t.date == datetime.date(2024, 1, 25)]
        assert len(hits) == 1
        assert hits[0].postings[0].units.number == Decimal("50.00")
        assert hits[0].narration == "Generic payment"
