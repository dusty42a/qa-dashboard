from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from app.config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a QA risk-triage assistant. For each commit you receive, judge the regression \
risk it introduces to a product release, then emit a structured assessment.

Risk dimensions to weigh:
- Blast radius (how many call sites or files are touched, how central the modified code is).
- Change kind (refactor, behavioral change, config change, dependency bump, dead-code removal).
- Test coverage signal (does the diff add or change tests? are touched files traditionally \
covered? if you cannot tell, say so).
- Coupling to risky surfaces (auth, billing, data migrations, persistence, scheduling, \
external integrations, concurrency).
- Scope vs. ticket (does the diff overshoot what the ticket asks for?).

Levels:
- low: small, localized, well-scoped, clearly low-blast-radius.
- med: non-trivial scope, touches shared code, missing test signal, or partial uncertainty.
- high: touches risky surfaces, large diff, scope creep, schema/migration changes, or \
explicit uncertainty markers ("[diff truncated]", missing context).

Return ONLY a tool call to `record_risk`. Do not respond in prose.
"""

RISK_TOOL = {
    "name": "record_risk",
    "description": "Record the risk assessment for a single commit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "level": {"type": "string", "enum": ["low", "med", "high"]},
            "reasons": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short bullet reasons for the chosen level. 1-5 items.",
            },
            "affected_areas": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Functional areas touched (e.g. 'auth', 'reports', 'svn:trunk/lib/x.php'). "
                    "1-5 items."
                ),
            },
        },
        "required": ["level", "reasons", "affected_areas"],
    },
}


@dataclass
class RiskResult:
    level: str
    reasons: list[str]
    affected_areas: list[str]
    raw_response: str


def _truncate_diff(diff: str | None, cap: int) -> tuple[str, bool]:
    if not diff:
        return "[no diff available]", False
    lines = diff.splitlines()
    if len(lines) <= cap:
        return diff, False
    head = "\n".join(lines[:cap])
    return f"{head}\n[diff truncated: showed {cap} of {len(lines)} lines]", True


def build_user_prompt(
    *,
    issue_subject: str,
    issue_description: str,
    commit_message: str,
    diff: str | None,
    diff_cap: int,
) -> tuple[str, bool]:
    diff_text, truncated = _truncate_diff(diff, diff_cap)
    body = (
        f"Ticket: {issue_subject}\n\n"
        f"Ticket description:\n{issue_description or '(empty)'}\n\n"
        f"Commit message:\n{commit_message or '(empty)'}\n\n"
        f"Unified diff:\n```\n{diff_text}\n```\n"
    )
    return body, truncated


async def score_commit(
    *,
    issue_subject: str,
    issue_description: str,
    commit_message: str,
    diff: str | None,
) -> RiskResult | None:
    if not settings.risk_enabled or not settings.anthropic_api_key:
        return None

    user_prompt, _ = build_user_prompt(
        issue_subject=issue_subject,
        issue_description=issue_description,
        commit_message=commit_message,
        diff=diff,
        diff_cap=settings.diff_line_cap,
    )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        msg = await client.messages.create(
            model=settings.risk_model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[RISK_TOOL],
            tool_choice={"type": "tool", "name": "record_risk"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # network, auth, rate limit
        log.warning("risk scoring failed: %s", exc)
        return None

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_risk":
            data = block.input
            return RiskResult(
                level=data.get("level", "med"),
                reasons=list(data.get("reasons", [])),
                affected_areas=list(data.get("affected_areas", [])),
                raw_response=json.dumps(data),
            )

    log.warning("risk scoring returned no tool_use block")
    return None
