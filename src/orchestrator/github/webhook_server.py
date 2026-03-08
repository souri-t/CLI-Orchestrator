"""Webhook モード用の FastAPI サーバー。

GitHub Webhook の issues イベントを受信し、
ai-task ラベルが付いた Issue をパイプラインに投入する。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from orchestrator.github.issue_monitor import IssueTask
from orchestrator.logger import get_logger

if TYPE_CHECKING:
    from orchestrator.trigger import Orchestrator

log = get_logger(__name__)


def create_app(orchestrator: "Orchestrator", webhook_secret: str) -> FastAPI:
    """FastAPI アプリケーションを生成する。

    Args:
        orchestrator: タスクを処理する Orchestrator インスタンス
        webhook_secret: GitHub Webhook の署名検証用シークレット

    Returns:
        設定済みの FastAPI アプリ
    """
    app = FastAPI(
        title="Orchestrator Webhook",
        description="GitHub Issue → PR 自動化 Orchestrator の Webhook エンドポイント",
        version="0.1.0",
    )

    @app.get("/health")
    async def health_check() -> JSONResponse:
        """ヘルスチェックエンドポイント。"""
        return JSONResponse({"status": "ok"})

    @app.post("/webhook")
    async def handle_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
        x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    ) -> JSONResponse:
        """GitHub Webhook イベントを受信するエンドポイント。"""
        body = await request.body()

        # 署名検証
        if webhook_secret:
            _verify_signature(body, x_hub_signature_256, webhook_secret)

        # issues イベント以外は無視
        if x_github_event != "issues":
            log.debug("webhook_ignored", gh_event=x_github_event, delivery=x_github_delivery)
            return JSONResponse({"message": f"event '{x_github_event}' ignored"})

        # ペイロードをパース
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        action = payload.get("action", "")
        issue_data = payload.get("issue", {})
        repo_data = payload.get("repository", {})

        # opened または labeled アクションのみ処理
        if action not in ("opened", "labeled"):
            log.debug("webhook_action_ignored", gh_action=action)
            return JSONResponse({"message": f"action '{action}' ignored"})

        # ラベルを確認
        trigger_label = orchestrator.issue_monitor._labels.trigger
        issue_labels = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]

        if trigger_label not in issue_labels:
            log.debug(
                "webhook_no_trigger_label",
                issue=issue_data.get("number"),
                labels=issue_labels,
            )
            return JSONResponse({"message": "trigger label not found, ignored"})

        # IssueTask を構築してパイプラインに投入
        task = _build_task_from_payload(issue_data, repo_data)
        if task is None:
            raise HTTPException(status_code=422, detail="Failed to parse issue payload")

        log.info(
            "webhook_issue_received",
            repo=task.repo_full_name,
            issue=task.issue_number,
            gh_action=action,
            delivery=x_github_delivery,
        )

        # ラベル遷移は IssueMonitor が行うが、Webhook の場合はここで直接 submit
        # (fetch_pending_issues を経由するとポーリングと重複する可能性がある)
        orchestrator.submit_task(task)

        return JSONResponse(
            {"message": "task submitted", "issue": task.issue_number},
            status_code=status.HTTP_202_ACCEPTED,
        )

    return app


def _verify_signature(body: bytes, signature_header: str | None, secret: str) -> None:
    """GitHub Webhook の署名を検証する。

    Raises:
        HTTPException: 署名が無効な場合
    """
    if not signature_header:
        raise HTTPException(
            status_code=403,
            detail="X-Hub-Signature-256 header missing",
        )

    if not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=403,
            detail="Invalid signature format",
        )

    expected = hmac.new(
        secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    received = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected, received):
        raise HTTPException(
            status_code=403,
            detail="Invalid webhook signature",
        )


def _build_task_from_payload(
    issue_data: dict,  # type: ignore[type-arg]
    repo_data: dict,  # type: ignore[type-arg]
) -> IssueTask | None:
    """GitHub Webhook ペイロードから IssueTask を構築する。"""
    try:
        return IssueTask(
            repo_full_name=repo_data["full_name"],
            issue_number=issue_data["number"],
            title=issue_data.get("title", ""),
            body=issue_data.get("body") or "",
            labels=[lbl["name"] for lbl in issue_data.get("labels", [])],
            html_url=issue_data.get("html_url", ""),
            comments=[],  # Webhook ペイロードにコメントは含まれないため空
        )
    except (KeyError, TypeError) as e:
        log.error("payload_parse_error", error=str(e))
        return None
