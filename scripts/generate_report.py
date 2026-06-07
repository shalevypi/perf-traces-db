"""
generate_report.py - Read db/traces.csv and produce a plain Markdown
table suitable for copy/pasting into Confluence.

Output: db/report.md  (or stdout with --stdout)

Table structure:
  - One row per test (primary vcore config).
  - If a test has data for multiple vcore counts, additional sub-rows
    are appended below the main row with empty category/name columns and
    a "Running on N vcores" label in the Description column, matching
    the existing Confluence table style.
  - Primary row is selected as the config with the highest vcore count
    that has BOTH sim and pldm data; falls back to highest vcore with
    any data.

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

# Preferred vcore counts for primary row selection, highest first
_VCORE_PREF = [32, 16, 8, 4, 2, 1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_descriptions() -> dict:
    if DESCRIPTIONS_FILE.exists():
        return json.loads(DESCRIPTIONS_FILE.read_text())
    return {}


def _load_csv() -> list:
    if not TRACES_CSV.exists():
        print(f"ERROR: {TRACES_CSV} not found. Run 'scan' first.", file=sys.stderr)
        sys.exit(1)
    with open(TRACES_CSV, newline="") as f:
        return list(csv.DictReader(f))


def _to_int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _fmt(val, missing="-") -> str:
    if val is None or str(val).strip() == "":
        return missing
    return str(val)


def _delta_and_pct(sim_c, pldm_c):
    """Return (delta_str, diff_str) or ("-", "-") if either value is missing."""
    if sim_c is None or pldm_c is None:
        return "-", "-"
    delta = pldm_c - sim_c
    delta_str = f"+{delta}" if delta > 0 else str(delta)
    if pldm_c == 0:
        return delta_str, "-"
    pct = delta / pldm_c * 100
    sign = "+" if pct >= 0 else ""
    return delta_str, f"{sign}{pct:.1f}%"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _best_per_source(rows: list, vcores_filter=None) -> dict:
    """
    For each (test_name, num_vcores, num_dies, source), return the row
    from the most recent date.  Filters to measurement/unknown run_type.
    """
    useful = [
        r for r in rows
        if r.get("run_type", "unknown") in ("measurement", "unknown")
    ]

    if vcores_filter is not None:
        useful = [
            r for r in useful
            if _to_int(r.get("num_vcores")) == vcores_filter
        ]

    best = {}
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
    return best


def _merge_sources(best: dict) -> dict:
    """
    Merge sim + pldm entries into per-(test_name, num_vcores, num_dies) dicts.
    Returns {(test_name, num_vcores, num_dies): merged_dict}
    """
    merged = {}
    for (test_name, num_vcores, num_dies, source), row in best.items():
        gk = (test_name, num_vcores, num_dies)
        if gk not in merged:
            merged[gk] = {
                "test_name": test_name,
                "num_vcores": num_vcores,
                "num_dies": num_dies,
                "category": "unknown",
                "sim_cycles": None,
                "pldm_cycles": None,
            }
        if source == "sim":
            merged[gk]["sim_cycles"] = _to_int(row.get("total_cycles"))
        elif source == "pldm":
            merged[gk]["pldm_cycles"] = _to_int(row.get("total_cycles"))
        cat = row.get("category", "unknown")
        if cat and cat != "unknown":
            merged[gk]["category"] = cat
    return merged


def _group_by_test(merged: dict) -> list:
    """
    Group merged vcore configs by test_name.  For each test, select a
    primary config (highest vcore count with both sim+pldm; fallback to
    highest with any data).  Remaining configs become sub-rows, sorted
    by vcore count ascending.

    Returns a list of dicts:
        {test_name, category, primary: config_dict, sub_rows: [config_dict]}
    """
    by_test: dict[str, list] = {}
    for (test_name, num_vcores, num_dies), cfg in merged.items():
        by_test.setdefault(test_name, []).append(cfg)

    result = []
    for test_name, configs in by_test.items():

        def _vc(c):
            v = _to_int(c.get("num_vcores"))
            return v if v is not None else 0

        # Primary: prefer highest vcore with both sim+pldm
        primary = None
        for pv in _VCORE_PREF:
            for c in configs:
                if _vc(c) == pv and c["sim_cycles"] and c["pldm_cycles"]:
                    primary = c
                    break
            if primary:
                break

        # Fallback: highest vcore with any data
        if primary is None:
            for c in sorted(configs, key=_vc, reverse=True):
                if c["sim_cycles"] or c["pldm_cycles"]:
                    primary = c
                    break

        if primary is None:
            primary = sorted(configs, key=_vc, reverse=True)[0]

        sub_rows = sorted(
            [c for c in configs if c is not primary],
            key=_vc,
        )

        category = primary.get("category", "unknown")
        for c in configs:
            if c.get("category", "unknown") != "unknown":
                category = c["category"]
                break

        result.append({
            "test_name": test_name,
            "category": category,
            "primary": primary,
            "sub_rows": sub_rows,
        })

    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _category_display(cat: str) -> str:
    return {
        "elementwise":   "Elementwise Arithmetic",
        "normalization": "Normalization",
        "cast":          "Type Conversion",
        "memory":        "Memory",
        "vme":           "VME",
        "legacy":        "Control Flow",
        "deadlock":      "Deadlock",
        "cce":           "CCE",
        "unknown":       "Other",
    }.get(cat, cat.replace("_", " ").title())


def _render_table(records: list, descriptions: dict) -> str:
    lines = []

    header = (
        "| Category | Test Name | Description "
        "| Sim Cycles | PLDM Cycles | Delta | Diff% |"
    )
    separator = "|---|---|---|---:|---:|---:|---:|"
    lines.append(header)
    lines.append(separator)

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
        cat_str = _category_display(rec["category"])
        desc = (
            descriptions.get(test_name)
            or descriptions.get(test_name.split("_")[0])
            or ""
        )

        primary = rec["primary"]
        sub_rows = rec["sub_rows"]
        has_sub = len(sub_rows) > 0

        sim_c = primary["sim_cycles"]
        pldm_c = primary["pldm_cycles"]
        vc = primary.get("num_vcores", "")

        # Annotate primary cycles with vcore count when sub-rows exist
        if has_sub and vc:
            sim_str = f"{sim_c} ({vc} vcores)" if sim_c is not None else "-"
            pldm_str = f"{pldm_c} ({vc} vcores)" if pldm_c is not None else "-"
        else:
            sim_str = _fmt(sim_c)
            pldm_str = _fmt(pldm_c)

        delta_str, diff_str = _delta_and_pct(sim_c, pldm_c)

        lines.append(
            f"| {cat_str} | {test_name} | {desc} "
            f"| {sim_str} | {pldm_str} | {delta_str} | {diff_str} |"
        )

        # Sub-rows: empty category + test name; label in Description col
        for sub in sub_rows:
            sub_vc = sub.get("num_vcores", "")
            label = (
                f"Running on {sub_vc} vcores" if sub_vc
                else "Running on all vcores"
            )
            sub_sim = _fmt(sub["sim_cycles"])
            sub_pldm = _fmt(sub["pldm_cycles"])
            sub_delta, sub_diff = _delta_and_pct(
                sub["sim_cycles"], sub["pldm_cycles"]
            )
            lines.append(
                f"| | | {label} "
                f"| {sub_sim} | {sub_pldm} | {sub_delta} | {sub_diff} |"
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
    from datetime import date

    rows = _load_csv()
    descriptions = _load_descriptions()

    best = _best_per_source(rows, vcores_filter)
    merged = _merge_sources(best)
    records = _group_by_test(merged)

    if not records:
        print("WARNING: No records to report (check filters).", file=sys.stderr)
        return

    parts = []
    parts.append("# Performance: Simulator vs PLDM")
    parts.append("")
    parts.append(f"Generated: {date.today().isoformat()}")
    parts.append("")
    parts.append(
        "Delta = PLDM cycles - Sim cycles. "
        "Negative = PLDM is faster. "
        "Positive = Simulator is faster."
    )
    parts.append(
        "Measurement runs only. "
        "Per config: latest available date is used."
    )
    if vcores_filter is not None:
        parts.append(f"Filter: {vcores_filter} vcores only.")
    parts.append("")
    parts.append(_render_table(records, descriptions))
    parts.append("### Notes")
    parts.append("")
    parts.append(
        "- Sim Cycles: kernel window cycles from SystemC simulator VCD traces."
    )
    parts.append(
        "- PLDM Cycles: kernel window cycles from Palladium FPGA emulation."
    )
    parts.append(
        "- Delta: PLDM - Sim. Negative = PLDM faster; positive = simulator faster."
    )
    parts.append("- `-`: no VCD trace available for that environment.")
    parts.append(
        "- Sub-rows show results for additional vcore configurations."
    )
    parts.append("")

    output = "\n".join(parts)

    if to_stdout:
        print(output)
    else:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text(output)
        print(f"Report written to: {REPORT_FILE}")
