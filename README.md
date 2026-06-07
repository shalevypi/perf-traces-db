# VCD Traces Performance Database

Scans sim and PLDM VCD trace folders, extracts kernel cycle metrics via the
correlation submodule, and stores results in a CSV database.  A Markdown
report can be generated for copy/paste into Confluence.

## Prerequisites

- Python 3.10+
- SSH access to GitHub (for submodule clone)

## First-Time Setup

```
git submodule update --init --recursive
```

Set your Confluence API token if you need to reference the page:
```
export CONFLUENCE_API_TOKEN=<your-token>
```

## Usage

### Scan all trace folders

```
python scripts/main.py scan
```

Options:
- `--traces-root /path/to/traces` - override root directory (default: repo root)
- `--force-reparse` - re-process all VCDs even if already recorded

### Generate Markdown report

```
python scripts/main.py report
```

Output: `db/report.md` - plain Markdown, safe to paste into Confluence.

Options:
- `--vcores N` - include only traces with N vcores
- `--stdout` - print to terminal instead of writing the file

### Quick CSV view

```
python scripts/main.py show
python scripts/main.py show --test exp_fp32 --source sim
python scripts/main.py show --run-type measurement
```

## Directory Layout

```
traces/
+-- .gitignore
+-- README.md
+-- PLAN.md
+-- correlation/            <- git submodule (ip.infra.sw.sim.correlation)
+-- db/
|   +-- traces.csv          <- main database (committed)
|   +-- categories.json     <- test -> category map (committed, hand-editable)
|   +-- scan_state.json     <- dedup state (committed)
|   +-- test_descriptions.json  <- optional descriptions (committed)
|   +-- report.md           <- generated, NOT committed
|   +-- skipped_files.log   <- generated, NOT committed
+-- scripts/
|   +-- main.py             <- CLI entry point
|   +-- parse_filename.py   <- VCD filename parser
|   +-- run_correlation.py  <- correlation submodule wrapper
|   +-- scan_traces.py      <- folder scanner + CSV writer
|   +-- generate_report.py  <- Markdown report generator
|   +-- categories.py       <- category map builder/loader
+-- sim/                    <- NOT in git (*.vcd too large)
|   +-- YYYYMMDD/
+-- pldm/                   <- NOT in git (*.vcd too large)
    +-- YYYYMMDD/
```

## CSV Schema

| Column | Description |
|---|---|
| row_id | SHA-256 dedup key (first 16 hex chars of logical identity) |
| source | sim or pldm |
| date | ISO date (YYYY-MM-DD) of the trace folder |
| test_name | test body extracted from filename |
| dtype_in | input data type (best-effort, may be empty for old traces) |
| dtype_out | output data type |
| size | problem size parameter |
| num_vcores | number of VCores; empty means all VCores |
| num_dies | number of dies (default 1) |
| partition | address space partitioned between cores (0 or 1) |
| run_type | measurement, warmup, or unknown |
| category | from categories.json (elementwise, cast, memory, ...) |
| vcore_mhz | auto-detected VCore clock from VCD |
| start_cycle | kernel window start, cycles from VCD t=0 |
| end_cycle | kernel window end, cycles from VCD t=0 |
| total_cycles | end_cycle - start_cycle |
| kernel_prolog_marker | cycle of kernel-begin marker instruction |
| warmup_cycles | total_cycles of paired warmup run |
| meas_vs_warmup_ratio | measurement / warmup cycle ratio |
| file_path | path to the source .vcd file |
| parsed_at | ISO timestamp of when this row was written |

## Filename Patterns

| Pattern | Prefix | Example |
|---|---|---|
| A (new sim) | vcore_ | vcore_r0_t0_c0_d0_v0_asm_add_bf16_bf16_1024_num_vcores_8_partition_1_measurement.vcd |
| B (new pldm) | py_asm_ | py_asm_add_bf16_bf16_1024_num_vcores_32_partition_1.vcd |
| C (old sim) | vcore_ | vcore_sysc_trace_r0_t0_c0_d0_v0_test_asm_add_bf16.py.vcd |
| D (old pldm v1) | pldm_trace_ | pldm_trace_test_asm_add_bf16.py.vcd |
| E (old pldm v2) | asm_ | asm_cast_mxfp8_bf16.vcd |

Files that do not start with a valid prefix are logged as noise and skipped.

## Deduplication

Each VCD is identified by a 16-char SHA-256 hash of its logical fields
(source, date, test_name, dtypes, size, vcores, dies, partition, run_type).
A file is re-processed only if its mtime changes or --force-reparse is used.

## Adding New Descriptions

Edit `db/test_descriptions.json`:
```json
{
  "add_bf16_bf16": "Elementwise bf16 add: out[i] = a[i] + b[i]",
  "exp_fp32_fp32": "fp32 exp: out[i] = exp(in[i])"
}
```

## Confluence Page

Target: https://elementlabs.atlassian.net/wiki/spaces/RPI/pages/1089765982/
Copy the contents of db/report.md and paste into the page.
