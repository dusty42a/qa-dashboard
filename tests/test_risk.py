"""Tests for the Haiku risk scorer.

We never make a real Anthropic call. The happy-path test injects a fake
`AsyncAnthropic` class into the risk module and verifies that score_commit
parses its tool_use response into a RiskResult.
"""

from __future__ import annotations

import pytest

from app import risk
from app.config import settings


# ---- pure helpers -----------------------------------------------------------


def test_truncate_diff_below_cap_passes_through():
    diff = "\n".join(f"line {i}" for i in range(50))
    out, truncated = risk._truncate_diff(diff, cap=100)
    assert out == diff
    assert truncated is False


def test_truncate_diff_above_cap_truncates():
    diff = "\n".join(f"line {i}" for i in range(500))
    out, truncated = risk._truncate_diff(diff, cap=100)
    assert truncated is True
    assert "[diff truncated: showed 100 of 500 lines]" in out
    # Should keep the first 100 lines.
    assert "line 0" in out
    assert "line 99" in out
    assert "line 100" not in out


def test_truncate_diff_handles_none():
    out, truncated = risk._truncate_diff(None, cap=100)
    assert out == "[no diff available]"
    assert truncated is False


def test_build_user_prompt_includes_all_fields():
    body, truncated = risk.build_user_prompt(
        issue_subject="Login bug",
        issue_description="Users with backslashes in passwords can't log in.",
        commit_message="Escape special chars",
        diff="--- a\n+++ b\n@@ ...",
        diff_cap=100,
    )
    assert "Login bug" in body
    assert "backslashes" in body
    assert "Escape special chars" in body
    assert "--- a" in body
    assert truncated is False


def test_build_user_prompt_handles_missing_fields():
    body, _ = risk.build_user_prompt(
        issue_subject="X",
        issue_description="",
        commit_message="",
        diff=None,
        diff_cap=100,
    )
    assert "(empty)" in body
    assert "[no diff available]" in body


# ---- score_commit fallbacks -------------------------------------------------


async def test_score_commit_disabled_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "risk_enabled", False)
    out = await risk.score_commit(
        issue_subject="x", issue_description="x", commit_message="x", diff=None
    )
    assert out is None


async def test_score_commit_missing_key_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "risk_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    out = await risk.score_commit(
        issue_subject="x", issue_description="x", commit_message="x", diff=None
    )
    assert out is None


# ---- score_commit happy path -----------------------------------------------


class _FakeBlock:
    def __init__(self, type_: str, name: str | None = None, input_: dict | None = None):
        self.type = type_
        self.name = name
        self.input = input_


class _FakeMessage:
    def __init__(self, blocks: list[_FakeBlock]):
        self.content = blocks


class _FakeMessages:
    def __init__(self, response: _FakeMessage, calls: list[dict]):
        self._response = response
        self._calls = calls

    async def create(self, **kwargs):
        self._calls.append(kwargs)
        return self._response


class _FakeAsyncAnthropic:
    """Minimal stand-in for anthropic.AsyncAnthropic.

    Test code installs an instance class via monkeypatch; each test gets a
    fresh `calls` list to inspect what was sent.
    """

    next_response: _FakeMessage | None = None
    calls: list[dict] = []

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        self.messages = _FakeMessages(self.next_response, self.calls)


async def test_score_commit_parses_tool_use(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "risk_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    calls: list[dict] = []
    response = _FakeMessage(
        blocks=[
            _FakeBlock(
                type_="tool_use",
                name="record_risk",
                input_={
                    "level": "high",
                    "reasons": ["touches auth", "no test coverage"],
                    "affected_areas": ["auth", "session"],
                },
            )
        ]
    )

    class FakeClient:
        def __init__(self, api_key: str | None = None):
            self.messages = _FakeMessages(response, calls)

    monkeypatch.setattr(risk, "AsyncAnthropic", FakeClient)

    out = await risk.score_commit(
        issue_subject="Login bug",
        issue_description="...",
        commit_message="fix auth",
        diff="--- a\n+++ b\n",
    )
    assert out is not None
    assert out.level == "high"
    assert out.reasons == ["touches auth", "no test coverage"]
    assert out.affected_areas == ["auth", "session"]

    # Verify the call shape: model, tools, tool_choice, cached system prompt.
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == settings.risk_model
    assert call["tool_choice"] == {"type": "tool", "name": "record_risk"}
    assert call["tools"][0]["name"] == "record_risk"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_score_commit_returns_none_when_no_tool_use_block(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "risk_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    calls: list[dict] = []
    # Model returned only a text block (e.g. refused to call the tool).
    response = _FakeMessage(blocks=[_FakeBlock(type_="text")])

    class FakeClient:
        def __init__(self, api_key: str | None = None):
            self.messages = _FakeMessages(response, calls)

    monkeypatch.setattr(risk, "AsyncAnthropic", FakeClient)

    out = await risk.score_commit(
        issue_subject="x", issue_description="x", commit_message="x", diff=None
    )
    assert out is None


async def test_score_commit_swallows_anthropic_errors(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "risk_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    class _Boom:
        def __init__(self, api_key: str | None = None):
            self.messages = self

        async def create(self, **kwargs):
            raise RuntimeError("simulated network failure")

    monkeypatch.setattr(risk, "AsyncAnthropic", _Boom)

    out = await risk.score_commit(
        issue_subject="x", issue_description="x", commit_message="x", diff=None
    )
    # Network errors should not bubble out — return None and let the caller
    # leave the changeset unscored.
    assert out is None
