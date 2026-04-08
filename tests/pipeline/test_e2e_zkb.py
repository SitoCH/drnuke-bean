"""End-to-end tests: mocked ZKB EBICS fetch -> pipeline -> beancount output.

Chain exercised (nothing mocked except the network layer):
    zkb_ebics._fetch_statements  (mocked, returns real fixture XML)
    -> make_zkb_setup            (diskcache + write XML files to source_dir)
    -> run_all                   (identify -> extract -> _append_entries)
    -> parse_string              (beancount syntax validation)

The diskcache, ZKBCamtImporter, runner, and printer all run against real code.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest
from beancount.parser import parser as bc_parser

import drnukebean.importer.zkb_ebics as ebics_module
from drnukebean.importer.zkb_camt import ZKBCamtImporter
from drnukebean.importer.zkb_ebics import ZKBCredentials, make_zkb_setup
from drnukebean.pipeline.runner import run_all

FIXTURE = Path(__file__).parent.parent / "fixtures" / "zkb" / "camt053.xml"

_IBAN = "CH5604835012345678009"
_ACCOUNT = "Assets:Bank:ZKB:CHF"

# Runner passes first-of-month dates as period identifiers.
DATE_FROM = datetime.date(2024, 1, 1)
DATE_TO = datetime.date(2024, 1, 1)

_CREDS = ZKBCredentials(
    keys_file="/fake/keys.db",
    passphrase="fake",
    host_id="FAKEHOSTID",
    url="https://ebics.example.com/ebics",
    partner_id="FAKE_PARTNER",
    user_id="FAKE_USER",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(source_dir: Path, bean_output_file: Path) -> list[dict]:
    """Pipeline entry wiring real ZKBCamtImporter to the mocked EBICS setup."""
    setup = make_zkb_setup(credentials=_CREDS, dest_dir=source_dir)
    importer = ZKBCamtImporter(iban=_IBAN, account=_ACCOUNT)
    return [
        dict(
            name="zkb",
            importer=importer,
            source_dir=source_dir,
            bean_output_file=bean_output_file,
            setup=setup,
        )
    ]


def _run(pipeline: list[dict], *, dry_run: bool = False, dry_run_file: str | None = None) -> None:
    run_all(
        pipeline, dry_run=dry_run, dry_run_file=dry_run_file, date_from=DATE_FROM, date_to=DATE_TO
    )


# ===========================================================================
# Full run
# ===========================================================================


class TestFullRun:
    @pytest.fixture(autouse=True)
    def _mock_fetch(self, mocker):
        """Return the real fixture as a single-file EBICS response."""
        mocker.patch.object(
            ebics_module,
            "_fetch_statements",
            return_value={"camt053_stmt.xml": FIXTURE.read_bytes()},
        )

    @pytest.fixture(autouse=True)
    def _clean_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

    def test_output_file_created(self, tmp_path):
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        _run(_make_pipeline(src, out))
        assert out.exists() and out.stat().st_size > 0

    def test_output_is_valid_beancount_syntax(self, tmp_path):
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        _run(_make_pipeline(src, out))
        _, errors, _ = bc_parser.parse_string(out.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)

    def test_output_entry_count(self, tmp_path):
        """Fixture has 1 balance + 6 transactions = 7 entries."""
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        _run(_make_pipeline(src, out))
        entries, _, _ = bc_parser.parse_string(out.read_text())
        assert len(entries) >= 7

    def test_setup_writes_xml_to_source_dir(self, tmp_path):
        """make_zkb_setup must write at least one .xml file that identify() finds."""
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        _run(_make_pipeline(src, out))
        xml_files = [f for f in src.iterdir() if f.suffix == ".xml"]
        assert len(xml_files) >= 1


# ===========================================================================
# Dry run
# ===========================================================================


class TestDryRun:
    @pytest.fixture(autouse=True)
    def _mock_fetch(self, mocker):
        mocker.patch.object(
            ebics_module,
            "_fetch_statements",
            return_value={"camt053_stmt.xml": FIXTURE.read_bytes()},
        )

    @pytest.fixture(autouse=True)
    def _clean_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

    def test_output_file_not_created(self, tmp_path):
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        dry = tmp_path / "dry.bean"
        _run(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))
        assert not out.exists()

    def test_dry_path_written(self, tmp_path):
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        dry = tmp_path / "dry.bean"
        _run(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))
        assert dry.exists() and dry.stat().st_size > 0

    def test_dry_output_is_valid_beancount_syntax(self, tmp_path):
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        dry = tmp_path / "dry.bean"
        _run(_make_pipeline(src, out), dry_run=True, dry_run_file=str(dry))
        _, errors, _ = bc_parser.parse_string(dry.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)


# ===========================================================================
# Cache behaviour
# ===========================================================================


class TestCacheHandling:
    @pytest.fixture(autouse=True)
    def _clean_argv(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["run_imports.py"])

    def test_second_run_hits_cache(self, tmp_path, mocker):
        """_fetch_statements called once; second run served from diskcache."""
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        mock_fetch = mocker.patch.object(
            ebics_module,
            "_fetch_statements",
            return_value={"camt053_stmt.xml": FIXTURE.read_bytes()},
        )
        pipeline = _make_pipeline(src, out)

        _run(pipeline)  # first run: cache miss
        out.unlink()  # clear output
        _run(pipeline)  # second run: cache hit

        assert mock_fetch.call_count == 1

    def test_second_run_produces_valid_syntax(self, tmp_path, mocker):
        src = tmp_path / "zkb"
        src.mkdir()
        out = tmp_path / "zkb.bean"
        mocker.patch.object(
            ebics_module,
            "_fetch_statements",
            return_value={"camt053_stmt.xml": FIXTURE.read_bytes()},
        )
        pipeline = _make_pipeline(src, out)

        _run(pipeline)
        out.unlink()
        _run(pipeline)

        _, errors, _ = bc_parser.parse_string(out.read_text())
        assert errors == [], "Beancount syntax errors:\n" + "\n".join(str(e) for e in errors)
