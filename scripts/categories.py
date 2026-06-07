"""
categories.py - Build and load the test-name -> category mapping.

Categories are derived from the correlation submodule's kernel_tests
directory structure and the sim-correlation_tests.star suite lists.

The generated db/categories.json can be hand-edited after creation;
subsequent runs will NOT overwrite it unless --rebuild is passed.
"""

import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORR_ROOT = _REPO_ROOT / "correlation"
_CATEGORIES_FILE = _REPO_ROOT / "db" / "categories.json"

# Folder name inside kernel_tests -> canonical category name
_FOLDER_TO_CATEGORY = {
    "elementwise":  "elementwise",
    "normalization": "normalization",
    "cast":         "cast",
    "memory":       "memory",
    "vme":          "vme",
    "legacy":       "legacy",
    "deadlock":     "deadlock",
    "cce":          "cce",
}


def _test_name_from_path(test_path: str) -> str:
    """
    Extract bare test name from a star-file path like
    'elementwise/test_asm_add_bf16.py' -> 'add_bf16'
    """
    stem = Path(test_path).stem          # 'test_asm_add_bf16'
    return re.sub(r"^test_asm_", "", stem)  # 'add_bf16'


def build_categories(force: bool = False) -> dict:
    """
    Build category map from the correlation submodule and write to
    db/categories.json.  Returns the resulting dict.

    If the file already exists and force=False, the existing file is
    loaded and returned without modification.
    """
    if _CATEGORIES_FILE.exists() and not force:
        return load_categories()

    categories: dict[str, str] = {}

    # --- Method 1: walk kernel_tests subdirectories ---
    kernel_tests_dir = _CORR_ROOT / "kernel_tests"
    if kernel_tests_dir.exists():
        for folder in kernel_tests_dir.iterdir():
            if not folder.is_dir():
                continue
            cat = _FOLDER_TO_CATEGORY.get(folder.name, folder.name)
            for py_file in folder.glob("test_asm_*.py"):
                test_name = re.sub(r"^test_asm_", "", py_file.stem)
                categories[test_name] = cat

    # --- Method 2: parse star file suite lists ---
    star_file = _CORR_ROOT / "tests" / "sim-correlation_tests.star"
    if star_file.exists():
        star_text = star_file.read_text()
        # Match tuples like ("folder/test_asm_<name>.py", ...)
        for m in re.finditer(r'"((\w+)/test_asm_(\w+)\.py)"', star_text):
            folder = m.group(2)
            raw_name = m.group(3)
            cat = _FOLDER_TO_CATEGORY.get(folder, folder)
            if raw_name not in categories:
                categories[raw_name] = cat

    _CATEGORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CATEGORIES_FILE.write_text(
        json.dumps(dict(sorted(categories.items())), indent=2) + "\n"
    )
    print(f"[categories] Wrote {len(categories)} entries to {_CATEGORIES_FILE}")
    return categories


def load_categories() -> dict:
    """Load categories from db/categories.json, building it if absent."""
    if not _CATEGORIES_FILE.exists():
        return build_categories()
    return json.loads(_CATEGORIES_FILE.read_text())


def lookup(test_name: str, categories: dict | None = None) -> str:
    """
    Return the category for *test_name*.

    Matching strategy (in order):
    1. Exact match on test_name
    2. Exact match on the 'base' name (strip trailing dtype tokens)
    3. Prefix match (test_name starts with a known key)
    4. Fallback: "unknown"
    """
    if categories is None:
        categories = load_categories()

    if test_name in categories:
        return categories[test_name]

    # Strip trailing dtype tokens and try again
    _DTYPES = {"bf16", "fp32", "fp16", "fp8", "mxfp8",
               "int8", "int16", "int32", "uint8", "uint16"}
    tokens = test_name.split("_")
    while tokens and tokens[-1] in _DTYPES:
        tokens.pop()
    base = "_".join(tokens)
    if base and base in categories:
        return categories[base]

    # Prefix match
    for key, cat in categories.items():
        if test_name.startswith(key):
            return cat

    return "unknown"
