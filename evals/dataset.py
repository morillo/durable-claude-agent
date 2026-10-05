"""Dataset loading and gold-result hashing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from durable_agent.lakehouse import Lakehouse

DATASET_PATH = Path(__file__).resolve().parent / "dataset.jsonl"
TIERS = ("simple_aggregate", "multi_join", "time_window", "ambiguous", "policy_violating")


@dataclass(frozen=True)
class Example:
    id: str
    tier: str
    question: str
    gold_sql: str | None
    expected_risk: str
    expected_sections: tuple[str, ...]
    notes: str = ""
    gold_rows_hash: str | None = None

    @property
    def should_execute(self) -> bool:
        return self.gold_sql is not None


def load_dataset(path: Path = DATASET_PATH) -> list[Example]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d: dict[str, Any] = json.loads(line)
        rows.append(
            Example(
                id=d["id"],
                tier=d["tier"],
                question=d["question"],
                gold_sql=d.get("gold_sql"),
                expected_risk=d["expected_risk"],
                expected_sections=tuple(d.get("expected_sections", [])),
                notes=d.get("notes", ""),
                gold_rows_hash=d.get("gold_rows_hash"),
            )
        )
    ids = [r.id for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate ids in dataset")
    bad_tier = [r.id for r in rows if r.tier not in TIERS]
    if bad_tier:
        raise ValueError(f"unknown tier on {bad_tier}")
    return rows


def rows_hash(rows: list[list[Any]]) -> str:
    """Order-insensitive hash of a normalized result set (see scorers.normalize_rows)."""
    from evals.scorers import normalize_rows

    canonical = json.dumps(normalize_rows(rows), sort_keys=True, default=str)
    # Prefixed and short on purpose: an integrity check, not a credential (keeps secret scanners quiet).
    return "gold:" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def gold_hashes(lakehouse: Lakehouse, examples: list[Example]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ex in examples:
        if ex.gold_sql:
            res = lakehouse.execute(ex.gold_sql, max_rows=1000, timeout_s=30)
            out[ex.id] = rows_hash(res.rows)
    return out


def write_gold_hashes(path: Path, hashes: dict[str, str]) -> None:
    """Rewrite dataset.jsonl with ``gold_rows_hash`` filled in (``make eval-gold``)."""
    out_lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d["id"] in hashes:
            d["gold_rows_hash"] = hashes[d["id"]]
        out_lines.append(json.dumps(d, ensure_ascii=False))
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
