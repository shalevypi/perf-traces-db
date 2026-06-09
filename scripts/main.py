"""
main.py - CLI entry point for the VCD traces performance database.

Commands:
    scan    - Scan trace folders and update db/traces.csv
    report  - Generate Markdown performance report from db/traces.csv
    show    - Print a quick summary from db/traces.csv to the terminal

Examples:
    python scripts/main.py scan
    python scripts/main.py scan --traces-root /path/to/traces --force-reparse
    python scripts/main.py report
    python scripts/main.py report --vcores 32 --stdout
    python scripts/main.py show
    python scripts/main.py show --test add_bf16_bf16 --source sim
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


def cmd_scan(args):
    from scan_traces import scan
    scan(
        traces_root=args.traces_root,
        force_reparse=args.force_reparse,
        workers=args.workers,
    )


def cmd_report(args):
    from generate_report import generate_report
    generate_report(
        vcores_filter=args.vcores,
        date_filter=args.date,
        category_filter=args.category,
        by_category=args.by_category,
        to_stdout=args.stdout,
    )


def cmd_show(args):
    import csv
    csv_path = _REPO_ROOT / "db" / "traces.csv"
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found. Run 'scan' first.")
        sys.exit(1)

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    if args.test:
        rows = [r for r in rows if args.test in r.get("test_name", "")]
    if args.source:
        rows = [r for r in rows if r.get("source") == args.source]
    if args.run_type:
        rows = [r for r in rows if r.get("run_type") == args.run_type]

    if not rows:
        print("No matching rows.")
        return

    # Print compact summary table
    col_w = {
        "source": 5, "date": 10, "test_name": 35,
        "num_vcores": 7, "run_type": 12, "total_cycles": 12,
        "category": 14,
    }
    hdr = "  ".join(k.ljust(v) for k, v in col_w.items())
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        line = "  ".join(
            str(r.get(k, "-") or "-").ljust(v) for k, v in col_w.items()
        )
        print(line)
    print(f"\n{len(rows)} row(s)")


def main():
    parser = argparse.ArgumentParser(
        description="VCD traces performance database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # --- scan ---
    p_scan = sub.add_parser("scan", help="Scan trace folders, update db/traces.csv")
    p_scan.add_argument(
        "--traces-root",
        default=None,
        metavar="PATH",
        help="Root directory containing sim/ and pldm/ folders "
             "(default: repo root)",
    )
    p_scan.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Number of parallel worker processes for correlation "
             "(default: CPU count, capped at 8)",
    )
    p_scan.add_argument(
        "--force-reparse",
        action="store_true",
        help="Re-process all VCDs even if already in the database",
    )

    # --- report ---
    p_report = sub.add_parser(
        "report",
        help="Generate Markdown report from db/traces.csv",
    )
    p_report.add_argument(
        "--date",
        default=None,
        metavar="DATE",
        help="Filter to a specific date folder (YYYYMMDD or YYYY-MM-DD)",
    )
    p_report.add_argument(
        "--category",
        default=None,
        metavar="NAME",
        help="Filter report to a single category "
             "(e.g. elementwise, cast, memory, vme, normalization)",
    )
    p_report.add_argument(
        "--by-category",
        action="store_true",
        help="Split report into one table per category",
    )
    p_report.add_argument(
        "--vcores",
        type=int,
        default=None,
        metavar="N",
        help="Filter report to a specific vcore count",
    )
    p_report.add_argument(
        "--stdout",
        action="store_true",
        help="Print report to terminal instead of writing db/report.md",
    )

    # --- show ---
    p_show = sub.add_parser("show", help="Print CSV summary to terminal")
    p_show.add_argument("--test", default=None, metavar="NAME",
                        help="Filter by test name (substring match)")
    p_show.add_argument("--source", default=None, choices=["sim", "pldm"],
                        help="Filter by source")
    p_show.add_argument("--run-type", default=None,
                        choices=["measurement", "warmup", "unknown"],
                        help="Filter by run type")

    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "show":
        cmd_show(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
