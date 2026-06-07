"""
scan_traces.py - Scan sim/ and pldm/ trace folders, run correlation
analysis on each VCD, and write results to db/traces.csv.

Deduplication is managed via db/scan_state.json which records the
file mtime of every already-processed VCD.  A file is re-processed
only if its mtime has changed or --force-reparse is requested.

Run via:
    python scripts/main.py scan [--traces-root <path>] [--force-reparse]
"""

import csv
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from parse_filename import parse_vcd_filename  # noqa: E402
from run_correlation import run_correlation     # noqa: E402
from categories import lookup as cat_lookup, build_categories  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DB_DIR = _REPO_ROOT / "db"
TRACES_CSV = DB_DIR / "traces.csv"
SCAN_STATE = DB_DIR / "scan_state.json"
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
# Helpers
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


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
    """Return existing CSV rows keyed by row_id."""
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
        for row in sorted(rows.values(),
                          key=lambda r: (r["source"], r["date"], r["test_name"],
                                         r.get("num_vcores", ""), r.get("run_type", ""))):
            writer.writerow(row)


def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _empty_row(meta: dict, row_id: str, categories: dict) -> dict:
    """Build a CSV row dict from parsed metadata with empty correlation fields."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "row_id": row_id,
        "source": meta.get("source", ""),
        "date": meta.get("date", ""),
        "test_name": meta.get("test_name", ""),
        "dtype_in": meta.get("dtype_in") or "",
        "dtype_out": meta.get("dtype_out") or "",
        "size": meta.get("size") if meta.get("size") is not None else "",
        "num_vcores": meta.get("num_vcores") if meta.get("num_vcores") is not None else "",
        "num_dies": meta.get("num_dies", 1),
        "partition": meta.get("partition") if meta.get("partition") is not None else "",
        "run_type": meta.get("run_type", "unknown"),
        "category": cat_lookup(meta.get("test_name", ""), categories),
        "vcore_mhz": "",
        "start_cycle": "",
        "end_cycle": "",
        "total_cycles": "",
        "kernel_prolog_marker": "",
        "warmup_cycles": "",
        "meas_vs_warmup_ratio": "",
        "file_path": meta.get("file_path", ""),
        "parsed_at": now,
    }


def _apply_correlation(row: dict, corr: dict) -> None:
    """Fill correlation fields into a CSV row dict in-place."""
    if corr["error"]:
        return
    for field in ("vcore_mhz", "start_cycle", "end_cycle",
                  "total_cycles", "kernel_prolog_marker"):
        val = corr.get(field)
        row[field] = val if val is not None else ""


def _pair_warmup_measurement(rows: dict) -> None:
    """
    Post-scan pass: for each measurement row, find its matching warmup row
    (same source/date/test_name/dtype_in/dtype_out/size/num_vcores/
    num_dies/partition) and fill warmup_cycles + meas_vs_warmup_ratio.
    """
    match_key = lambda r: (
        r["source"], r["date"], r["test_name"],
        r["dtype_in"], r["dtype_out"], r["size"],
        r["num_vcores"], r["num_dies"], r["partition"],
    )

    warmup_map: dict[tuple, str] = {}  # key -> total_cycles str
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

def scan(traces_root: str | None = None, force_reparse: bool = False) -> None:
    """
    Walk traces_root/{sim,pldm}/<date>/ directories, parse every .vcd file,
    run correlation analysis, and update db/traces.csv.
    """
    root = Path(traces_root) if traces_root else _REPO_ROOT / "vcd_traces"

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    # Ensure categories exist
    categories = build_categories()

    state = _load_state()
    existing_rows = _load_csv()

    # Open skip log
    DB_DIR.mkdir(parents=True, exist_ok=True)
    skip_log = open(SKIPPED_LOG, "w")

    new_count = updated_count = skipped_count = error_count = 0

    for source in ("sim", "pldm"):
        source_dir = root / source
        if not source_dir.exists():
            log.warning("Source directory not found: %s", source_dir)
            continue

        for date_dir in sorted(source_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            date_str = date_dir.name

            log.info("Scanning %s/%s ...", source, date_str)

            for vcd_file in sorted(date_dir.iterdir()):
                # parse_vcd_filename handles non-.vcd silently
                meta, warning = parse_vcd_filename(
                    str(vcd_file), source, date_str
                )

                if warning:
                    log.warning(warning)
                    skip_log.write(warning + "\n")

                if meta is None:
                    continue

                row_id = _row_id(meta)
                mtime = _file_mtime(str(vcd_file))

                # Dedup check: state is keyed by file_path to avoid
                # collisions when two files in the same folder share the
                # same logical row_id (common in old trace folders).
                state_key = str(vcd_file)
                state_entry = state.get(state_key, {})
                already_done = (
                    not force_reparse
                    and state_entry.get("file_mtime") == mtime
                    and row_id in existing_rows
                )

                if already_done:
                    skipped_count += 1
                    continue

                is_update = row_id in existing_rows
                log.info(
                    "  [%s] %s",
                    "UPDATE" if is_update else "NEW",
                    vcd_file.name,
                )

                # Build row
                row = _empty_row(meta, row_id, categories)

                # Run correlation
                corr = run_correlation(str(vcd_file))
                if corr["error"]:
                    msg = f"CORR_ERROR: {vcd_file} -- {corr['error']}"
                    log.warning(msg)
                    skip_log.write(msg + "\n")
                    error_count += 1
                else:
                    _apply_correlation(row, corr)

                existing_rows[row_id] = row
                state[state_key] = {
                    "file_path": str(vcd_file),
                    "file_mtime": mtime,
                    "row_id": row_id,
                    "parsed_at": row["parsed_at"],
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

    total = new_count + updated_count
    log.info(
        "\nDone: %d new, %d updated, %d skipped (already parsed), "
        "%d correlation errors",
        new_count, updated_count, skipped_count, error_count,
    )
    log.info("CSV written to: %s", TRACES_CSV)
    if error_count:
        log.info("See %s for details on skipped/errored files.", SKIPPED_LOG)
