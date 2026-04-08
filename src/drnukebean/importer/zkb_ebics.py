"""
ZKB EBICS CAMT.053 download helper.

This module is deliberately separate from the importer (zkb_camt.py).  It owns
the network-side concern: fetching CAMT.053 XML statements from ZKB via the
EBICS H005 protocol and caching them on disk.  The importer is a pure XML parser
and has no knowledge of EBICS credentials or HTTP.

Caching
-------
The raw response dict ``{original_filename: bytes}`` returned by ``client.BTD()``
is cached in a ``diskcache.Cache`` stored under ``{dest_dir}/.cache``, keyed by
``(date_from.isoformat(), date_to.isoformat())``.  The TTL expires at midnight of
the current day.  On a cache hit the
EBICS server is not contacted and ``confirm_download`` is not called again.

On every call (hit or miss) each XML file in the response dict is written to
``{dest_dir}/camt053_{date_from}_{date_to}_{original_filename}``.  A file is
skipped if it already exists (idempotent writes).

Confirm-before-cache ordering
------------------------------
The fetch sequence is intentionally:
    BTD -> write files to disk -> confirm_download -> set cache

Usage in run_imports.py::

    from drnukebean.importer.zkb_ebics import ZKBCredentials, make_zkb_setup

    ZKB_CREDENTIALS = ZKBCredentials(
        keys_file='/path/to/zkb_keyring.db',
        passphrase='...',
        host_id='...',
        url='https://...',
        partner_id='...',
        user_id='...',
    )

    pipelines = [
        dict(
            name='zkb',
            importer=ZKBCamtImporter(...),
            source_dir=cfg.DOWNLOADS / 'zkb',
            setup=make_zkb_setup(
                credentials=cfg.ZKB_CREDENTIALS,
                dest_dir=cfg.DOWNLOADS / 'zkb',
            ),
            ...
        ),
    ]

    The runner calls setup(date_from, date_to) with the resolved date range
    (first day of each month) derived from --from / --to CLI flags or the
    run_all() programmatic params.
"""

from __future__ import annotations

import calendar
import dataclasses
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import diskcache
import fintech  # type: ignore[import-untyped]  # proprietary, no stubs

fintech.register()  # must be called before importing fintech.ebics submodules

from fintech.ebics import (  # type: ignore[import-untyped]
    BusinessTransactionFormat,
    EbicsBank,
    EbicsClient,
    EbicsFunctionalError,
    EbicsKeyRing,
    EbicsUser,
)
from loguru import logger

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ZKBCredentials:
    """EBICS H005 credentials for ZKB.

    All values come from the bank letter / EBICS initialisation process.
    Place an instance of this class in local_config.py (see
    local_config.py.example for a template).
    """

    keys_file: str | Path  # path to the fintech EBICS keyring file
    passphrase: str  # keyring encryption passphrase
    host_id: str  # bank-assigned EBICS host identifier (HostID)
    url: str  # EBICS server URL
    partner_id: str  # EBICS PartnerID (ContractID at ZKB)
    user_id: str  # EBICS UserID


# ---------------------------------------------------------------------------
# BTF constant
# ---------------------------------------------------------------------------

# BusinessTransactionFormat descriptor for ZKB CAMT.053 statements.
# These parameters are bank-specific and come from the
# "Geschäftsvorfälle / BTF-Parameter" table in the ZKB bank letter.
# service='EOP' (End-of-Period statement), version='08' = camt.053.001.08,
# which matches the XML namespace expected by ZKBCamtImporter.
_BTF = BusinessTransactionFormat(
    service="EOP",
    msg_name="camt.053",
    scope="CH",
    container="ZIP",
    version="08",
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _seconds_until_midnight() -> float:
    """Return seconds remaining until the next calendar-day boundary.

    Used as the diskcache TTL so that cached statements are refreshed at most
    once per calendar day — the same strategy as the IBKR fetcher.
    """
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


def _last_day_of_month(d: date) -> date:
    """Return the last calendar day of the month that contains ``d``."""
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _build_client(creds: ZKBCredentials) -> EbicsClient:
    """Construct and return a ready-to-use EbicsClient from credentials."""
    keyring = EbicsKeyRing(
        keys=str(creds.keys_file),
        passphrase=creds.passphrase,
    )
    bank = EbicsBank(
        keyring=keyring,
        hostid=creds.host_id,
        url=creds.url,
    )
    user = EbicsUser(
        keyring=keyring,
        partnerid=creds.partner_id,
        userid=creds.user_id,
    )
    return EbicsClient(bank, user, version="H005")


def _fetch_statements(
    creds: ZKBCredentials,
    date_from: date,
    date_to: date,
) -> dict[str, bytes]:
    """Fetch CAMT.053 statements via EBICS BTD and confirm receipt.

    Returns the raw dict ``{original_filename: content}`` as delivered by the
    bank.  An empty dict is returned (without confirming) when the bank reports
    no data for the requested period — this is a valid state for months with no
    transactions.

    Raises:
        RuntimeError: on any EBICS or network error.
    """
    client = _build_client(creds)
    try:
        statements: dict[str, bytes] = client.BTD(_BTF, start=date_from, end=date_to)
    except EbicsFunctionalError as exc:
        if exc.code == EbicsFunctionalError.EBICS_NO_DOWNLOAD_DATA_AVAILABLE:
            logger.warning(
                "ZKB EBICS: no statements available for {} -> {}; "
                "this is normal for periods with no transactions",
                date_from,
                date_to,
            )
            return {}
        raise RuntimeError(f"ZKB EBICS BTD failed for {date_from} -> {date_to}: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"ZKB EBICS BTD failed for {date_from} -> {date_to}: {exc}") from exc

    if not statements:
        logger.warning(
            "ZKB EBICS: no statements returned for {} -> {}; "
            "this is normal for periods with no transactions",
            date_from,
            date_to,
        )
        return {}

    # Confirm receipt only after we have data.  The confirm is a separate
    # EBICS transaction that acknowledges successful download to the bank.
    # We do this before writing to cache so that a crash here leaves the cache
    # unset and forces a retry on the next run.
    client.confirm_download()
    return statements


def _write_statements(
    statements: dict[str, bytes],
    dest: Path,
    date_from: date,
    date_to: date,
) -> list[Path]:
    """Write each statement file to dest, returning the list of written paths.

    Naming convention: ``camt053_{date_from}_{date_to}_{original_filename}``
    makes the date range explicit and avoids collisions across multiple runs.
    Files that already exist are silently skipped (idempotent).
    """
    written: list[Path] = []
    for original_name, content in statements.items():
        out_path = dest / f"camt053_{date_from}_{date_to}_{original_name}"
        if out_path.exists():
            logger.debug("ZKB EBICS: skipping existing file {}", out_path.name)
            continue
        if isinstance(content, bytes):
            out_path.write_bytes(content)
        else:
            # fintech may return str in certain parsing modes
            out_path.write_text(content, encoding="utf-8")
        written.append(out_path)
        logger.debug("ZKB EBICS: wrote {}", out_path.name)
    return written


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def make_zkb_setup(
    credentials: ZKBCredentials,
    dest_dir: str | Path,
) -> Callable[[date, date], None]:
    """Return a setup callable for use in the pipeline.

    The callable is invoked by the runner as ``setup(date_from, date_to)``
    where both dates are the first day of their respective months.

    On each invocation the callable checks a ``diskcache.Cache`` for a cached
    copy of the BTD response.  On a cache hit the EBICS server is not contacted
    and ``confirm_download`` is not called again.  On a cache miss the statements
    are fetched, confirmed, and stored in the cache with a TTL that expires at
    midnight.

    In both cases each XML file in the response is written to
    ``{dest_dir}/camt053_{date_from}_{date_to}_{original_filename}``
    (skipped if the file already exists).

    Args:
        credentials:  ZKB EBICS credentials (from local_config.py).
        dest_dir:     Directory where downloaded XML files are saved.  Should
                      match the ``source_dir`` of the corresponding pipeline
                      entry so the importer can find the files.
    """

    def setup(date_from: date, date_to: date) -> None:
        dest = Path(dest_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)

        # The runner passes first-of-month dates as period identifiers.
        # EBICS needs the actual last day of the final month as the end date;
        # otherwise a request for e.g. March yields start=end=2026-03-01 and
        # the bank returns EBICS_NO_DOWNLOAD_DATA_AVAILABLE.
        ebics_end = _last_day_of_month(date_to)

        # Cache key uses the runner's month identifiers (not EBICS dates) so
        # the key is stable regardless of how many days are in the month.
        cache_key = (date_from.isoformat(), date_to.isoformat())

        with diskcache.Cache(dest / ".cache") as cache:
            if cache_key in cache:
                logger.info("ZKB EBICS: cache hit for {} -> {}", date_from, date_to)
                statements: dict[str, bytes] = cast(dict, cache[cache_key])
            else:
                logger.info(
                    "ZKB EBICS: fetching statements {} -> {} via EBICS",
                    date_from,
                    ebics_end,
                )
                statements = _fetch_statements(credentials, date_from, ebics_end)

                # Only cache non-empty responses.  An empty response may mean
                # the period truly has no transactions, or it could be a
                # transient bank-side issue — either way we want the next run
                # to try again rather than caching a permanent empty result.
                if statements:
                    cache.set(cache_key, statements, expire=_seconds_until_midnight())

        written = _write_statements(statements, dest, date_from, ebics_end)
        if written:
            logger.info(
                "ZKB EBICS: wrote {} file(s) to {}",
                len(written),
                dest,
            )
        elif statements:
            # Statements were available (from cache or fresh fetch) but all
            # files already existed on disk — nothing new to write.
            logger.info(
                "ZKB EBICS: all files already present in {}, nothing written",
                dest,
            )

    return setup
