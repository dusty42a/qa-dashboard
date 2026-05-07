"""Round-trip tests for the SQLite storage layer."""

from __future__ import annotations

from app import db, storage


async def test_versions_roundtrip(initialized_db):
    versions = [
        {"id": 1, "name": "7.7", "status": "open", "due_date": "2026-06-01", "description": "x"},
        {"id": 2, "name": "7.8", "status": "open", "due_date": None, "description": "y"},
    ]
    async with db.connect() as conn:
        await storage.upsert_versions(conn, versions)
        cached = await storage.list_versions_cached(conn)

    assert len(cached) == 2
    names = {v["name"] for v in cached}
    assert names == {"7.7", "7.8"}


async def test_find_version_by_name(initialized_db):
    versions = [
        {"id": 1, "name": "7.7", "status": "open", "due_date": None, "description": ""},
    ]
    async with db.connect() as conn:
        await storage.upsert_versions(conn, versions)
        found = await storage.find_version_by_name(conn, "7.7")
        missing = await storage.find_version_by_name(conn, "does-not-exist")

    assert found is not None
    assert found["id"] == 1
    assert missing is None


async def test_upsert_issues_replaces_set(initialized_db):
    """upsert_issues should drop issues no longer attached to the version."""
    async with db.connect() as conn:
        await storage.upsert_versions(
            conn,
            [{"id": 1, "name": "7.7", "status": "open", "due_date": None, "description": ""}],
        )
        # First batch: two issues.
        await storage.upsert_issues(
            conn,
            1,
            [
                {"id": 100, "subject": "A", "status": {"name": "Open"}, "priority": {}, "tracker": {}, "assigned_to": {}, "author": {}, "updated_on": None},
                {"id": 101, "subject": "B", "status": {"name": "Open"}, "priority": {}, "tracker": {}, "assigned_to": {}, "author": {}, "updated_on": None},
            ],
        )
        # Second batch: only one issue. The other should be evicted.
        await storage.upsert_issues(
            conn,
            1,
            [
                {"id": 100, "subject": "A renamed", "status": {"name": "Open"}, "priority": {}, "tracker": {}, "assigned_to": {}, "author": {}, "updated_on": None},
            ],
        )

        cur = await conn.execute("SELECT id, subject FROM issues WHERE version_id = 1")
        rows = await cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == 100
    assert rows[0][1] == "A renamed"


async def test_changeset_and_risk_roundtrip(initialized_db):
    async with db.connect() as conn:
        await storage.upsert_versions(
            conn,
            [{"id": 1, "name": "7.7", "status": "open", "due_date": None, "description": ""}],
        )
        await storage.upsert_issues(
            conn,
            1,
            [
                {"id": 100, "subject": "A", "status": {"name": "Open"}, "priority": {}, "tracker": {}, "assigned_to": {}, "author": {}, "updated_on": None},
            ],
        )
        await storage.insert_changeset(
            conn,
            revision="r1",
            issue_id=100,
            committer="dev",
            committed_on="2026-05-01T09:00:00Z",
            message="fix",
            diff_text="--- a\n+++ b\n",
            diff_truncated=False,
        )
        await storage.save_risk(
            conn,
            revision="r1",
            model="claude-haiku-4-5-20251001",
            level="high",
            reasons=["touches auth"],
            affected_areas=["auth"],
            raw_response='{"level":"high"}',
        )

        rows = await storage.changesets_for_issue(conn, 100)

    assert len(rows) == 1
    cs = rows[0]
    assert cs["revision"] == "r1"
    assert cs["risk"] is not None
    assert cs["risk"]["level"] == "high"
    assert cs["risk"]["reasons"] == ["touches auth"]
    assert cs["risk"]["affected_areas"] == ["auth"]


async def test_existing_revisions(initialized_db):
    async with db.connect() as conn:
        await storage.upsert_versions(
            conn,
            [{"id": 1, "name": "7.7", "status": "open", "due_date": None, "description": ""}],
        )
        await storage.upsert_issues(
            conn,
            1,
            [
                {"id": 100, "subject": "A", "status": {"name": "Open"}, "priority": {}, "tracker": {}, "assigned_to": {}, "author": {}, "updated_on": None},
            ],
        )
        for r in ("r1", "r2", "r3"):
            await storage.insert_changeset(
                conn,
                revision=r,
                issue_id=100,
                committer="dev",
                committed_on=None,
                message="m",
                diff_text=None,
                diff_truncated=False,
            )

        seen = await storage.existing_revisions(conn, 100)

    assert seen == {"r1", "r2", "r3"}


async def test_fetch_log_staleness(initialized_db):
    async with db.connect() as conn:
        # Never fetched -> stale.
        assert await storage.is_stale(conn, "scope-x", ttl_seconds=600) is True

        await storage.mark_fetched(conn, "scope-x")
        # Just fetched -> fresh.
        assert await storage.is_stale(conn, "scope-x", ttl_seconds=600) is False
        # ttl=0 -> always stale.
        assert await storage.is_stale(conn, "scope-x", ttl_seconds=0) is True


async def test_build_version_payload(initialized_db):
    async with db.connect() as conn:
        await storage.upsert_versions(
            conn,
            [{"id": 1, "name": "7.7", "status": "open", "due_date": None, "description": ""}],
        )
        await storage.upsert_issues(
            conn,
            1,
            [
                {"id": 100, "subject": "A", "status": {"name": "Open"}, "priority": {"name": "High"}, "tracker": {"name": "Bug"}, "assigned_to": {"name": "X"}, "author": {"name": "Y"}, "updated_on": "2026-05-01"},
            ],
        )
        await storage.insert_changeset(
            conn,
            revision="r1",
            issue_id=100,
            committer="dev",
            committed_on="2026-05-01T09:00:00Z",
            message="m",
            diff_text=None,
            diff_truncated=False,
        )
        version = await storage.find_version_by_name(conn, "7.7")
        payload = await storage.build_version_payload(conn, version)

    assert payload["version"]["name"] == "7.7"
    assert len(payload["issues"]) == 1
    assert payload["issues"][0]["id"] == 100
    assert payload["issues"][0]["priority"] == "High"
    assert len(payload["issues"][0]["changesets"]) == 1
    assert payload["issues"][0]["changesets"][0]["revision"] == "r1"
    # No risk yet.
    assert payload["issues"][0]["changesets"][0]["risk"] is None
