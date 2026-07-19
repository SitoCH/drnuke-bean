"""
prices/split.py  --  split bean-price --update output into per-commodity .price files

Each commodity in the ledger whose price meta points to a data source will produce
a file <out_dir>/<COMMODITY>.price containing beancount price directives, one per day.

Usage:

  Pipe mode (bean-price handles fetching externally):
      bean-price --update main.bean | split-prices prices/

  Self-contained mode (runs bean-price internally):
      split-prices --ledger main.bean prices/

  Dry-run (shows what would be written, touches no files):
      split-prices --ledger main.bean --dry-run prices/

  Programmatic (call from run_imports_pipeline.py after run_all()):
      from drnukebean.prices.split import run_split_prices
      run_split_prices(ledger="main.bean", out_dir="prices/")
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from loguru import logger

# Matches a beancount price directive line, e.g.:
#   2026-01-15 price VT   144.92999268 USD
_PRICE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+price\s+([A-Z][A-Z0-9]*)\s+([\d.]+)\s+([A-Z][A-Z0-9]*)$"
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_price_lines(
    lines: Iterable[str],
) -> list[tuple[str, str, str, str]]:
    """Parse an iterable of text lines into (date, commodity, amount, currency) tuples.

    Non-matching lines (comments, blank lines, bean-price progress output) are
    silently ignored.  Duplicate (date, commodity) pairs within the same batch are
    collapsed to the first occurrence.
    """
    entries: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in lines:
        line = raw.strip()
        m = _PRICE_RE.match(line)
        if not m:
            continue
        key = (m.group(1), m.group(2))  # (date, commodity)
        if key in seen:
            continue
        seen.add(key)
        entries.append((m.group(1), m.group(2), m.group(3), m.group(4)))
    return entries


def _group_by_commodity(
    entries: list[tuple[str, str, str, str]],
) -> dict[str, list[tuple[str, str, str, str]]]:
    by_commodity: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for entry in entries:
        by_commodity[entry[1]].append(entry)
    return dict(by_commodity)


def _format_entries(entries: list[tuple[str, str, str, str]]) -> list[str]:
    """Return sorted, formatted price directive lines (no trailing newline per line)."""
    return [
        f"{date} price {commodity:<20} {amount} {currency}"
        for date, commodity, amount, currency in sorted(entries, key=lambda e: e[0])
    ]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _append_to_file(out_path: Path, entries: list[tuple[str, str, str, str]], dry_run: bool) -> int:
    """Append price directive lines to out_path.  Returns the number of lines written."""
    lines = _format_entries(entries)
    if not lines:
        return 0

    if dry_run:
        logger.info("DRY: {} entries -> {}", len(lines), out_path)
        for line in lines:
            logger.info(line)
        return len(lines)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    return len(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_split_prices(
    out_dir: str | Path,
    ledger: str | Path | None = None,
    dry_run: bool = False,
) -> None:
    """Fetch new prices and split them into per-commodity .price files.

    If *ledger* is given, ``bean-price --update <ledger>`` is run as a subprocess
    and its stdout is parsed.  Otherwise price directives are read from sys.stdin.

    Args:
        out_dir:  Directory where ``<COMMODITY>.price`` files are written (or
                  appended if they already exist).
        ledger:   Path to the main beancount ledger file.  When supplied the
                  function invokes bean-price internally; omit when piping.
        dry_run:  Print what would be written without touching any files.
    """
    out_dir = Path(out_dir)

    if ledger is not None:
        ledger = Path(ledger)
        logger.info("bean-price --update {}", ledger)
        result = subprocess.run(  # noqa: S603
            ["bean-price", "--update", str(ledger)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(
                "bean-price exited {} -- stderr: {}", result.returncode, result.stderr.strip()
            )
            return
        if result.stderr.strip():
            logger.debug("bean-price stderr: {}", result.stderr.strip())
        lines = result.stdout.splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    entries = _parse_price_lines(lines)
    if not entries:
        logger.warning("No price directives found in bean-price output")
        return

    by_commodity = _group_by_commodity(entries)
    total = 0
    for commodity, commodity_entries in sorted(by_commodity.items()):
        out_path = out_dir / f"{commodity}.price"
        count = _append_to_file(out_path, commodity_entries, dry_run)
        logger.info("{}: {} entries -> {}", commodity, count, out_path)
        total += count
    n = len(by_commodity)
    logger.info("split-prices: {} total entries across {} commodity file(s)", total, n)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="split-prices",
        description="Split bean-price --update output into per-commodity .price files.",
        epilog=(
            "Without --ledger, reads price directives from stdin:\n"
            "  bean-price --update main.bean | split-prices prices/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "out_dir",
        help="Directory to write (or append) <COMMODITY>.price files",
    )
    parser.add_argument(
        "--ledger",
        metavar="LEDGER",
        help="Run bean-price --update on this ledger (omit to read from stdin)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without modifying any files",
    )
    args = parser.parse_args()

    run_split_prices(
        out_dir=args.out_dir,
        ledger=args.ledger,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
