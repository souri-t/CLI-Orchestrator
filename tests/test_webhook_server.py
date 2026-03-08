"""webhook_server.py のユニットテスト。"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from orchestrator.github.webhook_server import create_app


def _make_signature(body: bytes, secret: str) -> str:
    """テスト用の署名を生成する。"""
    sig = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
    return f"sha256={sig}"


@pytest.fixture
def mock_orchestrator() -> MagicMock:
    mock = MagicMock()
    mock.issue_monitor._labels.trigger = "ai-task"
    return mock


@pytest.fixture
def client(mock_orchestrator: MagicMock) -> TestClient:
    app = create_app(mock_orchestrator, webhook_secret="test-secret")
    return TestClient(app, raise_server_exceptions=True)


ISSUE_PAYLOAD = {
    "action": "labeled",
    "issue": {
        "number": 42,
        "title": "Fix bug",
        "body": "There is a bug",
        "labels": [{"name": "ai-task"}],
        "html_url": "https://github.com/owner/repo/issues/42",
    },
    "repository": {
        "full_name": "owner/repo",
    },
}


class TestHealthCheck:
    def test_health_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestWebhookEndpoint:
    def test_valid_webhook_submits_task(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        """有効な ai-task Webhook が正しくタスクを投入すること。"""
        body = json.dumps(ISSUE_PAYLOAD).encode()
        signature = _make_signature(body, "test-secret")

        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "test-delivery-id",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 202
        mock_orchestrator.submit_task.assert_called_once()

    def test_invalid_signature_returns_403(self, client: TestClient) -> None:
        """署名が無効な場合は 403 を返すこと。"""
        body = json.dumps(ISSUE_PAYLOAD).encode()
        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=invalidsignature",
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403

    def test_missing_signature_returns_403(self, client: TestClient) -> None:
        """署名ヘッダーがない場合は 403 を返すこと。"""
        body = json.dumps(ISSUE_PAYLOAD).encode()
        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403

    def test_non_issues_event_ignored(self, client: TestClient) -> None:
        """issues 以外のイベントは無視されること。"""
        body = b"{}"
        signature = _make_signature(body, "test-secret")
        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "push",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200

    def test_no_trigger_label_ignored(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        """ai-task ラベルがない場合はタスクが投入されないこと。"""
        payload = {
            "action": "labeled",
            "issue": {
                "number": 1,
                "title": "test",
                "body": "",
                "labels": [{"name": "bug"}],  # ai-task なし
                "html_url": "https://github.com/x",
            },
            "repository": {"full_name": "owner/repo"},
        }
        body = json.dumps(payload).encode()
        signature = _make_signature(body, "test-secret")
        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        mock_orchestrator.submit_task.assert_not_called()

    def test_closed_action_ignored(
        self, client: TestClient, mock_orchestrator: MagicMock
    ) -> None:
        """closed アクションは無視されること。"""
        payload = {**ISSUE_PAYLOAD, "action": "closed"}
        body = json.dumps(payload).encode()
        signature = _make_signature(body, "test-secret")
        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "issues",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        mock_orchestrator.submit_task.assert_not_called()
