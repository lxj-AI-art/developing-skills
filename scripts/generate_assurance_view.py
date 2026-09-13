#!/usr/bin/env python3
"""Generate a standalone HTML view linking design, candidate, eval, assurance, and promotion evidence."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


def load_optional(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from None
    return value if isinstance(value, dict) else {}


def esc(value: Any) -> str:
    return html.escape("—" if value is None else str(value))


def status_badge(value: Any) -> str:
    text = str(value or "unknown")
    css = "ok" if text in {"pass", "candidate_is_best", "ready_for_human_review", "valid"} else ("warn" if text in {"inconclusive", "not_required", "running"} else "bad")
    return f'<span class="badge {css}">{esc(text)}</span>'


def render(workspace: Path, packet: dict[str, Any] | None = None) -> str:
    history = load_optional(workspace / "history.json")
    selection = load_optional(workspace / "selection.json")
    benchmark = load_optional(workspace / "benchmark.json")
    trace = load_optional(workspace / "design" / "traceability.json")
    if not trace:
        candidates = sorted((workspace / "assurance").glob("*/traceability.json")) if (workspace / "assurance").is_dir() else []
        if candidates:
            trace = load_optional(candidates[-1])
    rows = []
    attribution_rows = []
    change_rows = []
    for item in history.get("iterations", []):
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{esc(item.get('iteration'))}</td><td><code>{esc(str(item.get('candidate_id', ''))[:16])}</code></td>"
            f"<td>{esc(item.get('development_runs'))}</td><td>{esc(item.get('failed_candidate_development_runs'))}</td>"
            f"<td>{status_badge(item.get('decision') or item.get('stop_reason') or 'continued')}</td>"
            "</tr>"
        )
        for attribution in item.get("attributions", []):
            if isinstance(attribution, dict):
                attribution_rows.append(f"<tr><td>{esc(item.get('iteration'))}</td><td>{esc(attribution.get('case_id'))}</td><td>{esc(attribution.get('responsible_layer'))}</td><td>{esc(attribution.get('confidence'))}</td><td>{esc(attribution.get('observed_failure'))}</td></tr>")
        manifest_path = item.get("change_manifest")
        if isinstance(manifest_path, str):
            manifest = load_optional(Path(manifest_path))
            change_rows.append(f"<tr><td>{esc(item.get('iteration'))}</td><td>{esc(', '.join(manifest.get('responsible_layers', [])))}</td><td>{esc(', '.join(manifest.get('changed_files', [])))}</td><td>{esc(manifest.get('change_summary'))}</td><td><code>{esc(str(manifest.get('new_candidate_id',''))[:16])}</code></td></tr>")
    mappings = []
    for mapping in trace.get("mappings", []):
        if not isinstance(mapping, dict):
            continue
        files = [entry.get("path") or entry.get("evidence_ref") for entry in mapping.get("implementation", []) if isinstance(entry, dict)]
        evals = [f"{entry.get('case_id')}:{','.join(str(x) for x in entry.get('expectation_ids', []))}" for entry in mapping.get("evaluations", []) if isinstance(entry, dict)]
        mappings.append(f"<tr><td><code>{esc(mapping.get('design_item_id'))}</code></td><td>{esc(', '.join(str(x) for x in files))}</td><td>{esc(', '.join(evals))}</td></tr>")
    integrity = benchmark.get("integrity", {}) if isinstance(benchmark.get("integrity"), dict) else {}
    integrity_cards = "".join(f'<div class="metric"><span>{esc(key)}</span>{status_badge("pass" if value else "fail")}</div>' for key, value in sorted(integrity.items()) if isinstance(value, bool))
    governance = benchmark.get("governance", {}) if isinstance(benchmark.get("governance"), dict) else {}
    governance_cards = "".join(f'<div class="metric"><span>{esc(key)}</span>{status_badge(value.get("status") or value.get("gate"))}</div>' for key, value in sorted(governance.items()) if isinstance(value, dict))
    packet_html = ""
    if packet:
        gate_rows = "".join(f"<tr><td>{esc(name)}</td><td>{status_badge(value.get('status'))}</td><td><code>{esc(value.get('artifact_id') or value.get('candidate_id') or '')}</code></td></tr>" for name, value in packet.get("gates", {}).items() if isinstance(value, dict))
        packet_html = f"<section><h2>Promotion packet</h2><p>{status_badge(packet.get('status'))} Human approval required: {esc(packet.get('requires_human_approval'))}</p><table><tr><th>Gate</th><th>Status</th><th>Identity</th></tr>{gate_rows}</table></section>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>developing-skills assurance view</title><style>
body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#f4f6f8;color:#17212b}}main{{max-width:1180px;margin:auto;padding:28px}}section{{background:white;border:1px solid #dce2e8;border-radius:10px;margin:16px 0;padding:18px;box-shadow:0 2px 8px #0000000b}}h1,h2{{margin-top:0}}code{{font-size:12px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #e6eaee;text-align:left;padding:9px;vertical-align:top}}.badge{{display:inline-block;border-radius:999px;padding:2px 9px;font-weight:650}}.ok{{background:#dff5e5;color:#176b31}}.warn{{background:#fff2cc;color:#795b00}}.bad{{background:#ffe0df;color:#8e2420}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}.metric{{border:1px solid #e1e6eb;border-radius:8px;padding:10px;display:flex;justify-content:space-between;gap:10px}}
</style></head><body><main><h1>Skill assurance view</h1>
<section><h2>Current state</h2><div class="metrics"><div class="metric"><span>History</span>{status_badge(history.get('status'))}</div><div class="metric"><span>Selection</span>{status_badge(selection.get('decision'))}</div><div class="metric"><span>Design</span><code>{esc(str(history.get('design_id',''))[:16])}</code></div><div class="metric"><span>Eval set</span><code>{esc(str(history.get('eval_set_id',''))[:16])}</code></div></div></section>
<section><h2>Evidence integrity</h2><div class="metrics">{integrity_cards or '<p>No benchmark integrity evidence.</p>'}</div></section>
<section><h2>Governance</h2><div class="metrics">{governance_cards or '<p>No governance reports attached.</p>'}</div></section>
<section><h2>Iterations</h2><table><tr><th>Iteration</th><th>Candidate</th><th>Runs</th><th>Failures</th><th>Outcome</th></tr>{''.join(rows) or '<tr><td colspan="5">No iterations recorded.</td></tr>'}</table></section>
<section><h2>Design → implementation → evaluation</h2><table><tr><th>Design item</th><th>Implementation</th><th>Eval expectations</th></tr>{''.join(mappings) or '<tr><td colspan="3">No traceability mappings found.</td></tr>'}</table></section>
<section><h2>Failure attribution</h2><table><tr><th>Iteration</th><th>Case</th><th>Layer</th><th>Confidence</th><th>Observed failure</th></tr>{''.join(attribution_rows) or '<tr><td colspan="5">No failure attributions recorded.</td></tr>'}</table></section>
<section><h2>Candidate changes</h2><table><tr><th>From iteration</th><th>Layer</th><th>Files</th><th>Summary</th><th>New candidate</th></tr>{''.join(change_rows) or '<tr><td colspan="5">No candidate changes recorded.</td></tr>'}</table></section>
{packet_html}</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--promotion-packet", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not (args.workspace / "history.json").is_file():
            raise ValueError("workspace history.json is required")
        if args.output.exists():
            raise ValueError("refusing to overwrite output")
        packet = load_optional(args.promotion_packet) if args.promotion_packet else None
        document = render(args.workspace.resolve(), packet)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(document, encoding="utf-8")
        print(args.output)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
