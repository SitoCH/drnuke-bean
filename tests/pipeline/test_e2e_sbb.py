"""End-to-end tests: SBB CSV fixture -> pipeline -> beancount output.

Chain exercised (no mocking needed — SBB importer reads local CSV directly):
    SBBImporter.identify / extract  (real code, real fixture)
    -> run_all                      (identify -> extract -> _append_entries)
    -> parse_string                 (beancount syntax validation)

Semantic beancount validity (account declarations, balance assertions) is
out of scope — parse_string covers syntax only.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from beancount.parser import parser as bc_parser

from drnukebean.importer.sbb import SBBImporter
from drnukebean.pipeline.runner import run_all

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sbb" / "sbb_fixture.csv"

_HALBTAX = "Assets:Prepaid:HalbtaxPlus"
_BANK = "Assets:Bank:ZKB:CHF"
_EXPENSES = "Expenses:Transport:SBB"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(source_dir: Path, bean_output_file: Path, *, bank: str = _BANK) -> list[dict]:
    """Pipeline entry wiring SBBImporter to the fixture copied into source_dir."""
    shutil.copy(FIXTURE, source_dir / FIXTURE.name)
    importer = SBBImporter(
        account_halbtax=_HALBTAX,
        account_bank=bank,
        account_expenses=_EXPENSES,
    )
    return [
        {
            "name": "sbb",
            "importer": importer,
            "source_dir": source_dir,
            "bean_output_file": bean_output_file,
            "predict": False,
        }
    ]


# ---------------------------------------------------------------------------
# argv cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_imports.py"])


# ===========================================================================
# Full run
# ===========================================================================


class TestFullRun:
    def test_output_file_created(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out), dry_run=False)
        assert out.exists() and out.stat().st_size > 0

    def test_output_is_valid_beancount_syntax(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out), dry_run=False)
        _, errors, _ = bc_parser.parse_string(out.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)

    def test_output_contains_expected_entry_count(self, tmp_path):
        """Fixture has 32 transactions -> at least 32 entries in output."""
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out), dry_run=False)
        entries, _, _ = bc_parser.parse_string(out.read_text())
        assert len(entries) >= 32

    def test_output_contains_sbb_payee(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out), dry_run=False)
        assert "SBB" in out.read_text()

    def test_output_contains_halbtax_account(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out), dry_run=False)
        assert _HALBTAX in out.read_text()

    def test_output_contains_expense_account(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out), dry_run=False)
        assert _EXPENSES in out.read_text()


# ===========================================================================
# Dry run
# ===========================================================================


class TestDryRun:
    def test_output_file_not_created(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        dry = tmp_path / "dry.bean"
        run_all(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))
        assert not out.exists()

    def test_dry_path_written(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        dry = tmp_path / "dry.bean"
        run_all(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))
        assert dry.exists() and dry.stat().st_size > 0

    def test_dry_output_is_valid_beancount_syntax(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        dry = tmp_path / "dry.bean"
        run_all(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))
        _, errors, _ = bc_parser.parse_string(dry.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)


# ===========================================================================
# No bank configured (single-legged transactions with flag '!')
# ===========================================================================


class TestNoBank:
    def test_output_created_without_bank(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out, bank=""), dry_run=False)
        assert out.exists()

    def test_output_is_valid_beancount_syntax_without_bank(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out, bank=""), dry_run=False)
        _, errors, _ = bc_parser.parse_string(out.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)

    def test_incomplete_flag_present_in_output(self, tmp_path):
        src = tmp_path / "sbb"
        src.mkdir()
        out = tmp_path / "sbb.bean"
        run_all(_make_pipeline(src, out, bank=""), dry_run=False)
        # Non-halbtax rows should have flag '!' for manual completion
        assert "!" in out.read_text()
