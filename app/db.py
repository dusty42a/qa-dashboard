from __future__ import annotations

import aiosqlite

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    status        TEXT,
    description   TEXT,
    due_date      TEXT,
    raw_json      TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id            INTEGER PRIMARY KEY,
    version_id    INTEGER NOT NULL,
    subject       TEXT    NOT NULL,
    status        TEXT,
    priority      TEXT,
    tracker       TEXT,
    assigned_to   TEXT,
    author        TEXT,
    updated_on    TEXT,
    raw_json      TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL,
    FOREIGN KEY (version_id) REFERENCES versions(id)
);
CREATE INDEX IF NOT EXISTS issues_version_idx ON issues(version_id);

CREATE TABLE IF NOT EXISTS changesets (
    revision      TEXT    PRIMARY KEY,
    issue_id      INTEGER NOT NULL,
    committer     TEXT,
    committed_on  TEXT,
    message       TEXT,
    diff_text     TEXT,
    diff_truncated INTEGER NOT NULL DEFAULT 0,
    fetched_at    TEXT    NOT NULL,
    FOREIGN KEY (issue_id) REFERENCES issues(id)
);
CREATE INDEX IF NOT EXISTS changesets_issue_idx ON changesets(issue_id);

CREATE TABLE IF NOT EXISTS risk_assessments (
    revision      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    level         TEXT    NOT NULL,
    reasons_json  TEXT    NOT NULL,
    affected_json TEXT    NOT NULL,
    raw_response  TEXT,
    scored_at     TEXT    NOT NULL,
    PRIMARY KEY (revision, model),
    FOREIGN KEY (revision) REFERENCES changesets(revision)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    scope         TEXT    PRIMARY KEY,
    last_fetched_at TEXT  NOT NULL
);
"""


async def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def connect() -> aiosqlite.Connection:
    return aiosqlite.connect(settings.db_path)
