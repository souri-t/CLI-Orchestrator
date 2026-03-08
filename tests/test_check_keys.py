"""_check_api_keys() のユニットテスト。"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.main import _check_api_keys


def _mock_response(status_code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    return r


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_key_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", return_value=_mock_response(200)):
        results = _check_api_keys()

    anthropic = next(r for r in results if r.provider == "Anthropic")
    assert anthropic.ok is True
    assert anthropic.message == "OK"


def test_anthropic_key_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", return_value=_mock_response(401)):
        results = _check_api_keys()

    anthropic = next(r for r in results if r.provider == "Anthropic")
    assert anthropic.ok is False
    assert "認証失敗" in anthropic.message


def test_anthropic_key_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    results = _check_api_keys()

    anthropic = next(r for r in results if r.provider == "Anthropic")
    assert anthropic.ok is False
    assert anthropic.message == "未設定"


def test_anthropic_fallback_to_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTHROPIC_API_KEY が未設定でも ANTHROPIC_AUTH_TOKEN が使われることを確認。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", return_value=_mock_response(200)):
        results = _check_api_keys()

    anthropic = next(r for r in results if r.provider == "Anthropic")
    assert anthropic.ok is True


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def test_openai_key_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", return_value=_mock_response(200)):
        results = _check_api_keys()

    openai = next(r for r in results if r.provider == "OpenAI")
    assert openai.ok is True


def test_openai_key_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "bad-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", return_value=_mock_response(401)):
        results = _check_api_keys()

    openai = next(r for r in results if r.provider == "OpenAI")
    assert openai.ok is False
    assert "認証失敗" in openai.message


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


def test_google_key_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", return_value=_mock_response(200)):
        results = _check_api_keys()

    google = next(r for r in results if r.provider == "Google")
    assert google.ok is True


def test_google_key_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "bad-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", return_value=_mock_response(403)):
        results = _check_api_keys()

    google = next(r for r in results if r.provider == "Google")
    assert google.ok is False
    assert "認証失敗" in google.message


# ---------------------------------------------------------------------------
# 接続エラー
# ---------------------------------------------------------------------------


def test_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("httpx.get", side_effect=Exception("timeout")):
        results = _check_api_keys()

    anthropic = next(r for r in results if r.provider == "Anthropic")
    assert anthropic.ok is False
    assert "接続エラー" in anthropic.message


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


def test_openrouter_key_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    with patch("httpx.get", return_value=_mock_response(200)):
        results = _check_api_keys()

    openrouter = next(r for r in results if r.provider == "OpenRouter")
    assert openrouter.ok is True


def test_openrouter_key_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "bad-key")

    with patch("httpx.get", return_value=_mock_response(401)):
        results = _check_api_keys()

    openrouter = next(r for r in results if r.provider == "OpenRouter")
    assert openrouter.ok is False
    assert "認証失敗" in openrouter.message


# ---------------------------------------------------------------------------
# check-keys CLI コマンド
# ---------------------------------------------------------------------------


def test_check_keys_command_success() -> None:
    from click.testing import CliRunner

    from orchestrator.main import _KeyResult, cli

    runner = CliRunner()
    with patch("orchestrator.main._check_api_keys") as mock_check:
        mock_check.return_value = [
            _KeyResult("Anthropic", "ANTHROPIC_API_KEY", True, "OK"),
            _KeyResult("OpenAI", "OPENAI_API_KEY", False, "未設定"),
            _KeyResult("Google", "GOOGLE_API_KEY", False, "未設定"),
        ]
        result = runner.invoke(cli, ["check-keys"])

    assert result.exit_code == 0
    assert "Anthropic" in result.output
    assert "OK" in result.output


def test_check_keys_command_all_fail() -> None:
    from click.testing import CliRunner

    from orchestrator.main import _KeyResult, cli

    runner = CliRunner()
    with patch("orchestrator.main._check_api_keys") as mock_check:
        mock_check.return_value = [
            _KeyResult("Anthropic", "ANTHROPIC_API_KEY", False, "未設定"),
            _KeyResult("OpenAI", "OPENAI_API_KEY", False, "未設定"),
            _KeyResult("Google", "GOOGLE_API_KEY", False, "未設定"),
        ]
        result = runner.invoke(cli, ["check-keys"])

    assert result.exit_code == 1
    assert "有効な API キーがありません" in result.output
