"""
Beancount importer for ZKB (Zürcher Kantonalbank) CAMT.053 XML bank statements.

Format: CAMT.053.001.08 (ISO 20022), Swiss SPS/2.2 variant.

Supported entry types:
  - Bank transfers (incoming and outgoing), single TxDtls per Ntry
  - Mobile banking batches (multiple TxDtls per Ntry) -> one transaction each
  - Visa Debit card transactions (CardTx element, no TxDtls)
  - Empty statements (balance directive only, no transactions)

Each transaction is single-legged (account posting only). The second leg is
expected to be filled by the deterministic fixes function and/or smart_importer
in the pipeline downstream.
"""

from __future__ import annotations

import functools
import warnings
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import beangulp
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.number import Decimal

_NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"
_NS_MAP = {"ns": _NS}


@functools.lru_cache(maxsize=64)
def _parse(filepath: str):
    """Parse and cache an XML file by path to avoid redundant parses within a run."""
    return ET.parse(filepath).getroot()


def _text(element, path: str, default: str = "") -> str:
    """Return normalised text of the first matching sub-element, or default.

    Internal whitespace (newlines, extra spaces) is collapsed to a single space.
    """
    el = element.find(path, _NS_MAP)
    if el is None or not el.text:
        return default
    return " ".join(el.text.split())


def _signed(number: Decimal, cdt_dbt: str) -> Decimal:
    """Apply sign convention: CRDT -> positive (money in), DBIT -> negative (money out)."""
    if cdt_dbt == "CRDT":
        return number
    if cdt_dbt == "DBIT":
        return -number
    warnings.warn(
        f"ZKBCamtImporter: unexpected CdtDbtInd value {cdt_dbt!r}; treating as credit",
        stacklevel=2,
    )
    return number


class ZKBCamtImporter(beangulp.Importer):
    """
    Beancount importer for ZKB CAMT.053 XML bank statements.

    Args:
        iban:             Account IBAN as configured in ZKB EBICS delivery (spaces/dashes ignored).
        account:          Beancount account for this ZKB account, e.g. 'Assets:Bank:ZKB:CHF'.
        balance_account:  Account for Balance directives. Defaults to `account`. Set to a parent
                          account (e.g. 'Assets:Bank:ZKB') when the ZKB account is split into
                          sub-accounts in the ledger and the bank statement balance covers the sum.
        currency:         Account currency, default 'CHF'.
        extra_meta:       If True, attach extra metadata to each transaction: ref (bank reference),
                          counterparty_iban, and qrr_ref where available. Default False.
    """

    def __init__(
        self,
        iban: str,
        account: str,
        balance_account: str | None = None,
        currency: str = "CHF",
        extra_meta: bool = False,
    ) -> None:
        self._iban = iban.replace(" ", "").replace("-", "")
        if not (15 <= len(self._iban) <= 34):
            warnings.warn(
                f"ZKBCamtImporter: IBAN {self._iban!r} has unusual length "
                f"({len(self._iban)}); check for typos",
                stacklevel=2,
            )
        self._account = account
        self._balance_account = (
            balance_account if balance_account is not None else account
        )
        self._currency = currency
        self._extra_meta = extra_meta

    # ------------------------------------------------------------------
    # beangulp interface
    # ------------------------------------------------------------------

    def identify(self, filepath: str) -> bool:
        """Match by CAMT.053 XML namespace and IBAN."""
        if Path(filepath).suffix.lower() != ".xml":
            return False
        try:
            root = _parse(filepath)
            if _NS not in root.tag:
                return False
            stmt_iban = _text(root, "ns:BkToCstmrStmt/ns:Stmt/ns:Acct/ns:Id/ns:IBAN")
            return stmt_iban == self._iban
        except ET.ParseError:
            return False

    def account(self, filepath: str) -> str:
        return self._account

    def date(self, filepath: str) -> date | None:
        """Return the statement from-date (FrDtTm)."""
        try:
            root = _parse(filepath)
            s = _text(root, "ns:BkToCstmrStmt/ns:Stmt/ns:FrToDt/ns:FrDtTm")
            return date.fromisoformat(s[:10]) if s else None
        except (ET.ParseError, ValueError):
            return None

    def filename(self, filepath: str) -> str | None:
        """Suggest an archive filename: zkb_camt_<date>_<last4iban>.xml"""
        stmt_date = self.date(filepath)
        suffix = self._iban[-4:]
        return f"zkb_camt_{stmt_date}_{suffix}.xml" if stmt_date else None

    def extract(self, filepath: str, existing: list) -> list:
        try:
            root = _parse(filepath)
        except ET.ParseError as exc:
            warnings.warn(
                f"ZKBCamtImporter: XML parse error in {filepath}: {exc}", stacklevel=2
            )
            return []

        stmt = root.find("ns:BkToCstmrStmt/ns:Stmt", _NS_MAP)
        if stmt is None:
            return []

        entries: list = []
        entries.extend(self._balances(stmt, filepath))
        for index, ntry in enumerate(stmt.findall("ns:Ntry", _NS_MAP)):
            entries.extend(self._entry(ntry, filepath, index))
        return entries

    # ------------------------------------------------------------------
    # Balance extraction
    # ------------------------------------------------------------------

    def _balances(self, stmt, filepath: str) -> list:
        """Emit a single Balance directive for the first closing balance (CLBD).

        OPBD and CLAV entries are intentionally ignored; only CLBD is used.
        """
        for bal in stmt.findall("ns:Bal", _NS_MAP):
            if _text(bal, "ns:Tp/ns:CdOrPrtry/ns:Cd") != "CLBD":
                continue
            amt_el = bal.find("ns:Amt", _NS_MAP)
            if amt_el is None or not amt_el.text:
                continue
            date_str = _text(bal, "ns:Dt/ns:Dt")
            if not date_str:
                continue
            try:
                bal_date = date.fromisoformat(date_str) + timedelta(days=1)
            except ValueError:
                continue
            number = _signed(Decimal(amt_el.text.strip()), _text(bal, "ns:CdtDbtInd"))
            currency = amt_el.attrib.get("Ccy", self._currency)
            return [
                data.Balance(
                    data.new_metadata(filepath, 0),
                    bal_date,
                    self._balance_account,
                    Amount(number, currency),
                    None,
                    None,
                )
            ]
        return []

    # ------------------------------------------------------------------
    # Entry dispatch
    # ------------------------------------------------------------------

    def _entry(self, ntry, filepath: str, index: int) -> list:
        """Dispatch a single <Ntry> to the appropriate transaction builder."""
        try:
            if ntry.find("ns:CardTx", _NS_MAP) is not None:
                return [self._card_txn(ntry, filepath, index)]
            tx_details = ntry.findall("ns:NtryDtls/ns:TxDtls", _NS_MAP)
            if tx_details:
                return [
                    self._transfer_txn(ntry, txd, filepath, index) for txd in tx_details
                ]
            return [self._generic_txn(ntry, filepath, index)]
        except (ValueError, AttributeError) as exc:
            warnings.warn(
                f"ZKBCamtImporter: skipping entry {index} in {filepath}: {exc}",
                stacklevel=2,
            )
            return []

    # ------------------------------------------------------------------
    # Transaction builders
    # ------------------------------------------------------------------

    def _ntry_amount(self, ntry) -> tuple[Decimal, str, str]:
        """Return (number, currency, cdt_dbt) from the Ntry-level <Amt>."""
        amt_el = ntry.find("ns:Amt", _NS_MAP)
        if amt_el is None or not amt_el.text:
            raise ValueError("missing <Amt> in <Ntry>")
        currency = amt_el.attrib.get("Ccy", self._currency)
        return Decimal(amt_el.text.strip()), currency, _text(ntry, "ns:CdtDbtInd")

    def _booking_date(self, ntry) -> date:
        date_str = _text(ntry, "ns:BookgDt/ns:Dt")
        if not date_str:
            raise ValueError("missing <BookgDt/Dt> in <Ntry>")
        return date.fromisoformat(date_str)

    def _transfer_txn(self, ntry, txd, filepath: str, index: int) -> data.Transaction:
        """Build one transaction from a <TxDtls> element (bank transfer)."""
        # Amount: prefer TxDtls level, fall back to Ntry level
        amt_el = txd.find("ns:Amt", _NS_MAP)
        if amt_el is not None and amt_el.text:
            number = Decimal(amt_el.text.strip())
            currency = amt_el.attrib.get("Ccy", self._currency)
            cdt_dbt = _text(txd, "ns:CdtDbtInd")
        else:
            number, currency, cdt_dbt = self._ntry_amount(ntry)

        # Counterparty: creditor for outgoing (DBIT), debtor for incoming (CRDT)
        if cdt_dbt == "DBIT":
            payee = _text(txd, "ns:RltdPties/ns:Cdtr/ns:Pty/ns:Nm")
            cp_iban = _text(txd, "ns:RltdPties/ns:CdtrAcct/ns:Id/ns:IBAN")
        else:
            payee = _text(txd, "ns:RltdPties/ns:Dbtr/ns:Pty/ns:Nm")
            cp_iban = _text(txd, "ns:RltdPties/ns:DbtrAcct/ns:Id/ns:IBAN")

        # Narration priority: structured AddtlRmtInf > unstructured Ustrd > entry-level AddtlNtryInf
        narration = (
            _text(txd, "ns:RmtInf/ns:Strd/ns:AddtlRmtInf")
            or _text(txd, "ns:RmtInf/ns:Ustrd")
            or _text(ntry, "ns:AddtlNtryInf")
        )

        meta = data.new_metadata(filepath, index)
        if self._extra_meta:
            ref = _text(txd, "ns:Refs/ns:AcctSvcrRef") or _text(ntry, "ns:AcctSvcrRef")
            qrr_ref = _text(txd, "ns:RmtInf/ns:Strd/ns:CdtrRefInf/ns:Ref")
            if ref:
                meta["ref"] = ref
            if cp_iban:
                meta["counterparty_iban"] = cp_iban
            if qrr_ref:
                meta["qrr_ref"] = qrr_ref

        return data.Transaction(
            meta,
            self._booking_date(ntry),
            "*",
            payee,
            narration,
            data.EMPTY_SET,
            data.EMPTY_SET,
            [
                data.Posting(
                    self._account,
                    Amount(_signed(number, cdt_dbt), currency),
                    None,
                    None,
                    None,
                    None,
                )
            ],
        )

    def _card_txn(self, ntry, filepath: str, index: int) -> data.Transaction:
        """Build a transaction from a <CardTx> entry (Visa Debit card purchase)."""
        number, currency, cdt_dbt = self._ntry_amount(ntry)
        payee = _text(ntry, "ns:CardTx/ns:POI/ns:Id/ns:Id")
        narration = _text(ntry, "ns:AddtlNtryInf")

        meta = data.new_metadata(filepath, index)
        if self._extra_meta:
            ref = _text(ntry, "ns:AcctSvcrRef")
            if ref:
                meta["ref"] = ref

        return data.Transaction(
            meta,
            self._booking_date(ntry),
            "*",
            payee,
            narration,
            data.EMPTY_SET,
            data.EMPTY_SET,
            [
                data.Posting(
                    self._account,
                    Amount(_signed(number, cdt_dbt), currency),
                    None,
                    None,
                    None,
                    None,
                )
            ],
        )

    def _generic_txn(self, ntry, filepath: str, index: int) -> data.Transaction:
        """Fallback for entries with neither TxDtls nor CardTx."""
        number, currency, cdt_dbt = self._ntry_amount(ntry)
        narration = _text(ntry, "ns:AddtlNtryInf")

        meta = data.new_metadata(filepath, index)
        if self._extra_meta:
            ref = _text(ntry, "ns:AcctSvcrRef")
            if ref:
                meta["ref"] = ref

        return data.Transaction(
            meta,
            self._booking_date(ntry),
            "*",
            "",
            narration,
            data.EMPTY_SET,
            data.EMPTY_SET,
            [
                data.Posting(
                    self._account,
                    Amount(_signed(number, cdt_dbt), currency),
                    None,
                    None,
                    None,
                    None,
                )
            ],
        )
