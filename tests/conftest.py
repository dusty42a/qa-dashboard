"""Shared fixtures for the offline test harness.

The harness never talks to Redmine or Anthropic. We monkey-patch the high-level
functions in `app.redmine` and `app.risk` so the FastAPI app, refresh logic, and
SQLite storage all run for real with stub data.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Set safe defaults BEFORE importing app modules so pydantic-settings doesn't
# pick up real values from a developer's environment.
os.environ.setdefault("REDMINE_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("RISK_ENABLED", "true")
os.environ.setdefault("UPSTREAM_TTL_SECONDS", "600")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, redmine, refresh, risk  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


# ---- canned data -----------------------------------------------------------

VERSIONS_DATA = [
    {
        "id": 1,
        "name": "Release 7.7",
        "status": "open",
        "due_date": "2026-06-01",
        "description": "Q2 release",
    },
    {
        "id": 2,
        "name": "Release 7.8",
        "status": "open",
        "due_date": None,
        "description": "Q3 release",
    },
]

ISSUES_BY_VERSION: dict[int, list[dict]] = {
    1: [
        {
            "id": 100,
            "subject": "Login fails with special chars",
            "status": {"name": "Resolved"},
            "priority": {"name": "High"},
            "tracker": {"name": "Bug"},
            "assigned_to": {"name": "Alex"},
            "author": {"name": "QA"},
            "updated_on": "2026-05-01T10:00:00Z",
            "description": "Backslashes in passwords break the auth flow.",
            "changesets": [
                {
                    "revision": "12345",
                    "user": {"name": "dev1"},
                    "committed_on": "2026-05-01T09:00:00Z",
                    "comments": "Issue #100 - Escape special chars in password field",
                },
                {
                    "revision": "12346",
                    "user": {"name": "dev1"},
                    "committed_on": "2026-05-01T09:30:00Z",
                    "comments": "Issue #100 - Add regression test",
                },
                {
                    "revision": "12347",
                    "user": {"name": "dev1"},
                    "committed_on": "2026-05-01T10:00:00Z",
                    "comments": "Issue #100 - Merge From Trunk",
                },
                {
                    "revision": "12348",
                    "user": {"name": "dev1"},
                    "committed_on": "2026-05-01T10:30:00Z",
                    "comments": "Issue #100 - Tagging A.77.18 for release",
                },
            ],
        },
        {
            "id": 101,
            "subject": "Tweak report header",
            "status": {"name": "In Progress"},
            "priority": {"name": "Low"},
            "tracker": {"name": "Feature"},
            "assigned_to": {"name": "Sam"},
            "author": {"name": "PM"},
            "updated_on": "2026-05-02T11:00:00Z",
            "description": "Add company logo to monthly report header.",
            "changesets": [],
        },
    ],
    2: [],
}

DIFFS: dict[str, str] = {
    "12345": (
        "--- a/auth/login.php\n+++ b/auth/login.php\n"
        "@@ -10,7 +10,7 @@\n"
        "-    $pw = $_POST['password'];\n"
        "+    $pw = addslashes($_POST['password']);\n"
    ),
    "12346": (
        "--- a/tests/auth_test.php\n+++ b/tests/auth_test.php\n"
        "@@ -0,0 +1,5 @@\n"
        "+function test_login_with_backslash() {\n"
        "+    assert_equals(login('user', 'pa\\\\ss'), true);\n"
        "+}\n"
    ),
    "12347": "[merge from trunk - no diff text]",
    "12348": "[tag - no diff text]",
}


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's SQLite at a fresh temp directory for the test."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture
def initialized_db(tmp_data_dir: Path) -> Path:
    import asyncio

    asyncio.run(db.init_db())
    return tmp_data_dir


@pytest.fixture
def redmine_call_log() -> list[tuple[str, object]]:
    """Records (operation, argument) per call so tests can assert cache behavior."""
    return []


@pytest.fixture
def mock_redmine(
    monkeypatch: pytest.MonkeyPatch, redmine_call_log: list[tuple[str, object]]
) -> None:
    async def fake_list_versions() -> list[dict]:
        redmine_call_log.append(("list_versions", None))
        return [dict(v) for v in VERSIONS_DATA]

    async def fake_list_issues(version_id: int) -> list[dict]:
        redmine_call_log.append(("list_issues_for_version", version_id))
        # The real list endpoint doesn't include changesets — strip them here
        # so tests reflect that the dashboard has to fetch detail per issue.
        import copy
        summaries = []
        for issue in ISSUES_BY_VERSION.get(version_id, []):
            stripped = copy.deepcopy(issue)
            stripped.pop("changesets", None)
            stripped.pop("journals", None)
            summaries.append(stripped)
        return summaries

    async def fake_get_issue(issue_id: int) -> dict:
        redmine_call_log.append(("get_issue", issue_id))
        import copy
        for issues in ISSUES_BY_VERSION.values():
            for issue in issues:
                if issue["id"] == issue_id:
                    return copy.deepcopy(issue)
        raise AssertionError(f"unknown issue {issue_id}")

    async def fake_get_diff(revision: str) -> str | None:
        redmine_call_log.append(("get_diff", revision))
        return DIFFS.get(revision)

    monkeypatch.setattr(redmine, "list_versions", fake_list_versions)
    monkeypatch.setattr(redmine, "list_issues_for_version", fake_list_issues)
    monkeypatch.setattr(redmine, "get_issue", fake_get_issue)
    monkeypatch.setattr(redmine, "get_diff", fake_get_diff)


@pytest.fixture
def risk_call_log() -> list[dict]:
    return []


@pytest.fixture
def mock_risk(
    monkeypatch: pytest.MonkeyPatch, risk_call_log: list[dict]
) -> None:
    async def fake_score(*, issue_subject, issue_description, commit_message, diff):
        risk_call_log.append(
            {
                "issue_subject": issue_subject,
                "commit_message": commit_message,
                "diff_present": diff is not None,
            }
        )
        # Crude heuristic so tests can distinguish levels.
        text = f"{issue_subject} {commit_message}".lower()
        if "auth" in text or "login" in text or "password" in text:
            level = "high"
            reasons = ["touches authentication path"]
            areas = ["auth"]
        elif "test" in text:
            level = "low"
            reasons = ["test-only change"]
            areas = ["tests"]
        else:
            level = "med"
            reasons = ["unclassified change"]
            areas = ["misc"]
        return risk.RiskResult(
            level=level,
            reasons=reasons,
            affected_areas=areas,
            raw_response='{"level":"%s"}' % level,
        )

    monkeypatch.setattr(risk, "score_commit", fake_score)


@pytest.fixture
def reset_refresh_locks() -> Iterator[None]:
    """Refresh dedup locks live at module scope; clear them between tests."""
    refresh._locks.clear()
    yield
    refresh._locks.clear()


@pytest.fixture
def client(
    initialized_db: Path,
    mock_redmine: None,
    mock_risk: None,
    reset_refresh_locks: None,
) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
