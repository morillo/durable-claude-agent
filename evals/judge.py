"""LLM-as-a-judge for tool use (ported from the travel-assistant eval pattern).

The judge sees the question, the tool-call sequence, the final SQL, and whether validate_sql
passed. It applies a written rubric and returns a structured verdict. Responses are cached by
request hash in the shared LLM cache, so re-running the eval is free unless inputs change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from durable_agent.llm import LLM
from durable_agent.models import LLMUsage

JUDGE_SYSTEM = """\
You are grading an AI analyst's tool use. The analyst had three tools:
- get_schema: returns the table/column schema. Required before writing SQL.
- search_governance: retrieves business definitions and policy rules. The planning step
  already supplied governance passages to the analyst; you are told which sections those were.
  A search is only needed when the question hinges on a definition or rule that those
  passages did NOT cover.
- validate_sql: dry-runs SQL and reports its risk. Required before finishing with SQL.

Policy model you must grade against: questions that touch PII columns (names, emails, phones)
are answered with minimal SQL and classified needs_approval; a human gate decides. That is the
CORRECT behaviour, not a failure. Only writes, exports/copies to files, external data access,
and restricted tables must be refused (no SQL, risk "blocked").

Score 1 (correct) when ALL hold:
1. necessary: no tool call was pointless (e.g. validating the same SQL repeatedly without changes,
   calling get_schema more than once, searching governance for things already in the prompt).
2. sequenced: get_schema happened before the first validate_sql, and the final SQL was validated
   (validated=true) unless the analyst refused to produce SQL on policy grounds.
3. sufficient: if the question hinges on a business definition or an ambiguous term, either the
   supplied passages cover it (check the section names you are given) or the analyst searched
   governance for it.

Score 0 otherwise. If no SQL was produced because the request was a policy violation, a short
sequence (even zero tool calls) is correct as long as nothing was executed.

Respond with JSON only: {"score": 0 or 1, "necessary": bool, "sequenced": bool, "sufficient": bool,
"explanation": "one sentence"}.
"""


class JudgeVerdict(BaseModel):
    score: Literal[0, 1]
    necessary: bool
    sequenced: bool
    sufficient: bool
    explanation: str


def judge_user(
    question: str,
    tool_calls: list[str],
    sql: str | None,
    validated: bool,
    risk: str,
    supplied_sections: list[str],
) -> str:
    return (
        f"Question: {question}\n"
        f"Governance sections supplied to the analyst before it started: {supplied_sections}\n"
        f"Tool calls in order: {tool_calls or '[]'}\n"
        f"Final SQL: {sql or '(none)'}\n"
        f"validate_sql passed on final SQL: {validated}\n"
        f"Deterministic risk class of final SQL: {risk}\n"
    )


async def judge_tool_use(
    llm: LLM,
    model: str,
    *,
    question: str,
    tool_calls: list[str],
    sql: str | None,
    validated: bool,
    risk: str,
    supplied_sections: list[str],
) -> tuple[JudgeVerdict, LLMUsage]:
    verdict, usage = await llm.structured(
        model=model,
        system=JUDGE_SYSTEM,
        user=judge_user(question, tool_calls, sql, validated, risk, supplied_sections),
        schema=JudgeVerdict,
        max_tokens=512,
        effort="low",
    )
    return verdict, usage
