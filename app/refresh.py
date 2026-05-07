"""Refresh logic: fetch from Redmine into the SQLite cache, kick off risk scoring.

Single-process dedup via asyncio locks per scope. If a refresh is already
running for a scope, concurrent triggers no-op rather than stacking up.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import db, redmine, risk, storage
from app.config import settings

log = logging.getLogger(__name__)

_locks: dict[str, asyncio.Lock] = {}

# Cap concurrent outbound calls to Redmine so a busy version doesn't open
# 100 connections at once.
_REDMINE_CONCURRENCY = 5

# Commit-message prefixes (case-insensitive, applied AFTER stripping the
# leading "Issue #N - " prefix) that we never bother scoring. These are
# release-process noise — merges, tag commits, branch bookkeeping — that
# carry ~zero QA-risk signal but burn tokens like any other call.
_NOISE_PREFIXES = (
    "merge ",
    "tagging ",
    "creating tag",
)


def _strip_issue_prefix(message: str) -> str:
    """Strip the leading 'Issue #NNNNN - ' that Workbooks SVN messages use."""
    if not message:
        return ""
    import re

    return re.sub(r"^\s*Issue\s+#\d+\s*-\s*", "", message, count=1)


def _is_noise_commit(message: str) -> bool:
    body = _strip_issue_prefix(message).lstrip().lower()
    return any(body.startswith(p) for p in _NOISE_PREFIXES)


def _scoring_allowed_for_version(version_name: str) -> bool:
    allow = settings.risk_versions_set
    if not allow:
        return True
    return version_name in allow


def _lock(scope: str) -> asyncio.Lock:
    if scope not in _locks:
        _locks[scope] = asyncio.Lock()
    return _locks[scope]


# ---- versions list ---------------------------------------------------------


async def refresh_versions() -> None:
    scope = "versions"
    lock = _lock(scope)
    async with lock:
        # Re-check staleness inside the lock: a concurrent caller may have
        # already refreshed while we were waiting.
        async with db.connect() as conn:
            if not await storage.is_stale(conn, scope, settings.upstream_ttl_seconds):
                return
        try:
            versions = await redmine.list_versions()
        except Exception as exc:
            log.warning("versions fetch failed: %s", exc)
            return
        async with db.connect() as conn:
            await storage.upsert_versions(conn, versions)
            await storage.mark_fetched(conn, scope)
        log.info("refreshed %d versions", len(versions))


async def ensure_versions_present() -> None:
    """Synchronously fetch versions if the cache is empty (first-run bootstrap)."""
    async with db.connect() as conn:
        cached = await storage.list_versions_cached(conn)
    if not cached:
        await refresh_versions()


# ---- single version --------------------------------------------------------


def _truncate_for_storage(diff: str | None, cap: int) -> tuple[str | None, bool]:
    if not diff:
        return None, False
    lines = diff.splitlines()
    if len(lines) <= cap:
        return diff, False
    return "\n".join(lines[:cap]), True


async def _score_and_save(
    revision: str,
    issue_subject: str,
    issue_description: str,
    commit_message: str,
    diff: str | None,
) -> None:
    result = await risk.score_commit(
        issue_subject=issue_subject,
        issue_description=issue_description,
        commit_message=commit_message,
        diff=diff,
    )
    if not result:
        return
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


async def _fetch_issue_detail(
    sem: asyncio.Semaphore, issue_id: int
) -> dict[str, Any] | None:
    async with sem:
        try:
            return await redmine.get_issue(issue_id)
        except Exception as exc:
            log.warning("issue detail fetch failed for %s: %s", issue_id, exc)
            return None


async def _fetch_diff(sem: asyncio.Semaphore, revision: str) -> str | None:
    async with sem:
        try:
            return await redmine.get_diff(revision)
        except Exception as exc:
            log.warning("diff fetch failed for %s: %s", revision, exc)
            return None


async def refresh_version(version_id: int) -> None:
    scope = f"version:{version_id}"
    lock = _lock(scope)

    async with lock:
        # Re-check staleness inside the lock to dedup concurrent triggers.
        async with db.connect() as conn:
            if not await storage.is_stale(conn, scope, settings.upstream_ttl_seconds):
                return
        try:
            issue_summaries = await redmine.list_issues_for_version(version_id)
        except Exception as exc:
            log.warning("issues fetch failed for version %s: %s", version_id, exc)
            return

        # Redmine ignores `include=changesets` on the list endpoint, so we
        # have to fetch each issue's detail individually for the changesets.
        sem = asyncio.Semaphore(_REDMINE_CONCURRENCY)
        details = await asyncio.gather(
            *(_fetch_issue_detail(sem, s["id"]) for s in issue_summaries)
        )
        issues: list[dict[str, Any]] = []
        for summary, detail in zip(issue_summaries, details):
            issues.append(detail if detail is not None else summary)

        # Figure out which revisions are new for this refresh and fetch their
        # diffs in parallel.
        new_revisions: list[tuple[int, dict[str, Any]]] = []  # (issue_id, changeset)
        async with db.connect() as conn:
            for issue in issues:
                seen = await storage.existing_revisions(conn, issue["id"])
                for cs in issue.get("changesets") or []:
                    revision = str(cs.get("revision"))
                    if revision and revision not in seen:
                        new_revisions.append((issue["id"], cs))

        diffs = await asyncio.gather(
            *(_fetch_diff(sem, str(cs.get("revision"))) for _, cs in new_revisions)
        )

        # Persist everything; decide which commits actually get scored.
        version_name = ""
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT name FROM versions WHERE id = ?", (version_id,)
            )
            row = await cur.fetchone()
            if row:
                version_name = row[0]

        version_allows_scoring = _scoring_allowed_for_version(version_name)
        scoring_jobs: list[tuple[str, str, str, str, str | None]] = []
        skipped_noise = 0
        skipped_disallowed = 0
        skipped_capped = 0
        issue_lookup = {i["id"]: i for i in issues}

        async with db.connect() as conn:
            await storage.upsert_issues(conn, version_id, issues)
            for (issue_id, cs), diff_text in zip(new_revisions, diffs):
                revision = str(cs.get("revision"))
                stored_diff, truncated = _truncate_for_storage(
                    diff_text, settings.diff_line_cap
                )
                committer = (
                    cs.get("user", {}).get("name")
                    if cs.get("user")
                    else cs.get("committer")
                )
                message = cs.get("comments") or ""
                await storage.insert_changeset(
                    conn,
                    revision=revision,
                    issue_id=issue_id,
                    committer=committer,
                    committed_on=cs.get("committed_on"),
                    message=message,
                    diff_text=stored_diff,
                    diff_truncated=truncated,
                )
                # Cost gate: skip merges/tags, off-allowlist versions, and
                # anything past the per-cycle cap.
                if not version_allows_scoring:
                    skipped_disallowed += 1
                    continue
                if _is_noise_commit(message):
                    skipped_noise += 1
                    continue
                if len(scoring_jobs) >= settings.risk_max_per_cycle:
                    skipped_capped += 1
                    continue
                issue = issue_lookup.get(issue_id, {})
                scoring_jobs.append(
                    (
                        revision,
                        issue.get("subject", ""),
                        issue.get("description", "") or "",
                        message,
                        diff_text,
                    )
                )
            await storage.mark_fetched(conn, scope)

        log.info(
            "refreshed version %s (%s): %d issues, %d new commits, %d to score "
            "(skipped: %d noise, %d off-allowlist, %d over per-cycle cap)",
            version_id,
            version_name,
            len(issues),
            len(new_revisions),
            len(scoring_jobs),
            skipped_noise,
            skipped_disallowed,
            skipped_capped,
        )

        for rev, subj, desc, msg, diff in scoring_jobs:
            try:
                await _score_and_save(rev, subj, desc, msg, diff)
            except Exception as exc:
                log.warning("risk scoring failed for %s: %s", rev, exc)


async def maybe_refresh_versions_in_background(scheduler) -> None:
    """If the versions list is stale, schedule a background refresh."""
    async with db.connect() as conn:
        stale = await storage.is_stale(conn, "versions", settings.upstream_ttl_seconds)
    if stale:
        scheduler(refresh_versions())


async def maybe_refresh_version_in_background(version_id: int, scheduler) -> None:
    async with db.connect() as conn:
        stale = await storage.is_stale(
            conn, f"version:{version_id}", settings.upstream_ttl_seconds
        )
    if stale:
        scheduler(refresh_version(version_id))
