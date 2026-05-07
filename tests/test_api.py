"""End-to-end API tests with mocked Redmine + Anthropic.

These exercise the full request path: route handler → refresh → SQLite →
storage queries → JSON response. Only the outbound network calls are stubbed.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from app.config import settings


# Encoded forms for the canned version names that include a space.
V77 = quote("Release 7.7")
V78 = quote("Release 7.8")


def test_landing_page_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<title>QA Dashboard</title>" in resp.text


def test_version_page_serves_html(client):
    # The HTML page itself loads regardless of whether the version exists; the
    # JS calls the API and shows the not-found state when applicable.
    resp = client.get(f"/v/{V77}")
    assert resp.status_code == 200
    assert "Target version" in resp.text or "version" in resp.text.lower()


def test_static_assets_served(client):
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    assert "risk" in resp.text  # we define .risk-* classes


def test_api_versions_returns_mocked_list(client):
    resp = client.get("/api/versions")
    assert resp.status_code == 200
    body = resp.json()
    names = {v["name"] for v in body["versions"]}
    assert names == {"Release 7.7", "Release 7.8"}
    assert body["last_fetched_at"] is not None


def test_api_version_unknown_returns_404(client):
    resp = client.get("/api/v/9.9")
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["error"] == "version_not_found"
    assert body["detail"]["name"] == "9.9"


def test_api_version_returns_issues_with_risk(client, risk_call_log):
    resp = client.get(f"/api/v/{V77}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["version"]["name"] == "Release 7.7"
    assert body["last_fetched_at"] is not None
    assert len(body["issues"]) == 2

    login_issue = next(i for i in body["issues"] if i["id"] == 100)
    assert login_issue["subject"].startswith("Login fails")

    # All four changesets should be persisted, even the merge and tag ones.
    revisions = {c["revision"] for c in login_issue["changesets"]}
    assert revisions == {"12345", "12346", "12347", "12348"}

    # Only the two non-noise commits should have been scored.
    scored_revisions = {
        c["revision"] for c in login_issue["changesets"] if c["risk"] is not None
    }
    assert scored_revisions == {"12345", "12346"}

    cs_12345 = next(c for c in login_issue["changesets"] if c["revision"] == "12345")
    assert cs_12345["risk"]["level"] == "high"
    assert "auth" in cs_12345["risk"]["affected_areas"]

    feature_issue = next(i for i in body["issues"] if i["id"] == 101)
    assert feature_issue["changesets"] == []

    # The scorer was called once per non-noise changeset.
    assert len(risk_call_log) == 2


def test_noise_commits_are_persisted_but_not_scored(client, risk_call_log):
    resp = client.get(f"/api/v/{V77}")
    assert resp.status_code == 200
    body = resp.json()

    login_issue = next(i for i in body["issues"] if i["id"] == 100)
    cs_by_rev = {c["revision"]: c for c in login_issue["changesets"]}

    # Merge and tag commits are stored so they show up in the dashboard...
    assert "12347" in cs_by_rev
    assert "12348" in cs_by_rev
    # ...but no risk row was generated for them.
    assert cs_by_rev["12347"]["risk"] is None
    assert cs_by_rev["12348"]["risk"] is None

    # And they never reached the scorer at all.
    scored_messages = [c["commit_message"] for c in risk_call_log]
    assert all("Merge From Trunk" not in m for m in scored_messages)
    assert all("Tagging" not in m for m in scored_messages)


def test_risk_allowlist_blocks_off_list_versions(
    client, risk_call_log, monkeypatch: pytest.MonkeyPatch
):
    # Restrict scoring to a version other than 7.7. Commits should still be
    # stored, but no Anthropic calls should be issued.
    monkeypatch.setattr(settings, "risk_versions", "Release 7.8")

    resp = client.get(f"/api/v/{V77}")
    assert resp.status_code == 200
    body = resp.json()

    login_issue = next(i for i in body["issues"] if i["id"] == 100)
    assert len(login_issue["changesets"]) == 4
    assert all(c["risk"] is None for c in login_issue["changesets"])
    assert risk_call_log == []


def test_risk_max_per_cycle_caps_scoring(
    client, risk_call_log, monkeypatch: pytest.MonkeyPatch
):
    # Cap to 1 scored commit per refresh; the second non-noise commit should
    # be persisted unscored.
    monkeypatch.setattr(settings, "risk_max_per_cycle", 1)

    resp = client.get(f"/api/v/{V77}")
    assert resp.status_code == 200
    body = resp.json()

    login_issue = next(i for i in body["issues"] if i["id"] == 100)
    scored = [c for c in login_issue["changesets"] if c["risk"] is not None]
    assert len(scored) == 1
    assert len(risk_call_log) == 1


def test_api_version_second_hit_serves_from_cache(client, redmine_call_log):
    """Within the TTL, a second request must not trigger any upstream calls."""
    resp1 = client.get(f"/api/v/{V77}")
    assert resp1.status_code == 200

    calls_after_first = list(redmine_call_log)
    operations = {op for op, _ in calls_after_first}
    assert "list_versions" in operations
    assert "list_issues_for_version" in operations
    assert "get_issue" in operations
    assert "get_diff" in operations

    resp2 = client.get(f"/api/v/{V77}")
    assert resp2.status_code == 200

    assert redmine_call_log == calls_after_first


def test_api_version_does_not_rescore_known_changesets(client, risk_call_log):
    """Risk scoring should be once-per-changeset, not once-per-page-load."""
    client.get(f"/api/v/{V77}")
    calls_after_first = len(risk_call_log)
    assert calls_after_first == 2  # 2 non-noise of 4 total changesets

    client.get(f"/api/v/{V77}")
    assert len(risk_call_log) == calls_after_first


def test_empty_version_returns_no_issues(client):
    resp = client.get(f"/api/v/{V78}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]["name"] == "Release 7.8"
    assert body["issues"] == []
