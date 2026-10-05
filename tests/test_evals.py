"""Dataset integrity, scorer semantics, and scorecard rendering (no LLM, no Temporal)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from evals.dataset import DATASET_PATH, TIERS, Example, load_dataset
from evals.scorecard import aggregate, render_markdown
from evals.scorers import (
    approval_behavior,
    compare_results,
    execution_accuracy,
    normalize_rows,
    retrieval_recall,
    risk_accuracy,
)

from durable_agent.lakehouse import Lakehouse
from durable_agent.risk import Policy, classify
from tests.conftest import GOVERNANCE_DIR

# ---------------------------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------------------------


def test_dataset_shape() -> None:
    ds = load_dataset(DATASET_PATH)
    assert 25 <= len(ds) <= 40
    assert {e.tier for e in ds} == set(TIERS)
    assert all(e.expected_risk in {"safe", "needs_approval", "blocked"} for e in ds)
    assert all(e.gold_sql is None for e in ds if e.expected_risk == "blocked")
    assert all(e.gold_sql is not None for e in ds if e.expected_risk != "blocked")


def test_gold_sql_runs_and_matches_expected_risk(lakehouse: Lakehouse, policy: Policy) -> None:
    """Every gold query executes on the (small) test lakehouse and classifies as expected."""
    schema = lakehouse.schema()
    for ex in load_dataset(DATASET_PATH):
        if ex.gold_sql is None:
            continue
        assert classify(ex.gold_sql, schema, policy).level.value == ex.expected_risk, ex.id
        lakehouse.execute(ex.gold_sql, max_rows=1000, timeout_s=10)


def test_expected_sections_exist_in_corpus() -> None:
    from durable_agent.retrieval import load_corpus

    citations = {f"{c.source}#{c.anchor}" for c in load_corpus(GOVERNANCE_DIR)}
    for ex in load_dataset(DATASET_PATH):
        missing = set(ex.expected_sections) - citations
        assert not missing, (ex.id, missing)


# ---------------------------------------------------------------------------------------------
# scorers
# ---------------------------------------------------------------------------------------------


def test_compare_results_is_order_and_column_insensitive() -> None:
    assert compare_results([[1, "a"], [2, "b"]], [["b", 2], ["a", 1]])
    assert compare_results([[1.004]], [[1.0]])  # 2 dp rounding
    assert not compare_results([[1.01]], [[1.0]])
    assert not compare_results([[1]], [[1], [2]])
    assert not compare_results(None, [[1]])


def test_normalize_handles_temporal_and_month_strings() -> None:
    rows = normalize_rows([[dt.datetime(2025, 1, 1), 5], ["2025-01", 5], [dt.date(2025, 1, 1), 5]])
    assert rows[0] == rows[1] == rows[2]


def test_execution_accuracy_against_lakehouse(lakehouse: Lakehouse) -> None:
    gold = "SELECT count(*) FROM orders WHERE status <> 'cancelled'"
    assert execution_accuracy(
        lakehouse, gold, "SELECT count(*) AS n FROM orders WHERE status != 'cancelled'"
    )["match"]
    assert not execution_accuracy(lakehouse, gold, "SELECT count(*) FROM orders")["match"]
    bad = execution_accuracy(lakehouse, gold, "SELECT nope FROM orders")
    assert bad["match"] is False and bad["error"]
    assert execution_accuracy(lakehouse, None, "SELECT 1")["applicable"] is False
    assert execution_accuracy(lakehouse, gold, None)["error"] == "no SQL generated"


def test_risk_and_retrieval_scorers() -> None:
    assert risk_accuracy("safe", "safe")["match"] and not risk_accuracy("safe", "blocked")["match"]
    r = retrieval_recall(("a#x", "b#y"), ["b#y", "c#z"])
    assert r["recall"] == 0.5 and r["missed"] == ["a#x"]
    assert retrieval_recall((), ["a"])["applicable"] is False


def test_approval_behavior_scorer() -> None:
    ok = approval_behavior(
        "needs_approval", ["generating", "awaiting_approval", "executing", "completed"], True, False
    )
    assert ok["ok"] is True
    bad = approval_behavior("needs_approval", ["generating", "executing", "completed"], True, True)
    assert bad["ok"] is False
    assert approval_behavior("blocked", ["generating", "blocked"], False, False)["ok"] is True
    assert (
        approval_behavior("blocked", ["generating", "executing", "completed"], True, False)["ok"]
        is False
    )
    assert approval_behavior("safe", ["completed"], True, False)["applicable"] is False


# ---------------------------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------------------------


def _record(
    id_: str,
    tier: str,
    *,
    exec_match: bool | None,
    risk_match: bool,
    recall: float | None,
    judge: int | None,
    appr: bool | None,
    stage: str = "completed",
) -> dict:  # type: ignore[type-arg]
    return {
        "id": id_,
        "tier": tier,
        "question": f"q {id_}",
        "expected_risk": "safe",
        "stage": stage,
        "stages": [stage],
        "sql": "SELECT 1",
        "gold_sql": "SELECT 1",
        "error": None,
        "tool_calls": ["get_schema"],
        "execution": {"applicable": exec_match is not None, "match": exec_match, "error": None},
        "risk": {
            "expected": "safe",
            "actual": "safe" if risk_match else "blocked",
            "match": risk_match,
        },
        "retrieval": {
            "applicable": recall is not None,
            "recall": recall,
            "hit": [],
            "missed": ["x"] if recall is not None and recall < 1 else [],
        },
        "judge": None
        if judge is None
        else {
            "score": judge,
            "necessary": True,
            "sequenced": True,
            "sufficient": True,
            "explanation": "ok",
        },
        "judge_usage": None if judge is None else {"cost_usd": 0.001},
        "approval": {"applicable": appr is not None, "ok": appr, "detail": ""},
        "usage": {
            "model": "m",
            "calls": 3,
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 5,
            "cache_write_tokens": 0,
            "cost_usd": 0.01,
            "cached_responses": 1,
        },
        "wall_seconds": 12.0,
        "resumed_from_checkpoint": False,
    }


def test_aggregate_and_render(tmp_path: Path) -> None:
    records = [
        _record(
            "a",
            "simple_aggregate",
            exec_match=True,
            risk_match=True,
            recall=1.0,
            judge=1,
            appr=None,
        ),
        _record(
            "b",
            "simple_aggregate",
            exec_match=False,
            risk_match=True,
            recall=0.5,
            judge=0,
            appr=None,
        ),
        _record(
            "c",
            "policy_violating",
            exec_match=None,
            risk_match=False,
            recall=1.0,
            judge=1,
            appr=False,
            stage="blocked",
        ),
    ]
    card = aggregate(
        records, {"model_fast": "h", "model_strong": "s", "llm_effort": "medium", "timestamp": "t"}
    )
    m = card["metrics"]
    assert m["execution_accuracy"] == 0.5 and m["execution_applicable"] == 2
    assert abs(m["risk_accuracy"] - 2 / 3) < 1e-3
    assert abs(m["retrieval_recall_at_k"] - (2.5 / 3)) < 1e-3
    assert abs(m["tool_use_judge"] - 2 / 3) < 1e-3
    assert m["approval_behavior"] == 0.0 and m["approval_applicable"] == 1
    assert card["cost"]["total_usd"] == 0.033 and card["cost"]["cached_responses"] == 3
    assert {f["id"] for f in card["failures"]} == {"b", "c"}
    md = render_markdown(card)
    assert (
        "| Execution accuracy | **50.0%**" in md
        and "## Failures (2)" in md
        and "policy_violating" in md
    )
    assert Example  # imported for type completeness
