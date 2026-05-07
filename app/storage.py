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
        "SELECT id, name, status, description, due_date FROM versions ORDER BY name"
    )
    rows = await cur.fetchall()
    return [
        {"id": r[0], "name": r[1], "status": r[2], "description": r[3], "due_date": r[4]}
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
        "last_fetched_at": await last_fetched_at(conn, f"version:{version['id']}"),
    }
