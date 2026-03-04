"""Orchestrator CLI エントリーポイント。

使用方法:
    orchestrator run            # ポーリング or webhook を設定に従って起動
    orchestrator run-once       # 1回だけポーリングして終了 (デバッグ用)
    orchestrator status         # 設定と GitHub 接続を確認
"""
from __future__ import annotations

import signal
import sys
import time

import click
from github import Auth, Github, GithubException

from orchestrator.config import load_config, load_settings
from orchestrator.logger import get_logger, setup_logging
from orchestrator.trigger import Orchestrator

log = get_logger(__name__)


@click.group()
@click.option("--config", "-c", default="config.yaml", help="設定ファイルのパス")
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="ログレベル",
)
@click.pass_context
def cli(ctx: click.Context, config: str, log_level: str) -> None:
    """GitHub Issue → PR 自動化 Orchestrator."""
    setup_logging(log_level)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """設定に従って Orchestrator を起動する。

    mode: polling の場合 → ポーリングループを開始
    mode: webhook の場合 → Webhook サーバーを起動
    """
    config_path: str = ctx.obj["config_path"]
    app_config = load_config(config_path)
    settings = load_settings()

    if not settings.github_token:
        click.echo("❌ GITHUB_TOKEN が設定されていません。", err=True)
        sys.exit(1)

    if not app_config.repositories:
        click.echo("❌ config.yaml に repositories が設定されていません。", err=True)
        sys.exit(1)

    log.info(
        "orchestrator_starting",
        mode=app_config.mode,
        repos=app_config.repositories,
        max_concurrent=app_config.max_concurrent_tasks,
    )

    orchestrator = Orchestrator(config=app_config, github_token=settings.github_token)

    if app_config.mode == "polling":
        _run_polling(orchestrator, app_config.polling.interval_sec)
    elif app_config.mode == "webhook":
        _run_webhook(orchestrator, app_config, settings.webhook_secret)
    else:
        click.echo(f"❌ 不明なモード: {app_config.mode}", err=True)
        sys.exit(1)


@cli.command("run-once")
@click.pass_context
def run_once(ctx: click.Context) -> None:
    """1回だけポーリングを実行して終了する (デバッグ・テスト用)。"""
    config_path: str = ctx.obj["config_path"]
    app_config = load_config(config_path)
    settings = load_settings()

    if not settings.github_token:
        click.echo("❌ GITHUB_TOKEN が設定されていません。", err=True)
        sys.exit(1)

    orchestrator = Orchestrator(config=app_config, github_token=settings.github_token)

    log.info("run_once_start", repos=app_config.repositories)
    count = orchestrator.poll_once()
    click.echo(f"✅ {count} 件のタスクをキューに投入しました。完了を待機中...")

    # すべてのタスクが完了するまで待機
    orchestrator.shutdown(wait=True)
    click.echo("✅ 全タスク完了。")


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """設定を確認し、GitHub API への接続をテストする。"""
    config_path: str = ctx.obj["config_path"]
    app_config = load_config(config_path)
    settings = load_settings()

    click.echo("=== Orchestrator Status ===\n")

    # 設定表示
    click.echo(f"Mode         : {app_config.mode}")
    click.echo(f"Repositories : {', '.join(app_config.repositories) or '(未設定)'}")
    click.echo(f"Max Tasks    : {app_config.max_concurrent_tasks}")
    click.echo(f"Trigger Label: {app_config.labels.trigger}")
    click.echo(f"Sandbox Image: {app_config.sandbox.image}")
    click.echo(f"Timeout      : {app_config.sandbox.timeout_sec}s")

    if app_config.mode == "polling":
        click.echo(f"Poll Interval: {app_config.polling.interval_sec}s")
    else:
        click.echo(f"Webhook Port : {app_config.webhook.port}")

    # 認証ファイルの存在確認
    from pathlib import Path

    copilot_dir = Path(app_config.auth.copilot_dir).expanduser()
    gh_copilot_dir = Path(app_config.auth.github_copilot_config_dir).expanduser()
    click.echo(f"\nCopilot Dir  : {copilot_dir} {'✅' if copilot_dir.exists() else '❌ (not found)'}")
    click.echo(
        f"GH Copilot   : {gh_copilot_dir} {'✅' if gh_copilot_dir.exists() else '❌ (not found)'}"
    )

    # GitHub API 接続テスト
    click.echo("\n--- GitHub API ---")
    if not settings.github_token:
        click.echo("❌ GITHUB_TOKEN が設定されていません。")
        return

    try:
        gh = Github(auth=Auth.Token(settings.github_token))
        user = gh.get_user()
        click.echo(f"✅ 認証成功: @{user.login}")

        for repo_name in app_config.repositories:
            try:
                repo = gh.get_repo(repo_name)
                click.echo(f"✅ リポジトリアクセス OK: {repo.full_name}")
            except GithubException as e:
                click.echo(f"❌ リポジトリアクセス失敗: {repo_name} ({e.status})")
    except GithubException as e:
        click.echo(f"❌ GitHub API 認証失敗: {e}")


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------


def _run_polling(orchestrator: Orchestrator, interval_sec: int) -> None:
    """ポーリングループ。Ctrl+C で停止。"""
    click.echo(f"🔄 ポーリングモードで起動 (間隔: {interval_sec}秒) ... Ctrl+C で停止")

    def _handle_sigterm(signum: int, frame: object) -> None:
        log.info("received_sigterm")
        orchestrator.shutdown(wait=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while True:
            try:
                count = orchestrator.poll_once()
                if count:
                    log.info("poll_submitted_tasks", count=count)
            except Exception:
                log.exception("poll_error")

            time.sleep(interval_sec)
    except KeyboardInterrupt:
        click.echo("\n⏹  停止中 (実行中のタスクを待機)...")
        orchestrator.shutdown(wait=True)
        click.echo("✅ 停止完了。")


def _run_webhook(
    orchestrator: Orchestrator,
    app_config: object,
    webhook_secret: str,
) -> None:
    """Webhook サーバーを起動する。"""
    import uvicorn

    from orchestrator.config import AppConfig
    from orchestrator.webhook_server import create_app

    config: AppConfig = app_config  # type: ignore[assignment]
    fastapi_app = create_app(orchestrator, webhook_secret)

    if not webhook_secret:
        click.echo(
            "⚠️  WEBHOOK_SECRET が設定されていません。署名検証がスキップされます。",
            err=True,
        )

    click.echo(
        f"🌐 Webhook サーバー起動: http://{config.webhook.host}:{config.webhook.port}{config.webhook.path}"
    )

    uvicorn.run(
        fastapi_app,
        host=config.webhook.host,
        port=config.webhook.port,
        log_level="info",
    )
