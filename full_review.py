"""One-shot full risk review of all unscored Release 7.7 commits.

Fetches SVN diffs on demand, scores each commit with Claude, tracks real
token cost from API responses, and aborts before exceeding COST_CAP.

Usage:
    python full_review.py [--version-id 609] [--cap 10.0]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys

import anthropic
from anthropic import AsyncAnthropic

from app import db, storage
from app.config import settings
from app.refresh import _is_noise_commit, _truncate_for_storage
from app.risk import RISK_TOOL, SYSTEM_PROMPT, RiskResult, build_user_prompt

# Haiku 4.5 pricing (USD per token)
_IN = 0.80 / 1_000_000
_OUT = 4.00 / 1_000_000
_CACHE_WRITE = 1.00 / 1_000_000
_CACHE_READ = 0.08 / 1_000_000


_CHAR_CAP = 20_000  # ~5k tokens; hard ceiling — keeps total request well under 10k token/min rate limit


def _svn_diff(revision: str) -> str | None:
    if not settings.svn_binary or not settings.svn_repo_url:
        return None
    try:
        r = subprocess.run(
            [settings.svn_binary, "diff", "--change", revision, settings.svn_repo_url,
             "--no-auth-cache", "--non-interactive"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        raw = r.stdout or None
        if raw and len(raw) > _CHAR_CAP:
            raw = raw[:_CHAR_CAP] + "\n[diff truncated: exceeded character limit]"
        return raw
    except Exception:
        return None


async def _score(client: AsyncAnthropic, *, subject: str, description: str,
                 message: str, diff: str | None) -> tuple[RiskResult | None, float]:
    prompt, _ = build_user_prompt(
        issue_subject=subject,
        issue_description=description,
        commit_message=message,
        diff=diff,
        diff_cap=settings.diff_line_cap,
    )
    msg = await client.messages.create(
        model=settings.risk_model,
        max_tokens=512,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[RISK_TOOL],
        tool_choice={"type": "tool", "name": "record_risk"},
        messages=[{"role": "user", "content": prompt}],
    )
    u = msg.usage
    cost = (
        u.input_tokens * _IN +
        u.output_tokens * _OUT +
        getattr(u, "cache_read_input_tokens", 0) * _CACHE_READ +
        getattr(u, "cache_creation_input_tokens", 0) * _CACHE_WRITE
    )
    result = None
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_risk":
            d = block.input
            result = RiskResult(
                level=d.get("level", "med"),
                reasons=list(d.get("reasons", [])),
                affected_areas=list(d.get("affected_areas", [])),
                raw_response=json.dumps(d),
            )
    return result, cost


async def run(version_id: int, cost_cap: float) -> None:
    async with db.connect() as conn:
        cur = await conn.execute(
            """
            SELECT c.revision, c.message, c.diff_text, i.subject,
                   json_extract(i.raw_json, '$.description') as description
            FROM changesets c
            JOIN issues i ON c.issue_id = i.id
            LEFT JOIN risk_assessments r ON c.revision = r.revision
            WHERE i.version_id = ? AND r.revision IS NULL
            ORDER BY CAST(c.revision AS INTEGER)
            """,
            (version_id,),
        )
        rows = await cur.fetchall()

    candidates = [r for r in rows if not _is_noise_commit(r[1] or "")]
    print(f"Unscored non-noise commits: {len(candidates)}  |  cap: ${cost_cap:.2f}\n")

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    cumulative = 0.0
    scored = 0

    for revision, message, diff_text, subject, description in candidates:
        # Apply char cap to diffs already in DB (may have been stored before cap existed)
        if diff_text and len(diff_text) > _CHAR_CAP:
            diff_text = diff_text[:_CHAR_CAP] + "\n[diff truncated: exceeded character limit]"

        # Fetch diff from SVN if not already stored
        if not diff_text:
            raw = _svn_diff(revision)
            if raw:
                stored, truncated = _truncate_for_storage(raw, settings.diff_line_cap)
                async with db.connect() as conn:
                    await conn.execute(
                        "UPDATE changesets SET diff_text=?, diff_truncated=? WHERE revision=?",
                        (stored, truncated, revision),
                    )
                    await conn.commit()
                diff_text = stored

        # Retry with exponential backoff on rate limit errors
        delay = 60
        result, cost = None, 0.0
        for attempt in range(6):
            try:
                result, cost = await _score(
                    client,
                    subject=subject or "",
                    description=description or "",
                    message=message or "",
                    diff=diff_text,
                )
                break
            except anthropic.RateLimitError:
                print(f"  r{revision}  rate limit, waiting {delay}s...", flush=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            except Exception as exc:
                print(f"  r{revision}  ERROR: {exc}", flush=True)
                break

        cumulative += cost
        scored += 1

        if result:
            async with db.connect() as conn:
                await storage.save_risk(
                    conn,
                    revision=revision,
                    model=settings.risk_model,
                    level=result.level,
                    reasons=result.reasons,
                    affected_areas=result.affected_areas,
                    raw_response=result.raw_response,
                )
            label = {"low": "low ", "med": "med ", "high": "HIGH"}.get(result.level, result.level)
            print(f"  r{revision}  [{label}]  ${cumulative:.4f}  {(message or '')[:70]}", flush=True)

        if cumulative >= cost_cap:
            print(f"\nABORTED: ${cost_cap:.2f} cap reached after {scored} commits.")
            return

        await asyncio.sleep(60)  # ~1 req/min, stays under 10k token/min rate limit

    print(f"\nDone: {scored}/{len(candidates)} commits scored.  Total cost: ${cumulative:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-id", type=int, default=609, help="Redmine version DB id")
    parser.add_argument("--cap", type=float, default=10.0, help="Cost cap in USD")
    args = parser.parse_args()
    asyncio.run(run(args.version_id, args.cap))


if __name__ == "__main__":
    main()
