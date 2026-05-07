from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class RedmineError(RuntimeError):
    pass


def _client() -> httpx.AsyncClient:
    if not settings.redmine_key:
        raise RedmineError("REDMINE_KEY is not set; configure .env before fetching from Redmine.")
    return httpx.AsyncClient(
        base_url=settings.redmine_base,
        headers={"X-Redmine-API-Key": settings.redmine_key},
        verify=settings.redmine_verify_ssl,
        timeout=httpx.Timeout(30.0),
    )


async def list_versions() -> list[dict[str, Any]]:
    async with _client() as c:
        r = await c.get(f"/projects/{settings.redmine_project}/versions.json")
    if r.status_code != 200:
        raise RedmineError(f"versions: HTTP {r.status_code}")
    return r.json().get("versions", [])


async def list_issues_for_version(version_id: int) -> list[dict[str, Any]]:
    """List issues for a target version. Note: changesets/journals are NOT
    returned by the list endpoint even with `include=...` — Redmine only
    honors that on the single-issue endpoint. Use get_issue() for those.
    """
    issues: list[dict[str, Any]] = []
    offset = 0
    page = 100
    async with _client() as c:
        while True:
            r = await c.get(
                "/issues.json",
                params={
                    "fixed_version_id": version_id,
                    "status_id": "*",
                    "limit": page,
                    "offset": offset,
                },
            )
            if r.status_code != 200:
                raise RedmineError(f"issues: HTTP {r.status_code}")
            body = r.json()
            chunk = body.get("issues", [])
            issues.extend(chunk)
            total = body.get("total_count", len(issues))
            offset += len(chunk)
            if not chunk or offset >= total:
                break
    return issues


async def get_issue(issue_id: int) -> dict[str, Any]:
    """Fetch a single issue with journals and changesets populated."""
    async with _client() as c:
        r = await c.get(
            f"/issues/{issue_id}.json",
            params={"include": "journals,changesets"},
        )
    if r.status_code != 200:
        raise RedmineError(f"issue {issue_id}: HTTP {r.status_code}")
    return r.json().get("issue", {})


async def _svn_diff(revision: str) -> str | None:
    """Fetch diff via direct SVN call. Returns None if SVN is not configured or fails."""
    if not settings.svn_binary or not settings.svn_repo_url:
        return None
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [settings.svn_binary, "diff", "--change", revision, settings.svn_repo_url,
             "--no-auth-cache", "--non-interactive"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning("svn diff %s failed: %s", revision, result.stderr.strip())
            return None
        return result.stdout or None
    except Exception as exc:
        log.warning("svn diff %s error: %s", revision, exc)
        return None


async def get_diff(revision: str) -> str | None:
    """Fetch unified diff for a revision. Tries Redmine proxy first, falls back to direct SVN."""
    async with _client() as c:
        r = await c.get(
            f"/projects/{settings.redmine_project}/repository/revisions/{revision}/diff.diff"
        )
    if r.status_code == 404:
        return await _svn_diff(revision)
    if r.status_code != 200:
        raise RedmineError(f"diff {revision}: HTTP {r.status_code}")
    return r.text
