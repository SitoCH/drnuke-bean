"""Unit tests for drnukebean.pipeline.runner.

Strategy:
  - Pure helpers (_parse_month, _last_month, etc.): direct calls.
  - sys.argv-dependent helpers: monkeypatch sys.argv.
  - File I/O: tmp_path fixture.
  - beancount.loader: mocked via mocker.patch.
  - No real network calls; StubImporter replaces all real importers.

The FixesWrapper and _run_entry / run_all tests use StubImporter + tmp_path
to exercise the full orchestration path without any real parser or bank API.
"""

from __future__ import annotations

import datetime
import sys
from decimal import Decimal
from pathlib import Path

import beangulp
from beancount.core import data
from beancount.core.amount import Amount

from drnukebean.pipeline.runner import (
    FixesWrapper,
    _append_entries,
    _build_wrapped,
    _last_month,
    _load_existing,
    _make_dry_path,
    _move_statements,
    _resolve_date_range,
    run_all,
)

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _txn(narration: str = "Test", date: datetime.date = datetime.date(2024, 1, 15)):
    return data.Transaction(
        meta={"filename": "", "lineno": 0},
        date=date,
        flag="*",
        payee=None,
        narration=narration,
        tags=frozenset(),
        links=frozenset(),
        postings=[],
    )


def _balance():
    return data.Balance(
        meta={"filename": "", "lineno": 0},
        date=datetime.date(2024, 2, 1),
        account="Assets:Bank:ZKB:CHF",
        amount=Amount(Decimal("10000"), "CHF"),
        tolerance=None,
        diff_amount=None,
    )


class StubImporter(beangulp.Importer):
    """Minimal beangulp.Importer for pipeline tests."""

    def __init__(
        self,
        account: str = "Assets:Test",
        pattern: str = "stub",
        entries: list | None = None,
        filename_override: str | None = None,
    ) -> None:
        self._account = account
        self._pattern = pattern
        self._entries = entries if entries is not None else [_txn()]
        self._filename_override = filename_override

    def identify(self, filepath: str) -> bool:
        return self._pattern in filepath

    def account(self, filepath: str) -> str:
        return self._account

    def date(self, filepath: str):
        return datetime.date(2024, 1, 31)

    def filename(self, filepath: str):
        return self._filename_override

    def extract(self, filepath: str, existing: list) -> list:
        return list(self._entries)


def _pipeline(
    tmp_path: Path,
    name: str = "stub",
    pattern: str = "stub",
    account: str = "Assets:Test",
    entries: list | None = None,
    setup: object = None,
    fixes: object = None,
) -> dict:
    """Build a minimal pipeline dict backed by a tmp_path source dir."""
    source_dir = tmp_path / name / "source"
    source_dir.mkdir(parents=True)
    bean_output_file = tmp_path / name / "output.bean"
    importer = StubImporter(account=account, pattern=pattern, entries=entries)
    d: dict = {
        "name": name,
        "importer": importer,
        "source_dir": str(source_dir),
        "bean_output_file": str(bean_output_file),
    }
    if setup is not None:
        d["setup"] = setup
    if fixes is not None:
        d["fixes"] = fixes
    return d


# ===========================================================================
# FixesWrapper
# ===========================================================================


class TestFixesWrapper:
    def test_fixes_applied_to_transaction(self):
        def tag_it(txn):
            return txn._replace(narration="FIXED: " + txn.narration)

        inner = StubImporter(entries=[_txn("Original")])
        wrapped = FixesWrapper(inner, tag_it)
        result = wrapped.extract("stub.xml", [])
        assert result[0].narration == "FIXED: Original"

    def test_non_transaction_passes_through_unchanged(self):
        bal = _balance()
        inner = StubImporter(entries=[_txn(), bal])
        wrapped = FixesWrapper(inner, lambda e: e._replace(narration="MUTATED"))
        result = wrapped.extract("stub.xml", [])
        assert any(isinstance(e, data.Balance) for e in result)
        bal_out = next(e for e in result if isinstance(e, data.Balance))
        assert bal_out == bal  # unchanged

    def test_identify_delegates(self):
        inner = StubImporter(pattern="myfile.xml")
        wrapped = FixesWrapper(inner, lambda e: e)
        assert wrapped.identify("myfile.xml") is True
        assert wrapped.identify("other.xml") is False

    def test_account_delegates(self):
        inner = StubImporter(account="Assets:Bank:ZKB:CHF")
        wrapped = FixesWrapper(inner, lambda e: e)
        assert wrapped.account("f.xml") == "Assets:Bank:ZKB:CHF"

    def test_date_delegates(self):
        inner = StubImporter()
        wrapped = FixesWrapper(inner, lambda e: e)
        assert wrapped.date("f.xml") == datetime.date(2024, 1, 31)


# ===========================================================================
# Pure date helpers
# ===========================================================================


class TestLastMonth:
    def test_returns_first_day_of_month(self):
        d = _last_month()
        assert d.day == 1

    def test_returns_date_in_past(self):
        assert _last_month() < datetime.date.today()

    def test_is_exactly_one_month_before_this_month(self):
        today = datetime.date.today()
        first_this = today.replace(day=1)
        last = _last_month()
        # Advancing last month by its days-in-month must land on first_this
        import calendar

        days_in_last = calendar.monthrange(last.year, last.month)[1]
        assert last + datetime.timedelta(days=days_in_last) == first_this


# ===========================================================================
# CLI argument parsing
# ===========================================================================


class TestResolveDateRange:
    def test_both_none_default_to_last_month(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        d_from, d_to = _resolve_date_range(None, None)
        expected = _last_month()
        assert d_from == expected
        assert d_to == expected

    def test_explicit_programmatic_range(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        d_from, d_to = _resolve_date_range(datetime.date(2024, 1, 1), datetime.date(2024, 3, 1))
        assert d_from == datetime.date(2024, 1, 1)
        assert d_to == datetime.date(2024, 3, 1)

    def test_from_only_sets_to_equal_from(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        d_from, d_to = _resolve_date_range(datetime.date(2024, 2, 1), None)
        assert d_from == d_to == datetime.date(2024, 2, 1)

    def test_cli_overrides_programmatic(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py", "--from", "2025-06"])
        d_from, d_to = _resolve_date_range(datetime.date(2024, 1, 1), datetime.date(2024, 1, 1))
        assert d_from == datetime.date(2025, 6, 1)


# ===========================================================================
# _make_dry_path
# ===========================================================================


class TestMakeDryPath:
    def test_creates_temp_file_when_none(self):
        p = _make_dry_path(None)
        assert p.exists()
        p.unlink()

    def test_returns_given_path(self, tmp_path):
        given = tmp_path / "dry.bean"
        result = _make_dry_path(str(given))
        assert result == given


# ===========================================================================
# _load_existing
# ===========================================================================


class TestLoadExisting:
    def test_returns_empty_when_no_predict_pipeline(self):
        pipelines = [{"name": "a", "predict": False}]
        assert _load_existing(pipelines, "/fake/ledger.bean") == []

    def test_returns_empty_when_no_ledger(self):
        pipelines = [{"name": "a", "predict": True}]
        assert _load_existing(pipelines, None) == []

    def test_loads_and_applies_lookback_filter(self, mocker):
        pipelines = [{"name": "a", "predict": True}]
        old_txn = _txn(date=datetime.date(2020, 1, 1))
        recent_txn = _txn(date=datetime.date.today())
        mocker.patch("beancount.loader.load_file", return_value=([old_txn, recent_txn], [], {}))
        result = _load_existing(pipelines, "/fake/ledger.bean", predict_lookback_days=365)
        assert old_txn not in result
        assert recent_txn in result

    def test_old_price_directives_filtered(self, mocker):
        pipelines = [{"name": "a", "predict": True}]
        old_price = data.Price(
            meta={"filename": "", "lineno": 0},
            date=datetime.date(2020, 1, 1),
            currency="VT",
            amount=Amount(Decimal("100"), "CHF"),
        )
        recent_price = data.Price(
            meta={"filename": "", "lineno": 0},
            date=datetime.date.today(),
            currency="VT",
            amount=Amount(Decimal("110"), "CHF"),
        )
        mocker.patch(
            "beancount.loader.load_file",
            return_value=([old_price, recent_price], [], {}),
        )
        result = _load_existing(pipelines, "/fake/ledger.bean", predict_lookback_days=365)
        assert old_price not in result
        assert recent_price in result

    def test_open_close_preserved_outside_lookback_window(self, mocker):
        """Open/Close directives must survive the date filter regardless of age.

        Regression: smart_importer calls account_map.pop() for Close directives.
        If the matching Open was stripped by the lookback filter, that raises
        KeyError: 'Liabilities:SomeAccount'.
        """
        pipelines = [{"name": "a", "predict": True}]
        old_open = data.Open(
            meta={"filename": "", "lineno": 0},
            date=datetime.date(2015, 1, 1),
            account="Liabilities:OldAccount",
            currencies=[],
            booking=None,
        )
        old_close = data.Close(
            meta={"filename": "", "lineno": 0},
            date=datetime.date(2022, 1, 1),
            account="Liabilities:OldAccount",
        )
        old_txn = _txn(date=datetime.date(2020, 1, 1))
        mocker.patch(
            "beancount.loader.load_file",
            return_value=([old_open, old_close, old_txn], [], {}),
        )
        result = _load_existing(pipelines, "/fake/ledger.bean", predict_lookback_days=365)
        assert old_open in result
        assert old_close in result
        assert old_txn not in result


# ===========================================================================
# _build_wrapped
# ===========================================================================


class TestBuildWrapped:
    def test_no_wrapping_returns_original(self):
        imp = StubImporter()
        assert _build_wrapped(imp, None, False) is imp

    def test_fixes_wrapper_applied(self):
        from drnukebean.pipeline.runner import FixesWrapper

        imp = StubImporter()
        wrapped = _build_wrapped(imp, lambda e: e, False)
        assert isinstance(wrapped, FixesWrapper)

    def test_predict_wrapper_applied(self):
        from smart_importer.wrapper import ImporterWrapper

        imp = StubImporter()
        wrapped = _build_wrapped(imp, None, True)
        assert isinstance(wrapped, ImporterWrapper)

    def test_fixes_and_predict_both_applied(self):
        from smart_importer.wrapper import ImporterWrapper

        imp = StubImporter()
        wrapped = _build_wrapped(imp, lambda e: e, True)
        assert isinstance(wrapped, ImporterWrapper)


# ===========================================================================
# _append_entries
# ===========================================================================


class TestAppendEntries:
    def test_creates_parent_dirs(self, tmp_path):
        dest = tmp_path / "nested" / "deep" / "out.bean"
        _append_entries([_txn()], dest, label="test.xml")
        assert dest.exists()

    def test_section_comment_written(self, tmp_path):
        dest = tmp_path / "out.bean"
        _append_entries([_txn()], dest, label="myfile.xml")
        content = dest.read_text()
        assert "myfile.xml" in content

    def test_appends_on_multiple_calls(self, tmp_path):
        dest = tmp_path / "out.bean"
        _append_entries([_txn("First")], dest, label="a.xml")
        _append_entries([_txn("Second")], dest, label="b.xml")
        content = dest.read_text()
        assert "a.xml" in content
        assert "b.xml" in content

    def test_transaction_content_written(self, tmp_path):
        dest = tmp_path / "out.bean"
        _append_entries([_txn("Hello world")], dest, label="x.xml")
        content = dest.read_text()
        assert "Hello world" in content


# ===========================================================================
# _move_statements
# ===========================================================================


class TestMoveStatements:
    def test_file_moved_to_account_path(self, tmp_path):
        src = tmp_path / "source" / "stmt.xml"
        src.parent.mkdir()
        src.write_text("data")
        dest_root = tmp_path / "archive"
        imp = StubImporter(account="Assets:Bank:ZKB:CHF")
        _move_statements([src], imp, dest_root)
        expected = dest_root / "Assets" / "Bank" / "ZKB" / "CHF" / "stmt.xml"
        assert expected.exists()
        assert not src.exists()

    def test_suggested_filename_used(self, tmp_path):
        src = tmp_path / "source" / "original.xml"
        src.parent.mkdir()
        src.write_text("data")
        dest_root = tmp_path / "archive"
        imp = StubImporter(account="Assets:Test", filename_override="renamed.xml")
        _move_statements([src], imp, dest_root)
        expected = dest_root / "Assets" / "Test" / "renamed.xml"
        assert expected.exists()

    def test_original_name_kept_when_no_suggestion(self, tmp_path):
        src = tmp_path / "source" / "original.xml"
        src.parent.mkdir()
        src.write_text("data")
        dest_root = tmp_path / "archive"
        imp = StubImporter(account="Assets:Test", filename_override=None)
        _move_statements([src], imp, dest_root)
        expected = dest_root / "Assets" / "Test" / "original.xml"
        assert expected.exists()


# ===========================================================================
# run_all — orchestration
# ===========================================================================


class TestRunAll:
    def _source_file(self, pipeline: dict, name: str = "stmt_stub.xml") -> Path:
        """Create a dummy source file that StubImporter will identify."""
        f = Path(pipeline["source_dir"]) / name
        f.write_text("<stub/>")
        return f

    def test_dry_run_writes_to_dry_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        p = _pipeline(tmp_path)
        self._source_file(p)
        dry_path = tmp_path / "dry.bean"
        run_all(
            [p],
            dry_run=True,
            dry_run_file=str(dry_path),
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert dry_path.exists()
        assert "Test" in dry_path.read_text()

    def test_dry_run_does_not_write_to_output_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        p = _pipeline(tmp_path)
        self._source_file(p)
        dry_path = tmp_path / "dry.bean"
        run_all(
            [p],
            dry_run=True,
            dry_run_file=str(dry_path),
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert not Path(p["bean_output_file"]).exists()

    def test_dry_run_does_not_move_source_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        p = _pipeline(tmp_path)
        src = self._source_file(p)
        dest_root = tmp_path / "archive"
        run_all(
            [p],
            dry_run=True,
            dry_run_file=str(tmp_path / "dry.bean"),
            statement_dest=str(dest_root),
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert src.exists()

    def test_full_run_writes_to_output_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        p = _pipeline(tmp_path)
        self._source_file(p)
        run_all(
            [p],
            dry_run=False,
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert Path(p["bean_output_file"]).exists()

    def test_full_run_moves_files_when_dest_root_given(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        p = _pipeline(tmp_path, account="Assets:Invest:IBKR")
        src = self._source_file(p)
        dest_root = tmp_path / "archive"
        run_all(
            [p],
            dry_run=False,
            statement_dest=str(dest_root),
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert not src.exists()
        assert (dest_root / "Assets" / "Invest" / "IBKR" / src.name).exists()

    def test_full_run_leaves_files_in_place_when_no_dest_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        p = _pipeline(tmp_path)
        src = self._source_file(p)
        run_all(
            [p],
            dry_run=False,
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert src.exists()

    def test_name_filter_via_cli_skips_non_matching(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py", "zkb"])
        p_zkb = _pipeline(tmp_path, name="zkb", pattern="zkb_stmt")
        p_ibkr = _pipeline(tmp_path, name="ibkr", pattern="ibkr_stmt")
        self._source_file(p_zkb, "zkb_stmt.xml")
        self._source_file(p_ibkr, "ibkr_stmt.xml")
        run_all(
            [p_zkb, p_ibkr],
            dry_run=False,
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert Path(p_zkb["bean_output_file"]).exists()
        assert not Path(p_ibkr["bean_output_file"]).exists()

    def test_setup_called_with_resolved_dates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        calls = []

        def capture_setup(d_from, d_to):
            calls.append((d_from, d_to))

        p = _pipeline(tmp_path, setup=capture_setup)
        self._source_file(p)
        run_all([p], date_from=datetime.date(2024, 1, 1), date_to=datetime.date(2024, 3, 1))
        assert calls == [(datetime.date(2024, 1, 1), datetime.date(2024, 3, 1))]

    def test_empty_source_dir_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])
        p = _pipeline(tmp_path)
        # No source files created — source_dir is empty
        run_all(
            [p],
            dry_run=False,
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert not Path(p["bean_output_file"]).exists()

    def test_fixes_applied_in_run_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

        def tag_it(txn):
            return txn._replace(narration="FIXED: " + txn.narration)

        p = _pipeline(tmp_path, fixes=tag_it)
        self._source_file(p)
        dry_path = tmp_path / "dry.bean"
        run_all(
            [p],
            dry_run=True,
            dry_run_file=str(dry_path),
            date_from=datetime.date(2024, 1, 1),
            date_to=datetime.date(2024, 1, 1),
        )
        assert "FIXED: Test" in dry_path.read_text()
