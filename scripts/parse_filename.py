"""
parse_filename.py - Parse VCD trace filenames into structured metadata.

Supported patterns:
  A  (new sim):    vcore_r*_asm_<body>_<size>_num_vcores_<N>_partition_<P>
                   [_warmup|_measurement].vcd
  A2 (new sim):    vcore_r*_asm_<body>_<size>[_warmup|_measurement].vcd
                   (no num_vcores/partition in name; num_vcores=None)
  B  (new pldm):   py_asm_<body>_<size>_num_vcores_<N>_partition_<P>.vcd
  C  (old sim):    vcore_sysc_trace_r*_test_asm_<test>[.py].vcd
  D (old pldm v1): pldm_trace_test_asm_<test>[.py].vcd
  E (old pldm v2): asm_<test>.vcd  (e.g. asm_cast_mxfp8_bf16.vcd)

Valid prefixes:
  Sim  : vcore_
  PLDM : py_asm_, pldm_trace_, asm_

Any .vcd whose filename does not start with one of these prefixes is treated
as noise and emits a WARNING.  Non-.vcd files are silently skipped.
"""

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KNOWN_DTYPES = frozenset({
    "bf16", "fp32", "fp16", "fp8", "mxfp8",
    "int8", "int16", "int32", "uint8", "uint16",
})

VALID_PREFIXES = ("py_asm_", "pldm_trace_", "asm_", "vcore_")

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Pattern A - new sim (with num_vcores + partition)
# vcore_r0_t0_c0_d0_v0_asm_<body>_<size>_num_vcores_<N>_partition_<P>[_run_type]
_RE_A = re.compile(
    r"^vcore_r\d+_t\d+_c\d+_d\d+_v\d+_"
    r"asm_(.+?)_(\d+)"
    r"_num_vcores_(\d+)"
    r"_partition_(\d+)"
    r"(?:_(warmup|measurement))?$"
)

# Pattern A2 - new sim (without num_vcores/partition)
# vcore_r0_t0_c0_d0_v0_asm_<body>_<size>[_warmup|_measurement]
_RE_A2 = re.compile(
    r"^vcore_r\d+_t\d+_c\d+_d\d+_v\d+_"
    r"asm_(.+?)_(\d+)"
    r"(?:_(warmup|measurement))?$"
)

# Pattern B - new pldm
# py_asm_<body>_<size>_num_vcores_<N>_partition_<P>
_RE_B = re.compile(
    r"^py_asm_(.+?)_(\d+)"
    r"_num_vcores_(\d+)"
    r"_partition_(\d+)$"
)

# Pattern C - old sim
# vcore_sysc_trace_r*_test_asm_<test>[.py]
_RE_C = re.compile(
    r"^vcore_sysc_trace_r\d+_t\d+_c\d+_d\d+_v\d+_"
    r"test_asm_(.+?)(?:\.py)?$"
)

# Pattern D - old pldm v1
# pldm_trace_test_asm_<test>[.py]
_RE_D = re.compile(
    r"^pldm_trace_test_asm_(.+?)(?:\.py)?$"
)

# Pattern E - old pldm v2: asm_<test>
# (only fires when stem starts with exactly "asm_" and is not already pattern A/C)
_RE_E = re.compile(r"^asm_(.+)$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> str:
    """Convert YYYYMMDD folder name to ISO date string YYYY-MM-DD."""
    s = str(date_str)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _extract_dtypes(body: str):
    """
    Given a test body string like 'add_bf16_bf16' or 'cast_mxfp8_bf16_bf16',
    extract (dtype_in, dtype_out) by finding the last two known dtype tokens
    scanning right-to-left.

    Returns (dtype_in, dtype_out) - either or both may be None if not found.
    """
    tokens = body.split("_")
    dtype_out_idx = None
    dtype_in_idx = None

    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i] in KNOWN_DTYPES:
            if dtype_out_idx is None:
                dtype_out_idx = i
            else:
                dtype_in_idx = i
                break

    dtype_out = tokens[dtype_out_idx] if dtype_out_idx is not None else None
    dtype_in = tokens[dtype_in_idx] if dtype_in_idx is not None else None
    return dtype_in, dtype_out


def _make_record(
    source, date_str, test_name, dtype_in, dtype_out,
    size, num_vcores, num_dies, partition, run_type, file_path
) -> dict:
    return {
        "source": source,
        "date": _parse_date(date_str),
        "test_name": test_name,
        "dtype_in": dtype_in,
        "dtype_out": dtype_out,
        "size": size,
        "num_vcores": num_vcores,
        "num_dies": num_dies,
        "partition": partition,
        "run_type": run_type,
        "file_path": str(file_path),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_vcd_filename(filepath, source: str, date_str: str):
    """
    Parse a VCD filename and return (record_dict, warning_str).

    - If the file should be silently skipped (non-.vcd): (None, None)
    - If the file is recognised noise (no valid prefix): (None, WARNING str)
    - If recognised but no pattern matches: (None, WARNING str)
    - On success: (dict, None)

    Args:
        filepath : str or Path to the .vcd file
        source   : "sim" or "pldm"
        date_str : folder date string, YYYYMMDD
    """
    path = Path(str(filepath))
    stem = path.stem  # strips last suffix (.vcd); keeps .py for double-ext files

    # Only process .vcd files
    if path.suffix.lower() != ".vcd":
        return None, None  # silently skip

    # Pre-filter: must start with a known prefix
    if not any(stem.startswith(p) for p in VALID_PREFIXES):
        return None, (
            f"NOISE: {filepath} -- no valid prefix "
            f"(expected one of: {', '.join(VALID_PREFIXES)})"
        )

    fp = str(filepath)

    # --- Pattern A: new sim (with num_vcores + partition) ---
    m = _RE_A.match(stem)
    if m:
        body, size_str, vcores_str, part_str, run_type = m.groups()
        dtype_in, dtype_out = _extract_dtypes(body)
        return _make_record(
            source, date_str, body, dtype_in, dtype_out,
            int(size_str), int(vcores_str), 1,
            int(part_str), run_type or "unknown", fp
        ), None

    # --- Pattern A2: new sim (without num_vcores/partition) ---
    m = _RE_A2.match(stem)
    if m:
        body, size_str, run_type = m.groups()
        dtype_in, dtype_out = _extract_dtypes(body)
        return _make_record(
            source, date_str, body, dtype_in, dtype_out,
            int(size_str), None, 1,
            None, run_type or "unknown", fp
        ), None

    # --- Pattern B: new pldm ---
    m = _RE_B.match(stem)
    if m:
        body, size_str, vcores_str, part_str = m.groups()
        dtype_in, dtype_out = _extract_dtypes(body)
        return _make_record(
            source, date_str, body, dtype_in, dtype_out,
            int(size_str), int(vcores_str), 1,
            int(part_str), "unknown", fp
        ), None

    # --- Pattern C: old sim ---
    m = _RE_C.match(stem)
    if m:
        return _make_record(
            source, date_str, m.group(1), None, None,
            None, None, 1, None, "unknown", fp
        ), None

    # --- Pattern D: old pldm v1 ---
    m = _RE_D.match(stem)
    if m:
        return _make_record(
            source, date_str, m.group(1), None, None,
            None, None, 1, None, "unknown", fp
        ), None

    # --- Pattern E: old pldm v2 (bare asm_<test>) ---
    # Guard: only fire if stem truly starts with "asm_" (not caught above)
    if stem.startswith("asm_"):
        m = _RE_E.match(stem)
        if m:
            return _make_record(
                source, date_str, m.group(1), None, None,
                None, None, 1, None, "unknown", fp
            ), None

    # Has a valid prefix but matched no known pattern
    return None, f"WARNING: {filepath} -- unrecognized filename pattern"
