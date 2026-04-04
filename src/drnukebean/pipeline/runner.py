"""
pipeline/runner.py  --  beangulp import orchestration

Usage in run_imports.py:
    from runner import run_all
    run_all(pipelines, ledger='~/ledger/main.bean')
    run_all(pipelines, ledger='~/ledger/main.bean', dry_run=True)

CLI name-filter (positional args, after any flags):
    python run_imports.py              # all importers
    python run_imports.py zkb          # ZKB only
    python run_imports.py pfg neon     # PFG and Neon

Date range (optional; applies to setup callables only):
    python run_imports.py                          # default: last month
    python run_imports.py --from 2026-01           # January 2026 only
    python run_imports.py --from 2026-01 --to 2026-02   # Jan + Feb 2026
    python run_imports.py --from 2025-04           # Apr 2025 through last month
    python run_imports.py zkb --from 2026-01       # ZKB only, January

    --to defaults to --from when only --from is given.
    File-based importers (Neon, PFG, Revolut) are unaffected by the date range.

Dry-run vs full run:
    Dry-run  : output appended to a single temp file; source files are NOT archived.
               Repeat as often as needed until output looks clean.
    Full run : output appended to each importer's own output_file; source files are
               moved to statement_dest after successful extraction.

Output writing bypasses beangulp's extract output machinery because that machinery
is designed for single-file interactive use, emits section headers that are not
valid beancount syntax (**** /path), and has no concept of per-importer file
routing, append semantics, or the fixes/predict pipeline stage.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import beangulp
from beancount.core import data
from beancount.parser import printer
from loguru import logger
from smart_importer import PredictPostings
from smart_importer.wrapper import ImporterWrapper


# ---------------------------------------------------------------------------
# FixesWrapper
# ---------------------------------------------------------------------------


class FixesWrapper(beangulp.Importer):
    """
    Thin beangulp.Importer decorator that applies a fixes_fn to each
    Transaction in extract(). Balance, Note, and other directive types
    pass through unchanged.
    """

    def __init__(self, importer: beangulp.Importer, fixes_fn: Callable) -> None:
        self._importer = importer
        self._fixes_fn = fixes_fn

    def identify(self, filepath: str) -> bool:
        return self._importer.identify(filepath)

    def account(self, filepath: str) -> str:
        return self._importer.account(filepath)

    def date(self, filepath: str):
        return self._importer.date(filepath)

    def filename(self, filepath: str):
        return self._importer.filename(filepath)

    def extract(self, filepath: str, existing: list) -> list:
        entries = self._importer.extract(filepath, existing)
        return [
            self._fixes_fn(e) if isinstance(e, data.Transaction) else e for e in entries
        ]


# ---------------------------------------------------------------------------
# run_all  (public entrypoint)
# ---------------------------------------------------------------------------


def run_all(
    pipelines: list[dict],
    ledger: str | Path | None = None,
    dry_run: bool = False,
    dry_run_file: str | None = None,
    statement_dest: str | Path | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    predict_lookback_days: int | None = 730,
) -> None:
    """
    Run all (or a named subset of) import pipelines.

    Args:
        pipelines:      List of pipeline entry dicts. Required keys:
                            name        : str             -- used for CLI name-filter
                            importer    : beangulp.Importer
                            source_dir  : str | Path
                            output_file : str | Path
                        Optional keys:
                            setup       : callable(date_from, date_to) -> None
                                          Runs before identify. Receives the resolved
                                          date range (first day of each month).
                                          Use for API-fetched statements (IBKR, ZKB).
                            fixes       : callable(txn) -> txn, default None
                            predict     : bool, default False
        ledger:         Path to main beancount ledger. Loaded once and passed
                        as `existing` to extract() calls (used by smart_importer
                        for training). Optional; required when predict=True entries
                        are present.
        dry_run:        If True, all output is appended to dry_run_file and source
                        files are NOT moved. Repeat as needed until output is clean.
        dry_run_file:   Path for dry-run output. Defaults to a temp file whose
                        path is printed at startup.
        statement_dest: Destination root for source files after a successful full
                        run. Files land at <statement_dest>/<account-as-path>/<filename>
                        (colons in the account name become directory separators).
                        Must NOT be a subfolder of any source_dir (beangulp scans
                        subdirectories and would re-identify archived files).
                        If None, source files are left in place after extraction.
        date_from:      Start of the report period (first day of month). CLI
                        --from takes precedence. Defaults to last month.
        date_to:        End of the report period (first day of month). CLI
                        --to takes precedence. Defaults to date_from.
        predict_lookback_days: Limit smart_importer training data to entries at most
                        this many days old. None loads the full ledger. Default: 730
                        (two years).
    """
    dry_run, name_filter = _resolve_cli(dry_run)
    date_from, date_to = _resolve_date_range(date_from, date_to)

    dry_path = None
    if dry_run:
        dry_path = _make_dry_path(dry_run_file)
        logger.info("DRY RUN -- output -> {}", dry_path)

    logger.info("date range: {} -> {}", date_from, date_to)
    active = [e["name"] for e in pipelines if not name_filter or e["name"] in name_filter]
    logger.info("importers: [{}]", ", ".join(active))
    existing = _load_existing(pipelines, ledger, predict_lookback_days)
    dest_root = Path(statement_dest).expanduser() if statement_dest else None

    for entry in pipelines:
        if name_filter and entry["name"] not in name_filter:
            continue
        _run_entry(entry, existing, dry_run, dry_path, dest_root, date_from, date_to)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_month(s: str) -> date:
    """Parse a YYYY-MM string to the first day of that month."""
    try:
        return date.fromisoformat(s + "-01")
    except ValueError:
        raise ValueError(f"Invalid month format {s!r}: expected YYYY-MM") from None


def _last_month() -> date:
    """Return the first day of the previous calendar month."""
    first_this_month = date.today().replace(day=1)
    return (first_this_month - timedelta(days=1)).replace(day=1)


def _resolve_cli(dry_run: bool) -> tuple[bool, set[str]]:
    """Parse sys.argv for --dry-run, --from, --to, and optional importer name filter.

    --from and --to are stripped together with their values before the name filter
    is collected, so YYYY-MM strings are never captured as importer names.
    """
    args = sys.argv[1:]
    if "--dry-run" in args:
        dry_run = True
        args = [a for a in args if a != "--dry-run"]

    name_args: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("--from", "--to") and i + 1 < len(args):
            i += 2  # consume flag and its value
        else:
            name_args.append(args[i])
            i += 1

    return dry_run, set(name_args)


def _cli_dates() -> tuple[date | None, date | None]:
    """Scan sys.argv for --from / --to and return parsed dates (or None)."""
    args = sys.argv[1:]
    result: dict[str, date | None] = {"--from": None, "--to": None}
    i = 0
    while i < len(args):
        if args[i] in result and i + 1 < len(args):
            result[args[i]] = _parse_month(args[i + 1])
            i += 2
        else:
            i += 1
    return result["--from"], result["--to"]


def _resolve_date_range(
    date_from: date | None,
    date_to: date | None,
) -> tuple[date, date]:
    """Resolve the effective date range from CLI args and programmatic params.

    CLI --from / --to take precedence over programmatic params.
    --to defaults to --from when only --from is given.
    Both default to last month when neither is given.
    """
    cli_from, cli_to = _cli_dates()
    resolved_from = cli_from if cli_from is not None else date_from
    resolved_to = cli_to if cli_to is not None else date_to

    if resolved_from is None and resolved_to is None:
        resolved_from = resolved_to = _last_month()
    elif resolved_from is not None and resolved_to is None:
        resolved_to = resolved_from
    elif resolved_from is None and resolved_to is not None:
        resolved_from = resolved_to

    assert resolved_from is not None and resolved_to is not None
    return resolved_from, resolved_to


def _make_dry_path(dry_run_file: str | None) -> Path:
    """Resolve or create the dry-run output file."""
    if dry_run_file:
        return Path(dry_run_file).expanduser()
    tmp = tempfile.NamedTemporaryFile(
        prefix="bean_import_dryrun_", suffix=".bean", delete=False
    )
    tmp.close()
    return Path(tmp.name)


def _load_existing(
    pipelines: list[dict],
    ledger: str | Path | None,
    predict_lookback_days: int | None = 730,
) -> list:
    """Load existing beancount entries if any pipeline entry uses smart_importer.

    Args:
        predict_lookback_days: Limit training data to entries at most this many days
                               old.  None means no limit (load full ledger).
                               Default: 730 (two years).
    """
    if not ledger or not any(p.get("predict", False) for p in pipelines):
        return []
    from beancount import loader  # avoid hard dependency when predict is unused

    entries, _, _ = loader.load_file(str(Path(ledger).expanduser()))
    if predict_lookback_days is not None:
        cutoff = date.today() - timedelta(days=predict_lookback_days)
        # Only apply the date filter to Transaction and Price entries.
        # Open/Close and other structural directives must always be present
        # so that smart_importer can build a consistent open-accounts map
        # without hitting a KeyError when a Close has no matching Open in
        # the window.
        entries = [
            e for e in entries
            if not isinstance(e, (data.Transaction, data.Price)) or e.date >= cutoff
        ]
    return entries


def _build_wrapped(
    importer: beangulp.Importer,
    fixes_fn: Callable | None,
    predict: bool,
) -> beangulp.Importer:
    """Wrap importer with FixesWrapper and/or ImporterWrapper as configured."""
    wrapped = importer
    if fixes_fn:
        wrapped = FixesWrapper(wrapped, fixes_fn)
    if predict:
        wrapped = ImporterWrapper(wrapped, PredictPostings())
    return wrapped


def _run_entry(
    entry: dict,
    existing: list,
    dry_run: bool,
    dry_path: Path | None,
    dest_root: Path | None,
    date_from: date,
    date_to: date,
) -> None:
    """Execute the full pipeline for a single importer entry."""
    importer = entry["importer"]
    source_dir = Path(entry["source_dir"]).expanduser()
    output_file = Path(entry["output_file"]).expanduser()
    setup_fn = entry.get("setup")
    fixes_fn = entry.get("fixes")
    predict = entry.get("predict", False)

    if setup_fn:
        setup_fn(date_from, date_to)

    wrapped = _build_wrapped(importer, fixes_fn, predict)

    source_files = sorted(source_dir.iterdir()) if source_dir.is_dir() else []
    name = entry["name"]
    matched = []
    for f in source_files:
        if not f.is_file():
            continue
        if wrapped.identify(str(f)):
            logger.debug("[{}] identified: {}", name, f.name)
            matched.append(f)
        else:
            logger.debug("[{}] skipped: {}", name, f.name)

    all_entries: list = []
    dest = dry_path if dry_run else output_file
    for filepath in matched:
        entries = wrapped.extract(str(filepath), existing)
        if not entries:
            logger.warning("[{}] identified file yielded 0 entries: {}", name, filepath.name)
        _append_entries(data.sorted(entries), dest, label=filepath.name)
        all_entries.extend(entries)

    if not dry_run and matched and dest_root:
        _move_statements(matched, importer, dest_root)

    _log_summary(entry, matched, all_entries, dest, dry_run)


def _log_summary(
    entry: dict,
    matched: list,
    all_entries: list,
    dest: Path,
    dry_run: bool,
) -> None:
    """Log a one-line summary for a completed pipeline entry."""
    flags = [k for k in ("setup", "fixes", "predict") if entry.get(k)]
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    n_txn = sum(1 for e in all_entries if isinstance(e, data.Transaction))
    n_bal = sum(1 for e in all_entries if isinstance(e, data.Balance))
    mode = "DRY" if dry_run else "RUN"
    logger.info(
        "[{}] {}{}: {} file(s), {} transaction(s), {} balance(s) -> {}",
        mode,
        entry["name"],
        flag_str,
        len(matched),
        n_txn,
        n_bal,
        dest,
    )


def _append_entries(entries: list, dest: Path, label: str) -> None:
    """Append beancount entries to dest, preceded by a section comment."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(f"\n; *** {label} ***\n\n")
        printer.print_entries(entries, file=fh)


def _move_statements(
    matched: list[Path], importer: beangulp.Importer, dest_root: Path
) -> None:
    """
    Move each matched source file to <dest_root>/<account-as-path>/<filename>.

    The importer account 'Assets:Bank:ZKB:CHF' becomes the relative path
    Assets/Bank/ZKB/CHF under dest_root. The suggested filename from
    importer.filename() is used if provided; otherwise the original name is kept.
    """
    for filepath in matched:
        account = importer.account(str(filepath))
        account_path = Path(*account.split(":"))  # colons -> directory separators
        suggested = importer.filename(str(filepath))
        dest_name = suggested if suggested else filepath.name
        dest_dir = dest_root / account_path
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / dest_name
        shutil.move(str(filepath), dest)
        logger.debug("moved: {} -> {}", filepath.name, dest)
