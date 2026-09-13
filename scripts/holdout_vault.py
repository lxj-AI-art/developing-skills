#!/usr/bin/env python3
"""Create and audit a local governed holdout vault with access budgets and exposure tracking."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_eval_set import load_json, validate


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"refusing to overwrite {path}")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def token_bytes(token_file: Path) -> bytes:
    try:
        value = token_file.read_text(encoding="utf-8").strip().encode("utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read token file: {exc}") from None
    if not value:
        raise ValueError("token file is empty")
    return value


def within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def append_ledger(vault: Path, token: bytes, event: dict[str, Any]) -> dict[str, Any]:
    ledger = vault / "access-ledger.jsonl"
    previous = "0" * 64
    if ledger.exists():
        lines = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines:
            previous = json.loads(lines[-1])["entry_hash"]
    entry = {**event, "timestamp": utc_now(), "previous_hash": previous}
    entry_hash = hmac.new(token, canonical(entry), hashlib.sha256).hexdigest()
    entry["entry_hash"] = entry_hash
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def verify_ledger(vault: Path, token: bytes) -> tuple[bool, list[dict[str, Any]], list[str]]:
    ledger = vault / "access-ledger.jsonl"
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    previous = "0" * 64
    if not ledger.exists():
        return True, entries, errors
    for index, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"ledger line {index} is invalid JSON")
            continue
        claimed = entry.pop("entry_hash", None)
        if entry.get("previous_hash") != previous:
            errors.append(f"ledger line {index} chain mismatch")
        actual = hmac.new(token, canonical(entry), hashlib.sha256).hexdigest()
        if not isinstance(claimed, str) or not hmac.compare_digest(claimed, actual):
            errors.append(f"ledger line {index} signature mismatch")
        entry["entry_hash"] = claimed
        previous = str(claimed)
        entries.append(entry)
    return not errors, entries, errors


def create_vault(eval_path: Path, vault: Path, visible: Path, max_accesses: int) -> dict[str, Any]:
    data = load_json(eval_path)
    if not isinstance(data, dict):
        raise ValueError("eval set root must be an object")
    errors, _, counts = validate(data, eval_path, True)
    if errors:
        raise ValueError("eval set does not pass validation: " + "; ".join(errors))
    holdouts = [case for case in data["cases"] if case.get("split") == "holdout"]
    if not holdouts:
        raise ValueError("eval set contains no holdout cases")
    if vault.exists():
        raise ValueError("vault destination already exists")
    if visible.exists():
        raise ValueError("visible eval destination already exists")
    vault.mkdir(parents=True, mode=0o700)
    files_root = vault / "files"
    files_root.mkdir(mode=0o700)
    holdout_files = sorted({relative for case in holdouts for relative in case.get("files", [])})
    for relative in holdout_files:
        source = (eval_path.parent / relative).resolve()
        target = files_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    holdout_data = {
        "schema_version": data["schema_version"],
        "skill_name": data["skill_name"],
        "policy": data.get("policy", {}),
        "cases": holdouts,
    }
    write_new(vault / "holdout.json", holdout_data)
    token = secrets.token_urlsafe(32)
    token_path = vault / "token.txt"
    token_path.write_text(token + "\n", encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    visible_data = dict(data)
    visible_data["cases"] = [case for case in data["cases"] if case.get("split") != "holdout"]
    visible_data["holdout_authority"] = {
        "type": "local-governed-vault",
        "vault_id": digest(holdout_data),
        "case_count": len(holdouts),
        "hard_isolation": False,
    }
    write_new(visible, visible_data)
    manifest = {
        "schema_version": "1.0",
        "status": "sealed",
        "vault_id": digest(holdout_data),
        "source_eval_id": digest(data),
        "holdout_case_count": len(holdouts),
        "holdout_file_count": len(holdout_files),
        "max_final_accesses": max_accesses,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "hard_isolation": False,
        "created_at": utc_now(),
        "counts": counts,
    }
    write_new(vault / "manifest.json", manifest)
    append_ledger(vault, token.encode("utf-8"), {"event": "sealed", "purpose": "create", "candidate_id": None, "result": "success"})
    return {**manifest, "vault": str(vault), "visible_eval": str(visible), "token_file": str(token_path)}


def checkout(vault: Path, token_file: Path, purpose: str, candidate_id: str, output: Path, visible_eval: Path | None = None) -> dict[str, Any]:
    if within(output, vault):
        raise ValueError("checkout output must be outside the vault")
    manifest = load_json(vault / "manifest.json")
    token = token_bytes(token_file)
    if hashlib.sha256(token).hexdigest() != manifest.get("token_sha256"):
        raise ValueError("token does not authorize this vault")
    valid, entries, ledger_errors = verify_ledger(vault, token)
    if not valid:
        raise ValueError("vault ledger integrity failed: " + "; ".join(ledger_errors))
    exposed = any(entry.get("event") == "exposed_to_modifier" for entry in entries)
    final_accesses = sum(1 for entry in entries if entry.get("event") == "checkout" and entry.get("purpose") == "final_selection" and entry.get("result") == "success")
    if purpose == "final_selection":
        if exposed:
            raise ValueError("holdout has been exposed to a modifier and is no longer eligible for final selection")
        if final_accesses >= int(manifest.get("max_final_accesses", 1)):
            raise ValueError("holdout final-selection access budget is exhausted")
    if output.exists():
        raise ValueError("checkout output already exists")
    holdout = load_json(vault / "holdout.json")
    output.mkdir(parents=True)
    visible = load_json(visible_eval) if visible_eval else None
    if visible is not None:
        authority = visible.get("holdout_authority", {}) if isinstance(visible.get("holdout_authority"), dict) else {}
        if authority.get("vault_id") != manifest.get("vault_id"):
            raise ValueError("visible eval is not bound to this vault")
    visible_cases = [dict(case) for case in visible.get("cases", [])] if isinstance(visible, dict) else []
    holdout_cases = [dict(case) for case in holdout.get("cases", [])]
    data = {
        "schema_version": holdout["schema_version"],
        "skill_name": holdout["skill_name"],
        "policy": visible.get("policy", holdout.get("policy", {})) if isinstance(visible, dict) else holdout.get("policy", {}),
        "cases": visible_cases + holdout_cases,
        "holdout_authority": {"type": "local-governed-vault", "vault_id": manifest["vault_id"], "ledger_entry_pending": True, "hard_isolation": False},
    }
    for case in visible_cases:
        original = list(case.get("files", []))
        case["files"] = [f"inputs/visible/{relative}" for relative in original]
        for relative in original:
            source = (visible_eval.parent / relative).resolve()
            target = output / "inputs" / "visible" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for case in holdout_cases:
        case["files"] = [f"inputs/holdout/{relative}" for relative in case.get("files", [])]
    write_new(output / "eval-set.json", data)
    for source in sorted((vault / "files").rglob("*")):
        if source.is_file():
            relative = source.relative_to(vault / "files")
            target = output / "inputs" / "holdout" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    event = "exposed_to_modifier" if purpose == "modifier_debug" else "checkout"
    entry = append_ledger(vault, token, {"event": event, "purpose": purpose, "candidate_id": candidate_id, "result": "success", "output_id": digest(data)})
    data["holdout_authority"]["ledger_entry_pending"] = False
    data["holdout_authority"]["ledger_entry_hash"] = entry["entry_hash"]
    (output / "eval-set.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "released", "vault_id": manifest["vault_id"], "purpose": purpose, "candidate_id": candidate_id, "output": str(output), "eval_set": str(output / "eval-set.json"), "ledger_entry_hash": entry["entry_hash"], "holdout_eligible": purpose != "modifier_debug" and not exposed}


def status(vault: Path, token_file: Path) -> dict[str, Any]:
    manifest = load_json(vault / "manifest.json")
    token = token_bytes(token_file)
    authorized = hashlib.sha256(token).hexdigest() == manifest.get("token_sha256")
    if not authorized:
        return {"status": "unauthorized", "vault_id": manifest.get("vault_id"), "ledger_valid": None}
    valid, entries, errors = verify_ledger(vault, token)
    final_accesses = sum(1 for entry in entries if entry.get("event") == "checkout" and entry.get("purpose") == "final_selection" and entry.get("result") == "success")
    exposed = any(entry.get("event") == "exposed_to_modifier" for entry in entries)
    return {
        "status": "valid" if valid else "tampered",
        "vault_id": manifest.get("vault_id"),
        "ledger_valid": valid,
        "ledger_errors": errors,
        "entries": len(entries),
        "last_entry_hash": entries[-1]["entry_hash"] if entries else None,
        "final_accesses": final_accesses,
        "remaining_final_accesses": max(0, int(manifest.get("max_final_accesses", 1)) - final_accesses),
        "exposed_to_modifier": exposed,
        "holdout_eligible": valid and not exposed,
        "hard_isolation": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create")
    create.add_argument("--eval-set", required=True, type=Path)
    create.add_argument("--vault", required=True, type=Path)
    create.add_argument("--visible-eval", required=True, type=Path)
    create.add_argument("--max-final-accesses", type=int, default=1)
    check = sub.add_parser("status")
    check.add_argument("--vault", required=True, type=Path)
    check.add_argument("--token-file", required=True, type=Path)
    check.add_argument("--output", type=Path)
    release = sub.add_parser("checkout")
    release.add_argument("--vault", required=True, type=Path)
    release.add_argument("--token-file", required=True, type=Path)
    release.add_argument("--purpose", choices=("final_selection", "expert_audit", "modifier_debug"), default="final_selection")
    release.add_argument("--candidate-id", required=True)
    release.add_argument("--output", required=True, type=Path)
    release.add_argument("--visible-eval", type=Path, help="optional train/regression set produced by create; checkout then assembles a full governed eval set")
    args = parser.parse_args()
    try:
        if args.action == "create":
            if args.max_final_accesses < 1:
                raise ValueError("max final accesses must be >= 1")
            result = create_vault(args.eval_set.resolve(), args.vault.resolve(), args.visible_eval.resolve(), args.max_final_accesses)
        elif args.action == "checkout":
            result = checkout(args.vault.resolve(), args.token_file.resolve(), args.purpose, args.candidate_id, args.output.resolve(), args.visible_eval.resolve() if args.visible_eval else None)
        else:
            result = status(args.vault.resolve(), args.token_file.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.action == "status" and args.output:
        write_new(args.output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"sealed", "released", "valid"} else 1


if __name__ == "__main__":
    sys.exit(main())
