"""Read/write helpers around the SQLite cache.

Kept separate from `api.py` so route handlers stay thin and the upsert SQL is
easy to find.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- versions --------------------------------------------------------------


async def upsert_versions(conn: aiosqlite.Connection, versions: list[dict[str, Any]]) -> None:
    now = _now()
    for v in versions:
        await conn.execute(
            """
            INSERT INTO versions (id, name, status, description, due_date, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                status=excluded.status,
                description=excluded.description,
                due_date=excluded.due_date,
                raw_json=excluded.raw_json,
                fetched_at=excluded.fetched_at
            """,
            (
                v["id"],
                v["name"],
                v.get("status"),
                v.get("description"),
                v.get("due_date"),
                json.dumps(v),
                now,
            ),
        )
    await conn.commit()


async def list_versions_cached(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT v.id, v.name, v.status, v.description, v.due_date,
               COUNT(DISTINCT i.id)   AS issue_count,
               COUNT(DISTINCT c.revision) AS total_changesets,
               COUNT(DISTINCT ra.revision) AS scored_changesets
        FROM versions v
        LEFT JOIN issues i  ON i.version_id = v.id
        LEFT JOIN changesets c ON c.issue_id = i.id
        LEFT JOIN risk_assessments ra ON ra.revision = c.revision
        GROUP BY v.id
        ORDER BY v.name
        """
    )
    rows = await cur.fetchall()
    return [
        {"id": r[0], "name": r[1], "status": r[2], "description": r[3],
         "due_date": r[4], "issue_count": r[5],
         "total_changesets": r[6], "scored_changesets": r[7]}
        for r in rows
    ]


async def find_version_by_name(
    conn: aiosqlite.Connection, name: str
) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, name, status, description, due_date FROM versions WHERE name = ?",
        (name,),
    )
    r = await cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "status": r[2], "description": r[3], "due_date": r[4]}


# ---- issues ----------------------------------------------------------------


async def upsert_issues(
    conn: aiosqlite.Connection, version_id: int, issues: list[dict[str, Any]]
) -> None:
    now = _now()
    # Replace the version's issue set wholesale so closed/moved issues drop out.
    await conn.execute("DELETE FROM issues WHERE version_id = ?", (version_id,))
    for it in issues:
        await conn.execute(
            """
            INSERT INTO issues (id, version_id, subject, status, priority, tracker,
                                assigned_to, author, updated_on, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                it["id"],
                version_id,
                it.get("subject", ""),
                (it.get("status") or {}).get("name"),
                (it.get("priority") or {}).get("name"),
                (it.get("tracker") or {}).get("name"),
                (it.get("assigned_to") or {}).get("name"),
                (it.get("author") or {}).get("name"),
                it.get("updated_on"),
                json.dumps(it),
                now,
            ),
        )
    await conn.commit()


# ---- changesets ------------------------------------------------------------


async def existing_revisions(conn: aiosqlite.Connection, issue_id: int) -> set[str]:
    cur = await conn.execute(
        "SELECT revision FROM changesets WHERE issue_id = ?", (issue_id,)
    )
    return {r[0] for r in await cur.fetchall()}


async def insert_changeset(
    conn: aiosqlite.Connection,
    *,
    revision: str,
    issue_id: int,
    committer: str | None,
    committed_on: str | None,
    message: str | None,
    diff_text: str | None,
    diff_truncated: bool,
) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO changesets
            (revision, issue_id, committer, committed_on, message, diff_text,
             diff_truncated, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision,
            issue_id,
            committer,
            committed_on,
            message,
            diff_text,
            1 if diff_truncated else 0,
            _now(),
        ),
    )
    await conn.commit()


async def changesets_for_issue(
    conn: aiosqlite.Connection, issue_id: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT c.revision, c.committer, c.committed_on, c.message, c.diff_truncated,
               r.level, r.reasons_json, r.affected_json, r.model, r.scored_at
        FROM changesets c
        LEFT JOIN risk_assessments r ON r.revision = c.revision
        WHERE c.issue_id = ?
        ORDER BY c.committed_on DESC
        """,
        (issue_id,),
    )
    out: list[dict[str, Any]] = []
    for row in await cur.fetchall():
        risk = None
        if row[5] is not None:
            risk = {
                "level": row[5],
                "reasons": json.loads(row[6]) if row[6] else [],
                "affected_areas": json.loads(row[7]) if row[7] else [],
                "model": row[8],
                "scored_at": row[9],
            }
        out.append(
            {
                "revision": row[0],
                "committer": row[1],
                "committed_on": row[2],
                "message": row[3],
                "diff_truncated": bool(row[4]),
                "risk": risk,
            }
        )
    return out


# ---- risk ------------------------------------------------------------------


async def save_risk(
    conn: aiosqlite.Connection,
    *,
    revision: str,
    model: str,
    level: str,
    reasons: list[str],
    affected_areas: list[str],
    raw_response: str,
) -> None:
    await conn.execute(
        """
        INSERT OR REPLACE INTO risk_assessments
            (revision, model, level, reasons_json, affected_json, raw_response, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision,
            model,
            level,
            json.dumps(reasons),
            json.dumps(affected_areas),
            raw_response,
            _now(),
        ),
    )
    await conn.commit()


# ---- fetch_log -------------------------------------------------------------


async def is_stale(conn: aiosqlite.Connection, scope: str, ttl_seconds: int) -> bool:
    cur = await conn.execute(
        "SELECT last_fetched_at FROM fetch_log WHERE scope = ?", (scope,)
    )
    row = await cur.fetchone()
    if not row:
        return True
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age >= ttl_seconds


async def mark_fetched(conn: aiosqlite.Connection, scope: str) -> None:
    await conn.execute(
        """
        INSERT INTO fetch_log (scope, last_fetched_at) VALUES (?, ?)
        ON CONFLICT(scope) DO UPDATE SET last_fetched_at = excluded.last_fetched_at
        """,
        (scope, _now()),
    )
    await conn.commit()


async def last_fetched_at(conn: aiosqlite.Connection, scope: str) -> str | None:
    cur = await conn.execute(
        "SELECT last_fetched_at FROM fetch_log WHERE scope = ?", (scope,)
    )
    row = await cur.fetchone()
    return row[0] if row else None


# ---- confidence areas -------------------------------------------------------

_WEIGHTS = {"high": 5.0, "med": 2.0, "low": 0.2}

# Maps lowercase raw values → canonical display name.
_AREA_ALIASES: dict[str, str] = {
    # Authentication / session
    "auth": "Authentication",
    "authentication": "Authentication",
    "session management": "Authentication",
    # CSS / theming
    "css": "CSS / Theming",
    "css variables": "CSS / Theming",
    "ui/css": "CSS / Theming",
    "ui-styling": "CSS / Theming",
    "ui styling": "CSS / Theming",
    "theme styling": "CSS / Theming",
    "theming": "CSS / Theming",
    "theme-wb-green-2024": "CSS / Theming",
    "wb-theme-wb-green-2024/colour-profiles": "CSS / Theming",
    # Test infrastructure / CI
    "testing": "Test Infrastructure",
    "test-infrastructure": "Test Infrastructure",
    "test infrastructure": "Test Infrastructure",
    "testing infrastructure": "Test Infrastructure",
    "testcafe": "Test Infrastructure",
    "browserstack": "Test Infrastructure",
    "browserstack-integration": "Test Infrastructure",
    "ci": "CI / Build",
    "ci/cd": "CI / Build",
    "ci/cd build process": "CI / Build",
    "ci/build": "CI / Build",
    # Notifications / email
    "email": "Email",
    "email notifications": "Email Notifications",
    "notifications": "Notifications",
    # UI / components
    "ui": "UI",
    "ui components": "UI Components",
    # Database / persistence
    "database": "Database",
    "migrations": "Database Migrations",
    # API
    "api": "API",
}

import re as _re
_FILE_EXT_RE = _re.compile(r'\.[a-z]{2,5}$', _re.IGNORECASE)
_PATH_PREFIXES = (
    "trunk/", "branches/", "workbooks_app/", "testing/",
    "stylesheets/", "svn:", "svn+ssh",
)


def _friendly_area(raw: str) -> str | None:
    """Return a human-friendly display name, or None if raw looks like a file path."""
    area = raw.strip()
    if not area:
        return None
    # Filter file paths: anything with slashes that has a file extension,
    # multiple path segments, or a known path prefix.
    if "/" in area:
        last_seg = area.split("/")[-1]
        low = area.lower()
        if (
            _FILE_EXT_RE.search(last_seg)
            or area.count("/") >= 2
            or any(low.startswith(p) for p in _PATH_PREFIXES)
        ):
            return None
    # Filter plain filenames (e.g. wb_test.rake, comments.css, jenkins.sh)
    if _FILE_EXT_RE.search(area) and " " not in area:
        return None
    # Alias lookup (case-insensitive)
    key = area.lower()
    if key in _AREA_ALIASES:
        return _AREA_ALIASES[key]
    # Title-case slugs/lowercase strings; leave mixed-case as-is
    if area == area.lower() or area.replace("-", "").replace("_", "") == area.replace("-", "").replace("_", "").lower():
        return area.replace("-", " ").replace("_", " ").title()
    return area


async def build_confidence_areas(
    conn: aiosqlite.Connection,
    version_id: int,
    *,
    min_score: float = 3.0,
    limit: int = 12,
) -> dict[str, Any]:
    commit_cur = await conn.execute(
        """
        SELECT COUNT(DISTINCT c.revision), COUNT(DISTINCT ra.revision)
        FROM changesets c
        JOIN issues i ON i.id = c.issue_id
        LEFT JOIN risk_assessments ra ON ra.revision = c.revision
        WHERE i.version_id = ?
        """,
        (version_id,),
    )
    counts = await commit_cur.fetchone()
    total_commits = counts[0] if counts else 0
    scored_commits = counts[1] if counts else 0

    area_cur = await conn.execute(
        """
        SELECT ra.affected_json, ra.level
        FROM risk_assessments ra
        JOIN changesets c ON c.revision = ra.revision
        JOIN issues i ON i.id = c.issue_id
        WHERE i.version_id = ?
        """,
        (version_id,),
    )
    rows = await area_cur.fetchall()

    stats: dict[str, dict[str, Any]] = {}
    for affected_json, level in rows:
        if not affected_json:
            continue
        try:
            affected = json.loads(affected_json)
        except (ValueError, TypeError):
            continue
        w = _WEIGHTS.get(level, 0)
        for raw in affected:
            friendly = _friendly_area(raw if isinstance(raw, str) else "")
            if not friendly:
                continue
            key = friendly.lower()
            if key not in stats:
                stats[key] = {"name": friendly, "high": 0, "med": 0, "low": 0, "score": 0.0}
            stats[key][level] = stats[key].get(level, 0) + 1
            stats[key]["score"] += w

    ranked = [
        v for v in stats.values()
        if v["score"] >= min_score and (v["high"] > 0 or v["med"] > 0)
    ]
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return {
        "areas": [{"name": r["name"], "high": r["high"], "med": r["med"], "low": r["low"]}
                  for r in ranked[:limit]],
        "scored_commits": scored_commits,
        "total_commits": total_commits,
    }


# ---- aggregate payload ------------------------------------------------------


async def build_version_payload(
    conn: aiosqlite.Connection, version: dict[str, Any]
) -> dict[str, Any]:
    cur = await conn.execute(
        """
        SELECT id, subject, status, priority, tracker, assigned_to, author, updated_on
        FROM issues
        WHERE version_id = ?
        ORDER BY priority DESC, updated_on DESC
        """,
        (version["id"],),
    )
    issues = []
    for row in await cur.fetchall():
        issue_id = row[0]
        changesets = await changesets_for_issue(conn, issue_id)
        issues.append(
            {
                "id": issue_id,
                "subject": row[1],
                "status": row[2],
                "priority": row[3],
                "tracker": row[4],
                "assigned_to": row[5],
                "author": row[6],
                "updated_on": row[7],
                "changesets": changesets,
            }
        )
    return {
        "version": version,
        "issues": issues,
        "confidence_areas": await build_confidence_areas(conn, version["id"]),
        "last_fetched_at": await last_fetched_at(conn, f"version:{version['id']}"),
    }
