"""
run_correlation.py - Wrapper around the correlation submodule's
vcd_kernel_cycles.parse_kernel_window().

Returns a normalised dict with cycle metrics for one VCD file.
The correlation submodule must be initialised:
    git submodule update --init --recursive
"""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the correlation scripts importable
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORR_SCRIPTS = _REPO_ROOT / "correlation" / "scripts"

if not _CORR_SCRIPTS.exists():
    raise ImportError(
        f"Correlation submodule not found at {_CORR_SCRIPTS}. "
        "Run: git submodule update --init --recursive"
    )

if str(_CORR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CORR_SCRIPTS))

from vcd_kernel_cycles import parse_kernel_window  # noqa: E402


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_correlation(vcd_path: str) -> dict:
    """
    Run the correlation analysis on *vcd_path* and return a normalised dict.

    All cycle values are expressed as VCore cycles (auto-detected clock).

    Keys returned:
        vcore_mhz           int   - detected (or fallback) clock in MHz
        start_cycle         int   - kernel window start, cycles from VCD t=0
        end_cycle           int   - kernel window end,   cycles from VCD t=0
        total_cycles        int   - end_cycle - start_cycle  (== cycles_NMHz)
        kernel_prolog_marker int  - cycle of MARKER_KERNEL_BEGIN instruction,
                                    or None if not present in VCD
        error               str   - non-empty if parsing failed; all other
                                    fields are None in that case
    """
    empty = {
        "vcore_mhz": None,
        "start_cycle": None,
        "end_cycle": None,
        "total_cycles": None,
        "kernel_prolog_marker": None,
        "error": "",
    }

    try:
        r = parse_kernel_window(str(vcd_path))
    except Exception as exc:
        empty["error"] = str(exc)
        return empty

    mhz = r["vcore_mhz"]
    ts = r["timescale_ns"]
    first_tick = r.get("vcd_first_tick", 0)

    def _to_cycle(tick):
        if tick is None:
            return None
        return round((tick - first_tick) * ts * mhz / 1000)

    start_cycle = _to_cycle(r["start_tick"])
    end_cycle = _to_cycle(r["end_tick"])
    total_cycles = r.get(f"cycles_{mhz}MHz")

    marker_tick = r.get("marker_begin_tick")
    prolog_cycle = _to_cycle(marker_tick) if marker_tick is not None else None

    return {
        "vcore_mhz": mhz,
        "start_cycle": start_cycle,
        "end_cycle": end_cycle,
        "total_cycles": total_cycles,
        "kernel_prolog_marker": prolog_cycle,
        "error": "",
    }
