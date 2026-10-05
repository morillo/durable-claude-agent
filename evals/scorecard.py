"""Aggregate per-example records into a scorecard (JSON + Markdown)."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def aggregate(records: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    n = len(records)
    exec_app = [r for r in records if r["execution"]["applicable"]]
    exec_ok = sum(1 for r in exec_app if r["execution"]["match"])
    risk_ok = sum(1 for r in records if r["risk"]["match"])
    rec_app = [r for r in records if r["retrieval"]["applicable"]]
    recall = statistics.fmean(r["retrieval"]["recall"] for r in rec_app) if rec_app else None
    judged = [r for r in records if r["judge"] is not None]
    judge = statistics.fmean(r["judge"]["score"] for r in judged) if judged else None
    appr_app = [r for r in records if r["approval"]["applicable"]]
    appr_ok = sum(1 for r in appr_app if r["approval"]["ok"])
    latencies = [r["wall_seconds"] for r in records]

    by_tier: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        groups[r["tier"]].append(r)
    for tier, rs in groups.items():
        ea = [r for r in rs if r["execution"]["applicable"]]
        ra = [r for r in rs if r["retrieval"]["applicable"]]
        jd = [r for r in rs if r["judge"] is not None]
        aa = [r for r in rs if r["approval"]["applicable"]]
        by_tier[tier] = {
            "n": len(rs),
            "execution_accuracy": _rate(sum(1 for r in ea if r["execution"]["match"]), len(ea)),
            "risk_accuracy": _rate(sum(1 for r in rs if r["risk"]["match"]), len(rs)),
            "retrieval_recall": round(statistics.fmean(r["retrieval"]["recall"] for r in ra), 4)
            if ra
            else None,
            "tool_use_judge": round(statistics.fmean(r["judge"]["score"] for r in jd), 4)
            if jd
            else None,
            "approval_behavior": _rate(sum(1 for r in aa if r["approval"]["ok"]), len(aa)),
        }

    failures = []
    for r in records:
        why = []
        if r["execution"]["applicable"] and not r["execution"]["match"]:
            why.append(
                "execution"
                + (f" ({r['execution']['error']})" if r["execution"].get("error") else "")
            )
        if not r["risk"]["match"]:
            why.append(f"risk expected={r['risk']['expected']} actual={r['risk']['actual']}")
        if r["retrieval"]["applicable"] and r["retrieval"]["recall"] < 1.0:
            why.append(f"retrieval missed {r['retrieval']['missed']}")
        if r["judge"] is not None and r["judge"]["score"] == 0:
            why.append(f"judge: {r['judge']['explanation']}")
        if r["approval"]["applicable"] and not r["approval"]["ok"]:
            why.append(f"approval: {r['approval']['detail']}")
        if r["stage"] == "failed":
            why.append(f"workflow failed: {r.get('error')}")
        if why:
            failures.append(
                {
                    "id": r["id"],
                    "tier": r["tier"],
                    "question": r["question"],
                    "sql": r["sql"],
                    "why": why,
                }
            )

    wf_cost = sum(r["usage"]["cost_usd"] for r in records)
    judge_cost = sum((r["judge_usage"] or {}).get("cost_usd", 0.0) for r in records)
    return {
        "n": n,
        "metrics": {
            "execution_accuracy": _rate(exec_ok, len(exec_app)),
            "execution_applicable": len(exec_app),
            "risk_accuracy": _rate(risk_ok, n),
            "retrieval_recall_at_k": round(recall, 4) if recall is not None else None,
            "tool_use_judge": round(judge, 4) if judge is not None else None,
            "approval_behavior": _rate(appr_ok, len(appr_app)),
            "approval_applicable": len(appr_app),
            "completed_without_workflow_failure": _rate(
                sum(1 for r in records if r["stage"] != "failed"), n
            ),
        },
        "by_tier": by_tier,
        "cost": {
            "workflow_usd": round(wf_cost, 4),
            "judge_usd": round(judge_cost, 4),
            "total_usd": round(wf_cost + judge_cost, 4),
            "input_tokens": sum(r["usage"]["input_tokens"] for r in records),
            "output_tokens": sum(r["usage"]["output_tokens"] for r in records),
            "cache_read_tokens": sum(r["usage"]["cache_read_tokens"] for r in records),
            "cached_responses": sum(r["usage"]["cached_responses"] for r in records),
            "api_calls": sum(r["usage"]["calls"] for r in records),
        },
        "latency_seconds": {
            "mean": round(statistics.fmean(latencies), 1) if latencies else None,
            "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 1)
            if latencies
            else None,
        },
        "failures": failures,
        "config": config,
    }


def render_markdown(card: dict[str, Any]) -> str:
    m, c, lat = card["metrics"], card["cost"], card["latency_seconds"]
    lines = [
        "# Eval scorecard",
        "",
        f"{card['n']} questions · models {card['config'].get('model_fast')} / {card['config'].get('model_strong')} · "
        f"effort {card['config'].get('llm_effort')} · {card['config'].get('timestamp')}",
        "",
        "| Metric | Score | Basis |",
        "|---|---|---|",
        f"| Execution accuracy | **{_pct(m['execution_accuracy'])}** | result sets equal on {m['execution_applicable']} questions with gold SQL |",
        f"| Risk classification accuracy | **{_pct(m['risk_accuracy'])}** | deterministic class vs expected, all {card['n']} |",
        f"| Retrieval recall@k | **{_pct(m['retrieval_recall_at_k'])}** | expected governance sections among retrieved passages |",
        f"| Tool-use judge | **{_pct(m['tool_use_judge'])}** | Claude-graded: necessary, sequenced, sufficient |",
        f"| Approval behavior | **{_pct(m['approval_behavior'])}** | {m['approval_applicable']} policy-violating questions gated before any execution |",
        f"| Runs without workflow failure | {_pct(m['completed_without_workflow_failure'])} | orchestration health |",
        "",
        "## By tier",
        "",
        "| Tier | n | Exec acc | Risk acc | Recall@k | Judge | Approval |",
        "|---|---|---|---|---|---|---|",
    ]
    for tier, t in card["by_tier"].items():
        lines.append(
            f"| {tier} | {t['n']} | {_pct(t['execution_accuracy'])} | {_pct(t['risk_accuracy'])} | "
            f"{_pct(t['retrieval_recall'])} | {_pct(t['tool_use_judge'])} | {_pct(t['approval_behavior'])} |"
        )
    lines += [
        "",
        "## Cost and latency",
        "",
        f"- Workflow spend: ${c['workflow_usd']:.4f} · judge spend: ${c['judge_usd']:.4f} · **total ${c['total_usd']:.4f}**",
        f"- Tokens: {c['input_tokens']:,} in / {c['output_tokens']:,} out / {c['cache_read_tokens']:,} cache reads · "
        f"{c['api_calls']} API calls, {c['cached_responses']} served from the local response cache",
        f"- Wall time per question: mean {lat['mean']}s, p95 {lat['p95']}s (includes Temporal scheduling and tool calls)",
        "",
        f"## Failures ({len(card['failures'])})",
        "",
    ]
    if not card["failures"]:
        lines.append("None.")
    for f in card["failures"]:
        lines.append(f"- **{f['id']}** ({f['tier']}): {f['question']}")
        for w in f["why"]:
            lines.append(f"  - {w}")
        if f.get("sql"):
            lines.append(f"  - sql: `{f['sql']}`")
    lines += ["", "## Config", "", "```json", json.dumps(card["config"], indent=2), "```", ""]
    return "\n".join(lines)


def write_outputs(out_dir: Path, records: list[dict[str, Any]], card: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r, default=str) for r in records) + "\n"
    )
    (out_dir / "scorecard.json").write_text(json.dumps(card, indent=2, default=str) + "\n")
    (out_dir / "scorecard.md").write_text(render_markdown(card))
