# Implementation Plan: VCD Traces Performance Database

## Overview of What Was Found

**Folder structure:** `traces/{sim,pldm}/YYYYMMDD/`

**Four distinct filename patterns observed:**

| Pattern | Example | Found In |
|---|---|---|
| **A** (new sim) | `vcore_r0_t0_c0_d0_v0_asm_<test>_<din>_<dout>_<size>_num_vcores_<N>_partition_<P>_[warmup\|measurement].[vcd\|meta]` | sim/20260523 |
| **B** (new pldm) | `py_asm_<test>_<din>_<dout>_<size>_num_vcores_<N>_partition_<P>.vcd` | pldm/20260523 |
| **C** (old sim) | `vcore_sysc_trace_r0_t0_c0_d0_v0_test_asm_<test>.py.[vcd\|meta\|html]` | sim/20260520 |
| **D** (old pldm v1) | `pldm_trace_test_asm_<test>.py.vcd` | pldm/20260520 |
| **E** (old pldm v2) | `asm_<test>.vcd` (e.g. `asm_cast_mxfp8_bf16.vcd`) | pldm/20260520 |
| **Noise** (skip+warn) | any `.vcd` whose name does NOT start with `py_asm_`, `pldm_trace_`, or `asm_` -- e.g. `vec_add_*.vcd`, `add_bf16_*.vcd`, `auto_export.vcd`, `waveform.vcd`, `test_add_fp32.vcd` | both |

**Confluence table columns:** Test Category, Test Name, Description, Sim Cycles, PLDM Cycles, Delta, Diff%

---

## Phase 1 -- Project Bootstrap & Git Setup

**Goal:** Initialize a clean, reproducible project skeleton.

- `git init` in `/home/shay.levy/traces`
- Add correlation project as a git submodule:
  ```
  git submodule add https://github.com/Elmnt-Internal/ip.infra.sw.sim.correlation correlation/
  ```
- Create `.gitignore`:
  - Exclude all `*.vcd` files (too large)
  - Exclude `*.meta`, `*.ev`, `*.html` in trace dirs
  - Exclude `__pycache__/`, `.env`, etc.
  - **Keep** `db/traces.csv`, `db/categories.json`, `db/scan_state.json` (the database artefacts are small and valuable)
- Project directory layout:
  ```
  traces/
  ├── .gitignore
  ├── README.md
  ├── correlation/          <- submodule
  ├── db/
  │   ├── traces.csv        <- main database (in git)
  │   ├── categories.json   <- test -> category mapping (in git)
  │   ├── scan_state.json   <- dedup/already-parsed tracking (in git)
  │   └── skipped_files.log <- gitignored, regenerated each run
  ├── scripts/
  │   ├── parse_filename.py
  │   ├── run_correlation.py
  │   ├── scan_traces.py
  │   ├── generate_report.py
  │   └── main.py           <- CLI entry point
  └── requirements.txt
  ```
- First commit: skeleton + submodule, no VCDs

---

## Phase 2 -- Filename Parser (`parse_filename.py`)

**Goal:** Reliably extract structured metadata from any filename, warn on unknowns.

- One regex per pattern (A/B/C/D/E), tried in order
- Parsed fields returned as a dict:

| Field | Notes |
|---|---|
| `source` | `sim` or `pldm` |
| `date` | parsed from folder name (`YYYYMMDD`), stored as ISO, display as `DD/MM/YYYY` |
| `test_name` | e.g., `add_bf16`, `exp_fp32`, `softmax_fp32` |
| `dtype_in` | e.g., `bf16`, `fp32` -- `None` for old patterns |
| `dtype_out` | same |
| `size` | tensor/problem size param -- `None` if absent |
| `num_vcores` | integer, `None` means "all vcores" |
| `num_dies` | integer, default `1` if not in name |
| `partition` | integer (0 or 1) |
| `run_type` | `measurement`, `warmup`, or `unknown` (old traces have no suffix) |
| `file_path` | relative path from project root |

- **Pre-filter by prefix**: before attempting any regex, check that the filename starts with
  `py_asm_`, `pldm_trace_`, or `asm_`. Any `.vcd` that fails this check is silently skipped
  and logged to `db/skipped_files.log` with a clear WARNING (these are noise files).
- Files that pass the prefix check but match **no** pattern: emit
  `WARNING: skipping <path> -- unrecognized filename pattern` and write to `db/skipped_files.log`
- `.meta`, `.html`, `.ev`, `.csv` extensions always skipped silently (only `.vcd` is a valid trace target)

---

## Phase 3 -- Correlation Wrapper (`run_correlation.py`)

**Goal:** Drive the correlation submodule scripts and extract cycle metrics from a VCD.

- Study the correlation project's entry point and category star file on first implementation
- Wrapper calls the relevant script(s) with the VCD path + `.meta` sidecar if present
  (for `kernel_address`)
- Returns a dict:

| Field | Notes |
|---|---|
| `start_cycle` | absolute cycle when kernel window begins |
| `end_cycle` | absolute cycle when kernel window ends |
| `total_cycles` | `end_cycle - start_cycle` |
| `kernel_prolog_marker` | cycle offset of prolog end marker |

- Failures (e.g., VCD not parseable) return `None` for all fields + a warning; the row is still
  written to CSV with empty metric columns so it is tracked as "attempted"

---

## Phase 4 -- CSV Database Schema & Scanner (`scan_traces.py`)

**Goal:** Scan all trace folders, extract metrics, write to `db/traces.csv`, never duplicate.

### CSV Schema

| Column | Type | Description |
|---|---|---|
| `row_id` | str | SHA256 hash of `(source, date, test_name, dtype_in, dtype_out, size, num_vcores, num_dies, partition, run_type)` -- the dedup key |
| `source` | str | `sim` / `pldm` |
| `date` | str | ISO date (`YYYY-MM-DD`) |
| `test_name` | str | |
| `dtype_in` | str | |
| `dtype_out` | str | |
| `size` | int | |
| `num_vcores` | int | |
| `num_dies` | int | default 1 |
| `partition` | int | |
| `run_type` | str | `measurement` / `warmup` / `unknown` |
| `category` | str | from `categories.json` |
| `start_cycle` | int | from correlation |
| `end_cycle` | int | from correlation |
| `total_cycles` | int | |
| `kernel_prolog_marker` | int | |
| `warmup_cycles` | int | filled in post-scan by pairing rows |
| `meas_vs_warmup_ratio` | float | `measurement_cycles / warmup_cycles` for the same test config |
| `file_path` | str | relative path |
| `parsed_at` | str | ISO timestamp of when this row was written |

### Dedup / State Tracking (`scan_state.json`)

```json
{
  "<row_id>": {
    "file_path": "...",
    "file_mtime": 1234567890.0,
    "parsed_at": "2026-06-07T..."
  }
}
```

- Before running correlation on a file, compute `row_id`; if it exists in `scan_state.json`
  with the same `file_mtime` -> skip (already in CSV)
- If `file_mtime` changed -> re-parse and overwrite that row in the CSV

### Scanner Logic

1. Walk `sim/` and `pldm/` date folders
2. For each `.vcd`: call parser -> if None, warn and log to `db/skipped_files.log`
3. Check `scan_state.json` for dedup
4. Run correlation wrapper
5. Look up category from `categories.json`
6. Append/update row in `db/traces.csv`
7. Post-scan pass: for each unique config (test+dtype+size+vcores+dies+partition+source+date),
   pair `measurement` and `warmup` rows -> fill `warmup_cycles` and `meas_vs_warmup_ratio`
   on measurement rows
8. Write updated `scan_state.json`

---

## Phase 5 -- Category Mapping (`categories.json`)

**Goal:** Map `test_name` -> category (e.g., `basic`, `vme`, `cce`, `convert`, `activation`,
`normalization`).

- On first run: read the correlation project's star file (in the submodule) to auto-populate
  `categories.json`
- Manual overrides supported (the file is hand-editable and committed)
- Scanner uses this file; if a test name has no entry -> `category = "unknown"` + warning

---

## Phase 6 -- Confluence Report Generator (`generate_report.py`)

**Goal:** Read `db/traces.csv` and produce a plain Markdown file that can be copy/pasted
directly into Confluence without garbled characters.

- Output file: `db/report.md` (gitignored; regenerated on demand)
- Aggregation: for each `(test_name, num_vcores, num_dies)` config, pick the latest date's
  `measurement` rows for sim + pldm, compute `Delta` and `Diff%`
- Produces one or more Markdown tables matching the existing Confluence structure:
  `Category | Test Name | Description | Sim Cycles | PLDM Cycles | Delta | Diff%`
- Delta sign convention matches the existing page: `PLDM - Sim`; negative means PLDM is
  faster, positive means simulator is faster
- **No special or non-ASCII characters anywhere** -- no Unicode arrows, em-dashes, box
  drawing, Greek letters, or fancy quotes; plain `-` for missing values
- Support a `--vcores N` flag to filter the table to a specific vcore count (useful for
  generating per-configuration snapshots to paste as separate Confluence tables)
- Description text sourced from `db/test_descriptions.json` (hand-maintained, committed),
  falling back to empty string if not found
- A `--stdout` flag prints to terminal instead of writing the file (quick review before paste)

---

## Phase 7 -- CLI Entry Point & Docs (`main.py` + `README.md`)

**Goal:** Unified, documented interface.

```
python scripts/main.py scan [--traces-root /path/to/traces] [--force-reparse]
python scripts/main.py report [--vcores N] [--stdout]
python scripts/main.py show [--test <name>] [--source sim|pldm]
```

- `scan`: runs Phase 4 scanner
- `report`: runs Phase 6 report generator; writes `db/report.md`
- `show`: quick CSV query/summary printed to terminal
- `README.md`: covers prerequisites (Python env, `CONFLUENCE_API_TOKEN`), submodule init
  (`git submodule update --init --recursive`), quick-start, column schema reference

---

## Key Design Decisions Summary

| Decision | Choice | Rationale |
|---|---|---|
| VCDs in git? | No -- `.gitignore`'d | Files are too large |
| CSV in git? | Yes | Small, important, diffs are useful |
| Dedup key | SHA256 of logical fields | Filename-independent, survives renames |
| Re-parse trigger | `file_mtime` changed | Cheap, no need to hash large VCDs |
| Old-pattern traces | Parse best-effort, mark fields as `None` | Still want them in the DB |
| Warmup/measurement linking | Post-scan join by config key | Keeps CSV flat; no nested structures |
| Confluence output | Plain Markdown file, no API writes | Safe copy/paste, no encoding issues |

---

## Open Questions (to confirm before implementation)

1. **Old traces (20260520)** -- many PLDM files don't have dtype/size/vcores in the name.
   Should we try to infer them from a lookup table, or leave those fields `None` and include
   them as-is?

2. **Scan scope** -- should the scanner treat `.meta` sidecar files as hints (e.g.,
   `kernel_address`) and merge them into the corresponding VCD row, or ignore them?

3. **`num_vcores = None` (all vcores)** -- do you know what the "all vcores" default count is
   (e.g., 32)? Should we store the literal count or keep it as `null`?

4. **Report granularity** -- should `report` produce one combined table for all vcore counts,
   or one table per vcore count by default (with `--vcores` to narrow it down)?
