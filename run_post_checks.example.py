"""
run_post_checks.py  --  post-import ledger formatting and validation.

Run manually at any point, or invoked automatically at the end of
run_imports_pipeline.py.

Formatting steps (modify files in place; skipped with --dry-run):
  1. line-endings  optional: --lf or --crlf; omit to leave line endings untouched.
                   Input files may contain LF, CRLF, or CR (old Mac); all are
                   handled correctly regardless of target.
  2. collapse      2+ consecutive blank/whitespace-only lines -> one blank line
  3. trailing      remove spaces/tabs at end of each line
  4. bean-format   align posting amounts to a consistent column

Validation steps (always read-only):
  5. bean-check    full parse + validate (balance assertions, accounts, plugins)
  6. flag-check    list transactions still flagged '!' for manual review

Usage:
    python run_post_checks.py                    # steps 2-6 (no line-ending change)
    python run_post_checks.py --lf               # also normalize line endings to LF
    python run_post_checks.py --crlf             # also normalize line endings to CRLF
    python run_post_checks.py --dry-run          # show what would change; no writes
    python run_post_checks.py --no-collapse      # skip step 2
    python run_post_checks.py --no-trailing      # skip step 3
    python run_post_checks.py --no-bean-format   # skip step 4
    python run_post_checks.py --no-bean-check    # skip step 5
    python run_post_checks.py --no-flag-check    # skip step 6

Sensitive values (ledger paths) live in pipeline_secrets.py.
See pipeline_secrets.example.py for the expected structure.
"""

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_dir = Path.cwd() / "logs"
_log_dir.mkdir(exist_ok=True)
logger.add(
    _log_dir / f"post_checks_{datetime.now():%Y%m%d_%H%M%S}.log",
    level="DEBUG",
    encoding="utf-8",
)

import pipeline_secrets as cfg  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_LEDGER_DIR: Path = cfg.LEDGER_DIR
_MAIN_LEDGER: Path = _LEDGER_DIR / "main.bean"

# Resolve CLI tools from the active Python environment so the script works
# correctly regardless of whether bean-format/bean-check are on PATH.
_BEAN_FORMAT = Path(sys.executable).parent / "bean-format"
_BEAN_CHECK = Path(sys.executable).parent / "bean-check"

# ---------------------------------------------------------------------------
# Formatting steps
# ---------------------------------------------------------------------------


def _collapse_blank_lines(text: str) -> str:
    # Matches 2+ consecutive newlines (possibly with only whitespace between them)
    # and collapses them to a single blank line. Operates on LF-only text.
    return re.sub(r"(\n[ \t]*){2,}\n", "\n\n", text)


def _strip_trailing_whitespace(text: str) -> str:
    return re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)


def _transform_text(
    text: str,
    line_ending: str | None,
    do_collapse: bool,
    do_trailing: bool,
) -> str:
    """Apply text transforms, always working in LF-space, then converting to target."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if do_collapse:
        text = _collapse_blank_lines(text)
    if do_trailing:
        text = _strip_trailing_whitespace(text)
    if line_ending == "crlf":
        text = text.replace("\n", "\r\n")
    return text


def _apply_text_transforms(
    files: list[Path],
    dry_run: bool,
    line_ending: str | None,
    do_collapse: bool,
    do_trailing: bool,
) -> None:
    for path in files:
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("skipping {} (not valid UTF-8)", path.name)
            continue

        text = _transform_text(original, line_ending, do_collapse, do_trailing)

        if text == original:
            logger.debug("no text changes: {}", path.name)
            continue

        if dry_run:
            n_changed = sum(
                1
                for a, b in zip(original.splitlines(), text.splitlines())
                if a != b
            )
            logger.info("[dry-run] text: {} line(s) would change in {}", n_changed, path.name)
        else:
            path.write_text(text, encoding="utf-8")
            logger.info("text-formatted: {}", path.name)


def _run_bean_format(files: list[Path], dry_run: bool) -> None:
    for path in files:
        if dry_run:
            result = subprocess.run(
                [str(_BEAN_FORMAT), str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if result.returncode != 0:
                logger.error("bean-format error on {}: {}", path.name, result.stderr.strip())
                continue
            if result.stdout != path.read_text(encoding="utf-8"):
                logger.info("[dry-run] bean-format would change: {}", path.name)
            else:
                logger.debug("bean-format: no change: {}", path.name)
        else:
            result = subprocess.run(
                [str(_BEAN_FORMAT), "-i", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if result.returncode != 0:
                logger.error("bean-format error on {}: {}", path.name, result.stderr.strip())
            else:
                logger.debug("bean-format: {}", path.name)


# ---------------------------------------------------------------------------
# Validation steps
# ---------------------------------------------------------------------------


def _run_bean_check(ledger: Path) -> bool:
    logger.info("bean-check: {}", ledger)
    result = subprocess.run(
        [str(_BEAN_CHECK), str(ledger)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        logger.info("bean-check: OK")
        if output:
            logger.debug("bean-check output:\n{}", output)
        return True
    else:
        logger.error("bean-check FAILED:\n{}", output)
        return False


def _check_open_flags(files: list[Path]) -> None:
    # Matches transaction header lines with the '!' flag:
    #   YYYY-MM-DD ! "narration"
    pattern = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+!\s+"([^"]*)"', re.MULTILINE)
    total = 0
    for path in files:
        matches = pattern.findall(path.read_text(encoding="utf-8"))
        if not matches:
            continue
        total += len(matches)
        logger.warning("{}: {} open flag(s) (!):", path.name, len(matches))
        for date_str, narration in matches[:5]:
            logger.warning('  {} ! "{}"', date_str, narration)
        if len(matches) > 5:
            logger.warning("  ... and {} more", len(matches) - 5)

    if total == 0:
        logger.info("open flags: none")
    else:
        logger.warning(
            "total open flags: {} -- manual review needed before closing the month", total
        )


# ---------------------------------------------------------------------------
# Public entrypoint (callable from pipeline or as standalone script)
# ---------------------------------------------------------------------------


def run_post_checks(
    *,
    ledger: Path = _MAIN_LEDGER,
    dry_run: bool = False,
    line_ending: str | None = None,
    collapse: bool = True,
    trailing: bool = True,
    bean_format: bool = True,
    bean_check: bool = True,
    flag_check: bool = True,
) -> None:
    """
    Args:
        line_ending: 'lf', 'crlf', or None (default) to leave line endings untouched.
    """
    files = sorted(ledger.parent.rglob("*.bean"))
    logger.info("post-checks: {} .bean file(s) under {}", len(files), ledger.parent)

    if line_ending or collapse or trailing:
        _apply_text_transforms(files, dry_run, line_ending, collapse, trailing)

    if bean_format:
        _run_bean_format(files, dry_run)

    if bean_check:
        _run_bean_check(ledger)

    if flag_check:
        _check_open_flags(files)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_post_checks.py",
        description="Format and validate the beancount ledger.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show what formatting would change; no files are written",
    )
    le = p.add_mutually_exclusive_group()
    le.add_argument("--lf", action="store_true", help="normalize all line endings to LF")
    le.add_argument("--crlf", action="store_true", help="normalize all line endings to CRLF")
    p.add_argument("--no-collapse", action="store_true", help="skip collapsing multiple blank lines")
    p.add_argument("--no-trailing", action="store_true", help="skip trailing whitespace removal")
    p.add_argument("--no-bean-format", action="store_true", help="skip bean-format posting alignment")
    p.add_argument("--no-bean-check", action="store_true", help="skip bean-check validation")
    p.add_argument("--no-flag-check", action="store_true", help="skip open-flag (!) report")
    return p.parse_args()


if __name__ == "__main__":
    ns = _parse_args()
    run_post_checks(
        dry_run=ns.dry_run,
        line_ending="lf" if ns.lf else "crlf" if ns.crlf else None,
        collapse=not ns.no_collapse,
        trailing=not ns.no_trailing,
        bean_format=not ns.no_bean_format,
        bean_check=not ns.no_bean_check,
        flag_check=not ns.no_flag_check,
    )
