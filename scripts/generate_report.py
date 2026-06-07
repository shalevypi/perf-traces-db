"""
generate_report.py - Read db/traces.csv and produce a plain Markdown
table suitable for copy/pasting into Confluence.

Output: db/report.md  (or stdout with --stdout)

Rules:
  - No non-ASCII characters anywhere
  - Plain "-" for missing values
  - Delta = PLDM cycles - Sim cycles  (negative = PLDM faster)
  - Only "measurement" (or "unknown") run_type rows are used
  - For each (test_name, num_vcores, num_dies), the LATEST date wins

Usage:
    python scripts/main.py report [--vcores N] [--stdout]
"""

import csv
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = _REPO_ROOT / "db"
TRACES_CSV = DB_DIR / "traces.csv"
DESCRIPTIONS_FILE = DB_DIR / "test_descriptions.json"
REPORT_FILE = DB_DIR / "report.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_descriptions() -> dict:
    if DESCRIPTIONS_FILE.exists():
        return json.loads(DESCRIPTIONS_FILE.read_text())
    return {}


def _load_csv() -> list[dict]:
    if not TRACES_CSV.exists():
        print(f"ERROR: {TRACES_CSV} not found. Run 'scan' first.", file=sys.stderr)
        sys.exit(1)
    with open(TRACES_CSV, newline="") as f:
        return list(csv.DictReader(f))


def _to_int(val) -> int | None:
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _fmt(val, missing="-") -> str:
    if val is None or str(val).strip() == "":
        return missing
    return str(val)


def _pct(delta: int, pldm: int) -> str:
    if pldm == 0:
        return "-"
    p = delta / pldm * 100
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"


def _delta_str(delta: int) -> str:
    return f"+{delta}" if delta > 0 else str(delta)


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def _aggregate(rows: list[dict], vcores_filter: int | None) -> list[dict]:
    """
    For each (test_name, num_vcores, num_dies) combination, pick the
    most recent measurement row for sim AND pldm separately.

    Returns a list of merged dicts ready for table rendering.
    """
    # Only keep measurement-like rows
    useful = [
        r for r in rows
        if r.get("run_type", "unknown") in ("measurement", "unknown")
    ]

    if vcores_filter is not None:
        useful = [
            r for r in useful
            if _to_int(r.get("num_vcores")) == vcores_filter
            or (r.get("num_vcores", "") == "" and vcores_filter is None)
        ]

    # Group by (test_name, num_vcores, num_dies) -> latest per source
    # key: (test_name, num_vcores, num_dies, source) -> best row
    best: dict[tuple, dict] = {}
    for r in useful:
        key = (
            r.get("test_name", ""),
            r.get("num_vcores", ""),
            r.get("num_dies", "1"),
            r.get("source", ""),
        )
        existing = best.get(key)
        if existing is None or r.get("date", "") > existing.get("date", ""):
            best[key] = r

    # Merge sim + pldm by (test_name, num_vcores, num_dies)
    merged: dict[tuple, dict] = {}
    for (test_name, num_vcores, num_dies, source), row in sorted(
        best.items(), key=lambda x: x[0]
    ):
        group_key = (test_name, num_vcores, num_dies)
        if group_key not in merged:
            merged[group_key] = {
                "test_name": test_name,
                "num_vcores": num_vcores,
                "num_dies": num_dies,
                "category": row.get("category", "unknown"),
                "sim_cycles": None,
                "pldm_cycles": None,
                "sim_date": "",
                "pldm_date": "",
            }
        if source == "sim":
            merged[group_key]["sim_cycles"] = _to_int(row.get("total_cycles"))
            merged[group_key]["sim_date"] = row.get("date", "")
            # prefer non-empty category
            if row.get("category") and row["category"] != "unknown":
                merged[group_key]["category"] = row["category"]
        elif source == "pldm":
            merged[group_key]["pldm_cycles"] = _to_int(row.get("total_cycles"))
            merged[group_key]["pldm_date"] = row.get("date", "")
            if row.get("category") and row["category"] != "unknown":
                merged[group_key]["category"] = row["category"]

    return list(merged.values())


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

def _category_display(cat: str) -> str:
    """Human-readable category name, no special chars."""
    return {
        "elementwise": "Elementwise Arithmetic",
        "normalization": "Normalization",
        "cast": "Type Conversion",
        "memory": "Memory",
        "vme": "VME",
        "legacy": "Control Flow",
        "deadlock": "Deadlock",
        "cce": "CCE",
        "unknown": "Other",
    }.get(cat, cat.replace("_", " ").title())


def _render_table(
    records: list[dict],
    descriptions: dict,
    title: str = "",
) -> str:
    lines = []

    if title:
        lines.append(f"## {title}")
        lines.append("")

    # Table header (plain ASCII, no Unicode)
    header = (
        "| Category | Test Name | Description "
        "| Sim Cycles | PLDM Cycles | Delta | Diff% |"
    )
    separator = (
        "|---|---|---|---:|---:|---:|---:|"
    )
    lines.append(header)
    lines.append(separator)

    # Sort by category then test name
    cat_order = [
        "elementwise", "cast", "memory", "normalization",
        "vme", "cce", "legacy", "deadlock", "unknown",
    ]
    def _sort_key(rec):
        cat = rec.get("category", "unknown")
        try:
            ci = cat_order.index(cat)
        except ValueError:
            ci = len(cat_order)
        return (ci, rec.get("test_name", ""))

    for rec in sorted(records, key=_sort_key):
        test_name = rec["test_name"]
        cat_str = _category_display(rec.get("category", "unknown"))

        # Description: look up short name variants
        desc = (
            descriptions.get(test_name)
            or descriptions.get(test_name.split("_")[0])
            or ""
        )

        sim_c = rec["sim_cycles"]
        pldm_c = rec["pldm_cycles"]

        sim_str = _fmt(sim_c)
        pldm_str = _fmt(pldm_c)

        if sim_c is not None and pldm_c is not None:
            delta = pldm_c - sim_c
            delta_str = _delta_str(delta)
            diff_str = _pct(delta, pldm_c)
        else:
            delta_str = "-"
            diff_str = "-"

        # vcores annotation if multiple
        vc = rec.get("num_vcores", "")
        vc_note = f" ({vc} vcores)" if vc and str(vc) not in ("", "1") else ""
        display_name = f"{test_name}{vc_note}"

        lines.append(
            f"| {cat_str} | {display_name} | {desc} "
            f"| {sim_str} | {pldm_str} | {delta_str} | {diff_str} |"
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(
    vcores_filter: int | None = None,
    to_stdout: bool = False,
) -> None:
    rows = _load_csv()
    descriptions = _load_descriptions()
    records = _aggregate(rows, vcores_filter)

    if not records:
        print("WARNING: No records to report (check filters).", file=sys.stderr)
        return

    # Group by num_vcores for multi-table output when no filter
    if vcores_filter is None:
        vcores_groups: dict = {}
        for rec in records:
            vc = rec.get("num_vcores", "")
            vcores_groups.setdefault(vc, []).append(rec)
    else:
        vcores_groups = {vcores_filter: records}

    parts = []

    # Header block
    from datetime import date
    parts.append(f"# Performance: Simulator vs PLDM")
    parts.append(f"")
    parts.append(f"Generated: {date.today().isoformat()}")
    parts.append(f"")
    parts.append(
        "Delta = PLDM cycles - Sim cycles. "
        "Negative = PLDM is faster. "
        "Positive = Simulator is faster."
    )
    parts.append(f"")
    parts.append(
        "Measurement runs only. "
        "Per config: latest available date is used."
    )
    parts.append(f"")

    for vc, grp_records in sorted(
        vcores_groups.items(),
        key=lambda x: (x[0] == "", int(x[0]) if str(x[0]).isdigit() else 999),
    ):
        if str(vc) == "" or vc is None:
            title = "All VCores (default)"
        else:
            title = f"{vc} VCores"
        parts.append(_render_table(grp_records, descriptions, title=title))

    # Notes
    parts.append("### Notes")
    parts.append("")
    parts.append(
        "- Sim Cycles: kernel execution window cycles from SystemC simulator VCD traces."
    )
    parts.append(
        "- PLDM Cycles: kernel execution window cycles from Palladium FPGA emulation."
    )
    parts.append(
        "- Delta: PLDM - Sim. Negative means PLDM is faster; "
        "positive means simulator is faster."
    )
    parts.append("- `-`: no VCD trace available for that environment.")
    parts.append("")

    output = "\n".join(parts)

    if to_stdout:
        print(output)
    else:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text(output)
        print(f"Report written to: {REPORT_FILE}")
