from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Directory (relative to a task's worktree root) that holds the multi-stage workflow's
#: plan artifacts. Never committed -- see .gitignore (`.ai/plans/`, alongside the other
#: Scheduler-owned `.ai/` subdirectories it already excludes).
PLANS_DIR = Path(".ai/plans")

#: Read-only execution flags per agent for the PLAN_REVIEW gate (#280 requirement 4):
#: the reviewer must not be able to edit any file, only read the plan/repo and reason
#: about it.
READ_ONLY_SANDBOX_ARGS: dict[str, tuple[str, ...]] = {
    "claude": ("--tools", "Read,Bash"),
    "codex": ("--sandbox", "read-only"),
}

_VALID_VERDICTS = frozenset({"APPROVE", "REQUEST_CHANGES"})


class PlanVerdictError(ValueError):
    """Raised when a plan review verdict is missing, malformed, or ambiguous.

    Every caller must fail closed (escalate to NEEDS_HUMAN) on this error rather than
    guessing an outcome.
    """


@dataclass(frozen=True, slots=True)
class PlanVerdict:
    verdict: str
    summary: str
    findings: tuple[str, ...]


PLAN_REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"enum": ["APPROVE", "REQUEST_CHANGES"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


def ensure_plan_review_output_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(PLAN_REVIEW_OUTPUT_SCHEMA, indent=2), encoding="utf-8")


def plan_path(issue_number: int) -> Path:
    """Return the worktree-relative path of the plan artifact for an issue."""
    if issue_number <= 0:
        raise ValueError("issue number must be positive")
    return PLANS_DIR / f"{issue_number}.md"


_CODE_BLOCK_PATTERN = re.compile(r"(`{3,})(?i:json)?[\t ]*\r?\n?([\s\S]*?)\r?\n?\1")


def _extract_json_payload(raw: str) -> Any:
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_matches = _CODE_BLOCK_PATTERN.findall(text)
    if len(fence_matches) == 1:
        content = fence_matches[0][1].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as err:
            raise PlanVerdictError(f"plan review verdict is not valid JSON: {err}") from err
    elif len(fence_matches) > 1:
        candidates: list[dict[str, Any]] = []
        for _, content in fence_matches:
            try:
                parsed = json.loads(content.strip())
                if isinstance(parsed, dict) and "verdict" in parsed:
                    candidates.append(parsed)
            except json.JSONDecodeError:
                pass
        if len(candidates) == 1:
            return candidates[0]
        raise PlanVerdictError("multiple code blocks found in plan review verdict output")

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1].strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return json.loads(text)


def parse_verdict(raw: str) -> PlanVerdict:
    """Parse a Plan Reviewer's structured JSON verdict.

    Fails closed with `PlanVerdictError` on anything that is not exactly the expected
    shape: unparseable JSON, a non-object payload, a missing/unsupported `verdict`
    field, or wrongly-typed `summary`/`findings` fields.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PlanVerdictError("plan review verdict output is empty")
    try:
        payload: Any = _extract_json_payload(raw)
    except json.JSONDecodeError as error:
        raise PlanVerdictError(f"plan review verdict is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise PlanVerdictError("plan review verdict must be a JSON object")

    allowed_keys = {"verdict", "summary", "findings"}
    extra_keys = set(payload.keys()) - allowed_keys
    if extra_keys:
        raise PlanVerdictError(f"extra keys in plan review verdict: {sorted(extra_keys)}")

    verdict = payload.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise PlanVerdictError(f"unsupported or missing plan review verdict: {verdict!r}")

    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        raise PlanVerdictError("plan review verdict 'summary' must be a string")

    findings_raw = payload.get("findings", [])
    if not isinstance(findings_raw, list) or any(
        not isinstance(item, str) for item in findings_raw
    ):
        raise PlanVerdictError("plan review verdict 'findings' must be a list of strings")

    return PlanVerdict(verdict=verdict, summary=summary, findings=tuple(findings_raw))
