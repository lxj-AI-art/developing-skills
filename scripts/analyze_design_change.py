#!/usr/bin/env python3
"""Compare approved-design candidates and enumerate invalidated implementation/eval evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from design_gate import validate_design


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path); parser.add_argument("new", type=Path)
    parser.add_argument("--old-traceability", type=Path); parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        old = validate_design(args.old, True); new = validate_design(args.new, True)
        if not old["valid"] or not new["valid"]: raise ValueError("both designs must pass the ready structural gate")
        old_items = {x["id"]: x for x in old["design_items"]}; new_items = {x["id"]: x for x in new["design_items"]}
        added = sorted(new_items.keys() - old_items.keys()); removed = sorted(old_items.keys() - new_items.keys())
        changed = sorted(key for key in old_items.keys() & new_items.keys() if old_items[key] != new_items[key])
        unchanged = sorted(old_items.keys() & new_items.keys() - set(changed))
        affected_files: set[str] = set(); affected_evals: set[str] = set()
        if args.old_traceability:
            trace = json.loads(args.old_traceability.read_text(encoding="utf-8"))
            for mapping in trace.get("mappings", []):
                if mapping.get("design_item_id") not in set(removed + changed): continue
                affected_files.update(x.get("path") for x in mapping.get("implementation", []) if isinstance(x, dict) and x.get("kind") == "file" and x.get("path"))
                affected_evals.update(x.get("case_id") for x in mapping.get("evaluations", []) if isinstance(x, dict) and x.get("case_id"))
        critical_removed = [key for key in removed if old_items[key].get("criticality") == "critical"]
        gate = "no_change" if not (added or removed or changed) else ("incompatible" if critical_removed else "revalidation_required")
        result: dict[str, Any] = {"schema_version": "1.0", "old_design_id": old["design_id"], "new_design_id": new["design_id"], "gate": gate, "changes": {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged}, "critical_removed": critical_removed, "invalidated_evidence": {"implementation_paths": sorted(affected_files), "eval_case_ids": sorted(affected_evals)}, "required_actions": [] if gate == "no_change" else ["issue a new approval for the new design_id", "update traceability for affected design items", "rerun affected regression and holdout evidence", "rerun conformance review"]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists(): raise ValueError("refusing to overwrite existing impact report")
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr); return 2


if __name__ == "__main__": sys.exit(main())
