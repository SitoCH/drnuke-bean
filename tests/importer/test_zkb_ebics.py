"""Tests for the ZKB EBICS download helper (zkb_ebics.py).

``_build_client`` is mocked at the module boundary so that no real EBICS or
fintech calls are made.  diskcache is backed by a tmp_path directory.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

import drnukebean.importer.zkb_ebics as ebics_module  # calls fintech.register()
from drnukebean.importer.zkb_ebics import (
    ZKBCredentials,
    _fetch_statements,
    _write_statements,
    make_zkb_setup,
)

# Must be imported after drnukebean.importer.zkb_ebics, which calls fintech.register().
from fintech.ebics import EbicsFunctionalError  # type: ignore[import-untyped]  # isort: skip

# Runner passes first-of-month dates as period identifiers.
DATE_FROM = datetime.date(2024, 1, 1)
DATE_TO = datetime.date(2024, 1, 1)
# The last day of the DATE_TO month — what EBICS receives as the end date.
DATE_TO_EBICS = datetime.date(2024, 1, 31)

_CREDS = ZKBCredentials(
    keys_file="/fake/keys.db",
    passphrase="fake-passphrase",
    host_id="FAKEHOSTID",
    url="https://ebics.example.com/ebics",
    partner_id="FAKE_PARTNER",
    user_id="FAKE_USER",
)

_SAMPLE_XML = b"<Document>fake camt content</Document>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client(mocker, btd_response: dict | None = None):
    """Patch _build_client and return the mock EbicsClient."""
    if btd_response is None:
        btd_response = {"stmt.xml": _SAMPLE_XML}
    mock_client = MagicMock()
    mock_client.BTD.return_value = btd_response
    mocker.patch.object(ebics_module, "_build_client", return_value=mock_client)
    return mock_client


def _run_setup(tmp_path: Path, mocker, btd_response=None):
    """Create a setup callable, mock the client, and invoke it."""
    mock_cl = _mock_client(mocker, btd_response)
    setup = make_zkb_setup(credentials=_CREDS, dest_dir=tmp_path)
    setup(DATE_FROM, DATE_TO)
    return mock_cl


# ===========================================================================
# make_zkb_setup — cache miss
# ===========================================================================


class TestCacheMiss:
    def test_btd_called_on_cache_miss(self, tmp_path, mocker):
        mock_cl = _run_setup(tmp_path, mocker)
        mock_cl.BTD.assert_called_once()

    def test_file_written_on_cache_miss(self, tmp_path, mocker):
        _run_setup(tmp_path, mocker)
        xml_files = [f for f in tmp_path.iterdir() if f.suffix == ".xml"]
        assert len(xml_files) == 1

    def test_filename_convention(self, tmp_path, mocker):
        _run_setup(tmp_path, mocker)
        xml_files = [f for f in tmp_path.iterdir() if f.suffix == ".xml"]
        assert xml_files[0].name == f"camt053_{DATE_FROM}_{DATE_TO_EBICS}_stmt.xml"

    def test_btd_receives_last_day_of_month_as_end(self, tmp_path, mocker):
        """setup must convert first-of-month date_to to last-of-month for EBICS."""
        mock_cl = _mock_client(mocker)
        setup = make_zkb_setup(credentials=_CREDS, dest_dir=tmp_path)
        setup(DATE_FROM, DATE_TO)
        _, kwargs = mock_cl.BTD.call_args
        assert kwargs["end"] == DATE_TO_EBICS

    def test_file_content_matches_btd_response(self, tmp_path, mocker):
        _run_setup(tmp_path, mocker)
        xml_files = [f for f in tmp_path.iterdir() if f.suffix == ".xml"]
        assert xml_files[0].read_bytes() == _SAMPLE_XML

    def test_confirm_download_called(self, tmp_path, mocker):
        mock_cl = _run_setup(tmp_path, mocker)
        mock_cl.confirm_download.assert_called_once()

    def test_confirm_called_after_btd(self, tmp_path, mocker):
        """confirm_download must come after BTD (protocol ordering)."""
        mock_cl = _run_setup(tmp_path, mocker)
        assert mock_cl.mock_calls.index(
            call.BTD(ebics_module._BTF, start=DATE_FROM, end=DATE_TO_EBICS)
        ) < mock_cl.mock_calls.index(call.confirm_download())

    def test_cache_populated_after_fetch(self, tmp_path, mocker):
        _run_setup(tmp_path, mocker)
        import diskcache

        with diskcache.Cache(tmp_path / ".cache") as cache:
            key = (DATE_FROM.isoformat(), DATE_TO.isoformat())
            assert key in cache


# ===========================================================================
# make_zkb_setup — cache hit
# ===========================================================================


class TestCacheHit:
    def test_btd_not_called_on_cache_hit(self, tmp_path, mocker):
        mock_cl = _run_setup(tmp_path, mocker)  # first run: miss
        mock_cl.BTD.reset_mock()
        # second run: should be a cache hit
        setup = make_zkb_setup(credentials=_CREDS, dest_dir=tmp_path)
        setup(DATE_FROM, DATE_TO)
        mock_cl.BTD.assert_not_called()

    def test_file_still_produced_on_cache_hit(self, tmp_path, mocker):
        # First run writes the file
        _run_setup(tmp_path, mocker)
        # Delete the file to confirm second run re-writes it from cache
        for f in tmp_path.glob("*.xml"):
            f.unlink()
        setup = make_zkb_setup(credentials=_CREDS, dest_dir=tmp_path)
        setup(DATE_FROM, DATE_TO)
        xml_files = [f for f in tmp_path.iterdir() if f.suffix == ".xml"]
        assert len(xml_files) == 1

    def test_confirm_not_called_on_cache_hit(self, tmp_path, mocker):
        mock_cl = _run_setup(tmp_path, mocker)
        mock_cl.confirm_download.reset_mock()
        setup = make_zkb_setup(credentials=_CREDS, dest_dir=tmp_path)
        setup(DATE_FROM, DATE_TO)
        mock_cl.confirm_download.assert_not_called()


# ===========================================================================
# make_zkb_setup — empty response
# ===========================================================================


class TestEmptyResponse:
    def test_empty_btd_not_cached(self, tmp_path, mocker):
        _run_setup(tmp_path, mocker, btd_response={})
        import diskcache

        with diskcache.Cache(tmp_path / ".cache") as cache:
            key = (DATE_FROM.isoformat(), DATE_TO.isoformat())
            assert key not in cache

    def test_empty_btd_no_file_written(self, tmp_path, mocker):
        _run_setup(tmp_path, mocker, btd_response={})
        xml_files = [f for f in tmp_path.iterdir() if f.suffix == ".xml"]
        assert len(xml_files) == 0

    def test_confirm_not_called_on_empty_btd(self, tmp_path, mocker):
        mock_cl = _run_setup(tmp_path, mocker, btd_response={})
        mock_cl.confirm_download.assert_not_called()


# ===========================================================================
# make_zkb_setup — no download data available
# ===========================================================================


class TestNoDownloadData:
    """EBICS_NO_DOWNLOAD_DATA_AVAILABLE from BTD must be handled gracefully."""

    def _raise_no_data(self, mocker, tmp_path):
        mock_cl = MagicMock()
        mock_cl.BTD.side_effect = EbicsFunctionalError(
            EbicsFunctionalError.EBICS_NO_DOWNLOAD_DATA_AVAILABLE
        )
        mocker.patch.object(ebics_module, "_build_client", return_value=mock_cl)
        setup = make_zkb_setup(credentials=_CREDS, dest_dir=tmp_path)
        setup(DATE_FROM, DATE_TO)
        return mock_cl

    def test_does_not_raise(self, tmp_path, mocker):
        self._raise_no_data(mocker, tmp_path)  # must not throw

    def test_no_file_written(self, tmp_path, mocker):
        self._raise_no_data(mocker, tmp_path)
        assert list(tmp_path.glob("*.xml")) == []

    def test_not_cached(self, tmp_path, mocker):
        self._raise_no_data(mocker, tmp_path)
        import diskcache

        with diskcache.Cache(tmp_path / ".cache") as cache:
            assert (DATE_FROM.isoformat(), DATE_TO.isoformat()) not in cache

    def test_confirm_not_called(self, tmp_path, mocker):
        mock_cl = self._raise_no_data(mocker, tmp_path)
        mock_cl.confirm_download.assert_not_called()


# ===========================================================================
# make_zkb_setup — idempotent writes
# ===========================================================================


class TestIdempotentWrite:
    def test_existing_file_not_overwritten(self, tmp_path, mocker):
        _run_setup(tmp_path, mocker)
        xml_file = next(tmp_path.glob("*.xml"))
        original_mtime = xml_file.stat().st_mtime

        # Patch to return different content; file must NOT be overwritten
        _mock_client(mocker, btd_response={"stmt.xml": b"<changed/>"})
        # Manually populate cache with new content to trigger write path
        import diskcache

        with diskcache.Cache(tmp_path / ".cache") as cache:
            cache.delete((DATE_FROM.isoformat(), DATE_TO.isoformat()))
        _run_setup(tmp_path, mocker, btd_response={"stmt.xml": b"<changed/>"})

        assert xml_file.stat().st_mtime == original_mtime
        assert xml_file.read_bytes() == _SAMPLE_XML  # original content preserved

    def test_dest_dir_created_if_missing(self, tmp_path, mocker):
        dest = tmp_path / "new_dir" / "subdir"
        _mock_client(mocker)
        setup = make_zkb_setup(credentials=_CREDS, dest_dir=dest)
        setup(DATE_FROM, DATE_TO)
        assert dest.is_dir()


# ===========================================================================
# _fetch_statements (internal, tested in isolation)
# ===========================================================================


class TestFetchStatements:
    def test_returns_btd_response(self, mocker):
        _mock_client(mocker, btd_response={"a.xml": b"data"})
        result = _fetch_statements(_CREDS, DATE_FROM, DATE_TO)
        assert result == {"a.xml": b"data"}

    def test_btd_error_raises_runtime_error(self, mocker):
        mock_cl = MagicMock()
        mock_cl.BTD.side_effect = Exception("EBICS network failure")
        mocker.patch.object(ebics_module, "_build_client", return_value=mock_cl)
        with pytest.raises(RuntimeError, match="ZKB EBICS BTD failed"):
            _fetch_statements(_CREDS, DATE_FROM, DATE_TO)

    def test_no_download_data_returns_empty_dict(self, mocker):
        """EBICS_NO_DOWNLOAD_DATA_AVAILABLE must be treated as empty, not an error."""
        mock_cl = MagicMock()
        mock_cl.BTD.side_effect = EbicsFunctionalError(
            EbicsFunctionalError.EBICS_NO_DOWNLOAD_DATA_AVAILABLE
        )
        mocker.patch.object(ebics_module, "_build_client", return_value=mock_cl)
        result = _fetch_statements(_CREDS, DATE_FROM, DATE_TO)
        assert result == {}

    def test_no_download_data_confirm_not_called(self, mocker):
        mock_cl = MagicMock()
        mock_cl.BTD.side_effect = EbicsFunctionalError(
            EbicsFunctionalError.EBICS_NO_DOWNLOAD_DATA_AVAILABLE
        )
        mocker.patch.object(ebics_module, "_build_client", return_value=mock_cl)
        _fetch_statements(_CREDS, DATE_FROM, DATE_TO)
        mock_cl.confirm_download.assert_not_called()

    def test_other_functional_error_raises_runtime_error(self, mocker):
        """Any EbicsFunctionalError other than NO_DOWNLOAD_DATA must still raise."""
        mock_cl = MagicMock()
        mock_cl.BTD.side_effect = EbicsFunctionalError(EbicsFunctionalError.EBICS_PROCESSING_ERROR)
        mocker.patch.object(ebics_module, "_build_client", return_value=mock_cl)
        with pytest.raises(RuntimeError, match="ZKB EBICS BTD failed"):
            _fetch_statements(_CREDS, DATE_FROM, DATE_TO)

    def test_empty_btd_returns_empty_dict(self, mocker):
        _mock_client(mocker, btd_response={})
        result = _fetch_statements(_CREDS, DATE_FROM, DATE_TO)
        assert result == {}

    def test_empty_btd_confirm_not_called(self, mocker):
        mock_cl = _mock_client(mocker, btd_response={})
        _fetch_statements(_CREDS, DATE_FROM, DATE_TO)
        mock_cl.confirm_download.assert_not_called()


# ===========================================================================
# _write_statements (internal, tested in isolation)
# ===========================================================================


class TestWriteStatements:
    def test_filename_convention(self, tmp_path):
        written = _write_statements({"report.xml": b"data"}, tmp_path, DATE_FROM, DATE_TO)
        assert len(written) == 1
        assert written[0].name == f"camt053_{DATE_FROM}_{DATE_TO}_report.xml"

    def test_skips_existing_file(self, tmp_path):
        existing = tmp_path / f"camt053_{DATE_FROM}_{DATE_TO}_report.xml"
        existing.write_bytes(b"original")
        written = _write_statements({"report.xml": b"new content"}, tmp_path, DATE_FROM, DATE_TO)
        assert written == []
        assert existing.read_bytes() == b"original"

    def test_returns_written_paths(self, tmp_path):
        stmts = {"a.xml": b"aaa", "b.xml": b"bbb"}
        written = _write_statements(stmts, tmp_path, DATE_FROM, DATE_TO)
        assert len(written) == 2

    def test_str_content_written_as_utf8(self, tmp_path):
        written = _write_statements({"s.xml": "string content"}, tmp_path, DATE_FROM, DATE_TO)
        assert written[0].read_text(encoding="utf-8") == "string content"
