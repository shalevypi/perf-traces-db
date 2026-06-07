# TODO / Pending Tasks

## Traces

### Restore castdown_fp32 and castup_fp32 PLDM traces
These are genuine fp32-dtype cast traces (different from the bf16 cast variants)
currently stranded in the .bak folder.  They should be restored to an active
date folder so the scanner picks them up.

Files to restore:
  vcd_traces/pldm/20260520.bak/asm_castdown_fp32.vcd
  vcd_traces/pldm/20260520.bak/asm_castup_fp32.vcd

Action: mkdir vcd_traces/pldm/20260520 and move the two files there,
then run: python3 scripts/main.py scan

### Run sim trace for eight_queens
PLDM trace exists in vcd_traces/pldm/20260520.bak/pldm_trace_test_asm_eight_queens.py.vcd
Sim trace not yet available.  Once run, add both to an active date folder.

### Decide on mul_bf16_1024
File: vcd_traces/pldm/20260520.bak/asm_mul_bf16_1024.vcd
Unclear if this is a distinct test or a duplicate of mul_bf16_bf16.
Needs investigation before restoring or deleting.

---

## Scanner

### ~~Add multi-process support to scan~~ DONE
The correlation submodule already has _parse_vcds_parallel (ProcessPoolExecutor).
Plugged into scan_traces.py - correlation runs in parallel during a full rescan.
Speedup: ~13s -> ~3.7s on 254 files with 8 workers.
Control with --workers N flag.
