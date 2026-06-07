"""
scan_traces.py - Scan sim/ and pldm/ trace folders, run correlation
analysis on each VCD, and write results to db/traces.csv.

Deduplication is managed via db/scan_state.json which records the
file mtime of every already-processed VCD.  A file is re-processed
only if its mtime has changed or --force-reparse is requested.

Correlation is run in parallel using ProcessPoolExecutor.  The number
of worker processes defaults to the CPU count (capped at 8) and can be
overridden with --workers N.

Run via:
    python scripts/main.py scan [--traces-root <path>] [--force-reparse]
                                [--workers N]
"""

import csv
import hashlib
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from parse_filename import parse_vcd_filename        # noqa: E402
from categories import lookup as cat_lookup, build_categories  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DB_DIR      = _REPO_ROOT / "db"
TRACES_CSV  = DB_DIR / "traces.csv"
SCAN_STATE  = DB_DIR / "scan_state.json"
SKIPPED_LOG = DB_DIR / "skipped_files.log"

# ---------------------------------------------------------------------------
# CSV schema - ordered column list
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "row_id",
    "source",
    "date",
    "test_name",
    "dtype_in",
    "dtype_out",
    "size",
    "num_vcores",
    "num_dies",
    "partition",
    "run_type",
    "category",
    "vcore_mhz",
    "start_cycle",
    "end_cycle",
    "total_cycles",
    "kernel_prolog_marker",
    "warmup_cycles",
    "meas_vs_warmup_ratio",
    "file_path",
    "parsed_at",
]

# ---------------------------------------------------------------------------
# Parallel worker  (must be a top-level function to be picklable)
# ---------------------------------------------------------------------------

def _correlation_worker(args: tuple) -> tuple:
    """
    Worker executed in a child process.
    args = (vcd_path_str, corr_scripts_str)
    Returns (vcd_path_str, result_dict).
    """
    vcd_path, corr_scripts = args
    if corr_scripts not in sys.path:
        sys.path.insert(0, corr_scripts)
    from run_correlation import run_correlation
    return vcd_path, run_correlation(vcd_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

_CORR_SCRIPTS = str(_REPO_ROOT / "correlation" / "scripts")


def _row_id(meta: dict) -> str:
    """Stable SHA-256 dedup key derived from the logical identity fields."""
    key = "|".join(str(meta.get(f, "")) for f in [
        "source", "date", "test_name", "dtype_in", "dtype_out",
        "size", "num_vcores", "num_dies", "partition", "run_type",
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_state() -> dict:
    if SCAN_STATE.exists():
        return json.loads(SCAN_STATE.read_text())
    return {}


def _save_state(state: dict) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    SCAN_STATE.write_text(json.dumps(state, indent=2) + "\n")


def _load_csv() -> dict:
    rows = {}
    if not TRACES_CSV.exists():
        return rows
    with open(TRACES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            rows[row["row_id"]] = row
    return rows


def _write_csv(rows: dict) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRACES_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(
            rows.values(),
            key=lambda r: (r["source"], r["date"], r["test_name"],
                           r.get("num_vcores", ""), r.get("run_type", "")),
        ):
            writer.writerow(row)


def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _empty_row(meta: dict, row_id: str, categories: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "row_id":       row_id,
        "source":       meta.get("source", ""),
        "date":         meta.get("date", ""),
        "test_name":    meta.get("test_name", ""),
        "dtype_in":     meta.get("dtype_in") or "",
        "dtype_out":    meta.get("dtype_out") or "",
        "size":         meta.get("size") if meta.get("size") is not None else "",
        "num_vcores":   meta.get("num_vcores") if meta.get("num_vcores") is not None else "",
        "num_dies":     meta.get("num_dies", 1),
        "partition":    meta.get("partition") if meta.get("partition") is not None else "",
        "run_type":     meta.get("run_type", "unknown"),
        "category":     cat_lookup(meta.get("test_name", ""), categories),
        "vcore_mhz":    "",
        "start_cycle":  "",
        "end_cycle":    "",
        "total_cycles": "",
        "kernel_prolog_marker": "",
        "warmup_cycles": "",
        "meas_vs_warmup_ratio": "",
        "file_path":    meta.get("file_path", ""),
        "parsed_at":    now,
    }


def _apply_correlation(row: dict, corr: dict) -> None:
    if corr["error"]:
        return
    for field in ("vcore_mhz", "start_cycle", "end_cycle",
                  "total_cycles", "kernel_prolog_marker"):
        val = corr.get(field)
        row[field] = val if val is not None else ""


def _pair_warmup_measurement(rows: dict) -> None:
    match_key = lambda r: (
        r["source"], r["date"], r["test_name"],
        r["dtype_in"], r["dtype_out"], r["size"],
        r["num_vcores"], r["num_dies"], r["partition"],
    )
    warmup_map: dict[tuple, str] = {}
    for row in rows.values():
        if row["run_type"] == "warmup" and row["total_cycles"]:
            warmup_map[match_key(row)] = row["total_cycles"]
    for row in rows.values():
        if row["run_type"] == "measurement" and row["total_cycles"]:
            wc = warmup_map.get(match_key(row))
            if wc:
                row["warmup_cycles"] = wc
                try:
                    ratio = float(row["total_cycles"]) / float(wc)
                    row["meas_vs_warmup_ratio"] = f"{ratio:.3f}"
                except (ValueError, ZeroDivisionError):
                    pass


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan(
    traces_root: str | None = None,
    force_reparse: bool = False,
    workers: int | None = None,
) -> None:
    """
    Walk traces_root/{sim,pldm}/<date>/ directories, parse every .vcd file,
    run correlation analysis in parallel, and update db/traces.csv.
    """
    root = Path(traces_root) if traces_root else _REPO_ROOT / "vcd_traces"

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    categories   = build_categories()
    state        = _load_state()
    existing_rows = _load_csv()

    DB_DIR.mkdir(parents=True, exist_ok=True)
    skip_log = open(SKIPPED_LOG, "w")

    # ------------------------------------------------------------------
    # Phase 1: walk folders, parse filenames, identify work to do
    # ------------------------------------------------------------------
    # pending: list of (vcd_path_str, meta, row_id, mtime, is_update)
    pending   = []
    skipped_count = 0
    warnings  = []   # (msg,) collected for skip_log

    for source in ("sim", "pldm"):
        source_dir = root / source
        if not source_dir.exists():
            log.warning("Source directory not found: %s", source_dir)
            continue

        for date_dir in sorted(source_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            if date_dir.name.endswith(".bak"):
                log.info("Skipping backup folder: %s", date_dir)
                continue
            date_str = date_dir.name
            log.info("Scanning %s/%s ...", source, date_str)

            for vcd_file in sorted(date_dir.iterdir()):
                meta, warning = parse_vcd_filename(
                    str(vcd_file), source, date_str
                )
                if warning:
                    warnings.append(warning)
                if meta is None:
                    continue

                row_id     = _row_id(meta)
                mtime      = _file_mtime(str(vcd_file))
                state_key  = str(vcd_file)
                state_entry = state.get(state_key, {})

                already_done = (
                    not force_reparse
                    and state_entry.get("file_mtime") == mtime
                    and row_id in existing_rows
                )
                if already_done:
                    skipped_count += 1
                    continue

                pending.append((str(vcd_file), meta, row_id, mtime,
                                row_id in existing_rows))

    # Flush warnings to skip log
    for msg in warnings:
        log.warning(msg)
        skip_log.write(msg + "\n")

    if not pending:
        skip_log.close()
        _pair_warmup_measurement(existing_rows)
        _write_csv(existing_rows)
        _save_state(state)
        log.info("\nDone: 0 new, 0 updated, %d skipped (already parsed), 0 correlation errors", skipped_count)
        log.info("CSV written to: %s", TRACES_CSV)
        return

    # ------------------------------------------------------------------
    # Phase 2: run correlation in parallel
    # ------------------------------------------------------------------
    n_workers = workers or min(8, os.cpu_count() or 4)
    log.info(
        "\nRunning correlation on %d file(s) using %d worker(s) ...",
        len(pending), n_workers,
    )

    # Build args list: (vcd_path, corr_scripts_path)
    work_args = [(vcd_path, _CORR_SCRIPTS) for vcd_path, *_ in pending]

    # Map vcd_path -> correlation result
    corr_results: dict[str, dict] = {}

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        future_to_path = {
            executor.submit(_correlation_worker, arg): arg[0]
            for arg in work_args
        }
        for future in as_completed(future_to_path):
            vcd_path, result = future.result()
            corr_results[vcd_path] = result

    # ------------------------------------------------------------------
    # Phase 3: assemble rows, update state
    # ------------------------------------------------------------------
    new_count = updated_count = error_count = 0

    for vcd_path, meta, row_id, mtime, is_update in pending:
        row  = _empty_row(meta, row_id, categories)
        corr = corr_results.get(vcd_path, {"error": "no result", "vcore_mhz": None,
                                            "start_cycle": None, "end_cycle": None,
                                            "total_cycles": None, "kernel_prolog_marker": None})
        if corr["error"]:
            msg = f"CORR_ERROR: {vcd_path} -- {corr['error']}"
            log.warning(msg)
            skip_log.write(msg + "\n")
            error_count += 1
        else:
            _apply_correlation(row, corr)
            log.info("  [%s] %s", "UPDATE" if is_update else "NEW",
                     Path(vcd_path).name)

        existing_rows[row_id] = row
        state[vcd_path] = {
            "file_path":  vcd_path,
            "file_mtime": mtime,
            "row_id":     row_id,
            "parsed_at":  row["parsed_at"],
        }

        if is_update:
            updated_count += 1
        else:
            new_count += 1

    skip_log.close()

    # Post-scan: pair warmup + measurement
    _pair_warmup_measurement(existing_rows)

    _write_csv(existing_rows)
    _save_state(state)

    log.info(
        "\nDone: %d new, %d updated, %d skipped (already parsed), "
        "%d correlation errors",
        new_count, updated_count, skipped_count, error_count,
    )
    log.info("CSV written to: %s", TRACES_CSV)
    if error_count:
        log.info("See %s for details on skipped/errored files.", SKIPPED_LOG)
