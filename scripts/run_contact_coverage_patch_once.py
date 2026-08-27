#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

PATCH_PATH = Path(__file__).with_name("apply_contact_coverage_patch_once.py")
spec = importlib.util.spec_from_file_location("contact_coverage_patch", PATCH_PATH)
if spec is None or spec.loader is None:
    raise SystemExit(f"could not load patch module: {PATCH_PATH}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

updated = 0
for operation in module.OPERATIONS:
    if (
        operation.get("kind") == "replace_once"
        and operation.get("path") == "scripts/build_g37_41_fuma.py"
        and str(operation.get("old", "")).startswith("- 電話付き企業:")
    ):
        operation["old"] = str(operation["old"]).replace("\n", "\\n")
        operation["new"] = str(operation["new"]).replace("\n", "\\n")
        updated += 1

if updated != 1:
    raise SystemExit(f"expected one portable README operation, found {updated}")
module.main()
