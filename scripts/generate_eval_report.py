#!/usr/bin/env python3
"""Generate a dependency-free static HTML review report."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError(f"root must be an object: {path}")
    return value


def show(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def percent(value: Any) -> str:
    return "—" if not isinstance(value, (int, float)) or isinstance(value, bool) else f"{value:.1%}"


def render(benchmark: dict[str, Any], selection: dict[str, Any] | None) -> str:
    summary_rows: list[str] = []
    for config, config_summary in benchmark.get("configurations", {}).items():
        rows = [("all", config_summary)] + list(config_summary.get("by_split", {}).items())
        for split, row in rows:
            counts = row.get("status_counts", {})
            unresolved = sum(int(counts.get(key, 0)) for key in ("blocked", "inconclusive", "invalid_case"))
            score = row.get("score", {})
            summary_rows.append(
                "<tr>"
                f"<td>{html.escape(str(config))}</td><td>{html.escape(str(split))}</td>"
                f"<td>{show(row.get('runs'))}</td><td>{percent(row.get('determinate_pass_rate'))}</td>"
                f"<td>{show(score.get('mean'))} ± {show(score.get('stddev'))}</td>"
                f"<td>{unresolved}</td><td>{show(row.get('critical_failure_runs'))}</td>"
                "</tr>"
            )

    run_rows: list[str] = []
    paired: dict[tuple[str, str, int], dict[str, dict[str, Any]]] = {}
    for run in benchmark.get("runs", []):
        pair_key = (str(run.get("case_id", "")), str(run.get("split", "")), int(run.get("run_number", 0)))
        paired.setdefault(pair_key, {})[str(run.get("configuration", ""))] = run
        run_id = f"{run.get('case_id', '')}:{run.get('configuration', '')}:run-{run.get('run_number', '')}"
        artifacts = "<br>".join(html.escape(str(item)) for item in run.get("artifacts", [])) or "—"
        grading_path = html.escape(str(run.get("grading_path", "")))
        run_rows.append(
            "<tr>"
            f"<td>{html.escape(str(run.get('case_id', '')))}</td>"
            f"<td>{html.escape(str(run.get('split', '')))}</td>"
            f"<td>{html.escape(str(run.get('configuration', '')))}</td>"
            f"<td>{show(run.get('run_number'))}</td>"
            f"<td class='status {html.escape(str(run.get('status', '')))}'>{html.escape(str(run.get('status', '')))}</td>"
            f"<td>{show(run.get('score'))}</td><td>{show(len(run.get('critical_failures', [])))}</td>"
            f"<td>{html.escape(str(run.get('execution_independence', 'unverified')))}</td>"
            f"<td>{artifacts}<details><summary>grading</summary><code>{grading_path}</code></details></td>"
            f"<td><textarea data-run-id=\"{html.escape(run_id, quote=True)}\" aria-label=\"Feedback for {html.escape(run_id, quote=True)}\"></textarea></td>"
            "</tr>"
        )

    pair_rows: list[str] = []
    for (case_id, split, run_number), versions in sorted(paired.items()):
        candidate = versions.get("candidate", {})
        baseline = versions.get("baseline", {})
        if not candidate or not baseline:
            continue
        score_delta = None
        if isinstance(candidate.get("score"), (int, float)) and isinstance(baseline.get("score"), (int, float)):
            score_delta = float(candidate["score"]) - float(baseline["score"])
        pair_rows.append(
            "<tr>"
            f"<td>{html.escape(case_id)}</td><td>{html.escape(split)}</td><td>{run_number}</td>"
            f"<td class='status {html.escape(str(candidate.get('status', '')))}'>{html.escape(str(candidate.get('status', '')))}</td>"
            f"<td>{show(candidate.get('score'))}</td>"
            f"<td class='status {html.escape(str(baseline.get('status', '')))}'>{html.escape(str(baseline.get('status', '')))}</td>"
            f"<td>{show(baseline.get('score'))}</td><td>{show(score_delta)}</td>"
            "</tr>"
        )

    analysis = benchmark.get("analysis", {}) if isinstance(benchmark.get("analysis"), dict) else {}
    finding_rows: list[str] = []
    for finding in analysis.get("findings", []):
        if not isinstance(finding, dict):
            continue
        finding_rows.append(
            "<tr>"
            f"<td>{html.escape(str(finding.get('severity', '')))}</td>"
            f"<td>{html.escape(str(finding.get('code', '')))}</td>"
            f"<td>{html.escape(str(finding.get('case_id', '—')))}</td>"
            f"<td>{html.escape(str(finding.get('message', '')))}</td>"
            "</tr>"
        )

    blind = benchmark.get("blind_comparison", {}) if isinstance(benchmark.get("blind_comparison"), dict) else {}
    blind_rows: list[str] = []
    for judgment in blind.get("judgments", []):
        if not isinstance(judgment, dict):
            continue
        rubric = judgment.get("rubric", {}) if isinstance(judgment.get("rubric"), dict) else {}
        a_rubric = rubric.get("A", {}) if isinstance(rubric.get("A"), dict) else {}
        b_rubric = rubric.get("B", {}) if isinstance(rubric.get("B"), dict) else {}
        a_score = f"{show(a_rubric.get('content'))} / {show(a_rubric.get('structure'))}"
        b_score = f"{show(b_rubric.get('content'))} / {show(b_rubric.get('structure'))}"
        blind_rows.append(
            "<tr>"
            f"<td>{html.escape(str(judgment.get('case_id', '')))}</td>"
            f"<td>{html.escape(str(judgment.get('split', '')))}</td>"
            f"<td>{show(judgment.get('run_number'))}</td>"
            f"<td>{html.escape(str(judgment.get('winner', '')))}</td>"
            f"<td>{html.escape(str(judgment.get('confidence', '—')))}</td>"
            f"<td>{html.escape(a_score)}</td>"
            f"<td>{html.escape(b_score)}</td>"
            f"<td>{html.escape('; '.join(str(item) for item in judgment.get('evidence', [])))}</td>"
            "</tr>"
        )

    decision = "not generated"
    decision_details = ""
    if selection:
        decision = str(selection.get("decision", "unknown"))
        details = list(selection.get("failed_gates", [])) + list(selection.get("uncertainties", [])) + list(selection.get("reasons", []))
        decision_details = "".join(f"<li>{html.escape(str(item))}</li>" for item in details)

    warnings = "".join(f"<li>{html.escape(str(item))}</li>" for item in benchmark.get("warnings", []))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skill Eval Review</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#172033;background:#f6f8fb}}main{{max-width:1200px;margin:auto}}
.card{{background:white;border:1px solid #dce2ea;border-radius:12px;padding:1.2rem;margin:1rem 0;box-shadow:0 2px 10px #1720330d}}
table{{border-collapse:collapse;width:100%;font-size:.92rem}}th,td{{border-bottom:1px solid #e5e9ef;padding:.65rem;text-align:left;vertical-align:top}}th{{background:#f0f3f8}}
.decision{{font-size:1.2rem;font-weight:700}}.pass{{color:#08783e}}.fail{{color:#b42318}}.blocked,.inconclusive,.invalid_case{{color:#9a6700}}
code{{white-space:pre-wrap;word-break:break-all}}details{{margin-top:.35rem}}ul{{margin:.4rem 0}}textarea{{min-width:16rem;min-height:4rem}}button{{padding:.65rem 1rem;border:0;border-radius:8px;background:#2357d8;color:white;font-weight:600;cursor:pointer}}
</style></head><body><main>
<h1>Skill Eval Review</h1>
<section class="card"><div>Generated: {html.escape(str(benchmark.get('generated_at', 'unknown')))}</div>
<div class="decision">Decision: {html.escape(decision)}</div><ul>{decision_details}</ul></section>
<section class="card"><h2>Benchmark</h2><table><thead><tr><th>Configuration</th><th>Split</th><th>Runs</th><th>Pass rate</th><th>Score mean ± stddev</th><th>Unresolved</th><th>Critical</th></tr></thead><tbody>{''.join(summary_rows)}</tbody></table></section>
<section class="card"><h2>Paired comparison</h2><table><thead><tr><th>Case</th><th>Split</th><th>Run</th><th>Candidate status</th><th>Candidate score</th><th>Baseline status</th><th>Baseline score</th><th>Δ score</th></tr></thead><tbody>{''.join(pair_rows) or '<tr><td colspan="8">No complete pairs</td></tr>'}</tbody></table></section>
<section class="card"><h2>Benchmark analyzer</h2><p>Gate: <strong>{html.escape(str(analysis.get('gate', 'not run')))}</strong></p><table><thead><tr><th>Severity</th><th>Type</th><th>Case</th><th>Finding</th></tr></thead><tbody>{''.join(finding_rows) or '<tr><td colspan="4">No findings</td></tr>'}</tbody></table></section>
<section class="card"><h2>Blind comparison</h2><p>Candidate wins: {show(blind.get('candidate_wins'))}; baseline wins: {show(blind.get('baseline_wins'))}; ties: {show(blind.get('ties'))}; candidate win rate: {percent(blind.get('candidate_win_rate'))}</p><table><thead><tr><th>Case</th><th>Split</th><th>Run</th><th>Winner</th><th>Confidence</th><th>A content / structure</th><th>B content / structure</th><th>Reason</th></tr></thead><tbody>{''.join(blind_rows) or '<tr><td colspan="8">Not run</td></tr>'}</tbody></table></section>
<section class="card"><h2>Runs</h2><table><thead><tr><th>Case</th><th>Split</th><th>Configuration</th><th>Run</th><th>Status</th><th>Score</th><th>Critical</th><th>Isolation</th><th>Artifacts</th><th>Feedback</th></tr></thead><tbody>{''.join(run_rows)}</tbody></table><p><button id="download-feedback">Download feedback.json</button></p></section>
<section class="card"><h2>Warnings</h2><ul>{warnings or '<li>None</li>'}</ul></section>
</main><script>
document.getElementById('download-feedback').addEventListener('click',()=>{{
  const reviews=[...document.querySelectorAll('textarea[data-run-id]')].map(x=>({{run_id:x.dataset.runId,feedback:x.value,timestamp:new Date().toISOString()}}));
  const blob=new Blob([JSON.stringify({{reviews,status:'complete'}},null,2)],{{type:'application/json'}});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='feedback.json';a.click();URL.revokeObjectURL(a.href);
}});
</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        benchmark = read_json(args.benchmark)
        selection = read_json(args.selection) if args.selection else None
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    output = args.output or args.benchmark.with_name("eval-report.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(benchmark, selection), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
