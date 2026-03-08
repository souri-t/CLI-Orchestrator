"""Orchestrator CLI エントリーポイント。

使用方法:
    orchestrator run            # ポーリング or webhook を設定に従って起動
    orchestrator run-once       # 1回だけポーリングして終了 (デバッグ用)
    orchestrator status         # 設定と GitHub 接続を確認
    orchestrator check-keys     # AI API キーの有効性を検証
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import NamedTuple

import click
from github import Auth, Github, GithubException

from orchestrator.config import AppConfig, load_config
from orchestrator.logger import get_logger, setup_logging
from orchestrator.trigger import Orchestrator

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# API キーヘルスチェック
# ---------------------------------------------------------------------------

class _KeyResult(NamedTuple):
    provider: str
    env_var: str
    ok: bool
    message: str


def _check_api_keys(app_config: AppConfig | None = None) -> list[_KeyResult]:
    """AI API キーを実際にリクエストして検証する。

    app_config が指定された場合は config.yaml の agent セクションから読み込む。
    省略された場合は環境変数から読み込む (後方互换・テスト用)。
    """
    import httpx

    oc = app_config.agent.opencode if app_config else None
    cp = app_config.agent.copilot if app_config else None

    def _get(env_var: str, cfg_val: str) -> str:
        """app_config が渡されていればその値を、なければ環境変数を返す。"""
        if app_config is not None:
            return cfg_val
        return os.environ.get(env_var, "")

    results: list[_KeyResult] = []

    # --- Anthropic ---
    anthropic_key = _get("ANTHROPIC_API_KEY", oc.anthropic_api_key if oc else "") or \
        _get("ANTHROPIC_AUTH_TOKEN", oc.anthropic_auth_token if oc else "")
    if anthropic_key:
        try:
            r = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=10.0,
            )
            if r.status_code == 200:
                results.append(_KeyResult("Anthropic", "ANTHROPIC_API_KEY", True, "OK"))
            elif r.status_code == 401:
                results.append(_KeyResult("Anthropic", "ANTHROPIC_API_KEY", False, "認証失敗 (無効なキー)"))
            else:
                results.append(_KeyResult("Anthropic", "ANTHROPIC_API_KEY", False, f"HTTP {r.status_code}"))
        except Exception as e:
            results.append(_KeyResult("Anthropic", "ANTHROPIC_API_KEY", False, f"接続エラー: {e}"))
    else:
        results.append(_KeyResult("Anthropic", "ANTHROPIC_API_KEY", False, "未設定"))

    # --- OpenAI ---
    openai_key = _get("OPENAI_API_KEY", oc.openai_api_key if oc else "")
    if openai_key:
        try:
            r = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {openai_key}"},
                timeout=10.0,
            )
            if r.status_code == 200:
                results.append(_KeyResult("OpenAI", "OPENAI_API_KEY", True, "OK"))
            elif r.status_code == 401:
                results.append(_KeyResult("OpenAI", "OPENAI_API_KEY", False, "認証失敗 (無効なキー)"))
            else:
                results.append(_KeyResult("OpenAI", "OPENAI_API_KEY", False, f"HTTP {r.status_code}"))
        except Exception as e:
            results.append(_KeyResult("OpenAI", "OPENAI_API_KEY", False, f"接続エラー: {e}"))
    else:
        results.append(_KeyResult("OpenAI", "OPENAI_API_KEY", False, "未設定"))

    # --- Google ---
    google_key = _get("GOOGLE_API_KEY", oc.google_api_key if oc else "")
    if google_key:
        try:
            r = httpx.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={google_key}",
                timeout=10.0,
            )
            if r.status_code == 200:
                results.append(_KeyResult("Google", "GOOGLE_API_KEY", True, "OK"))
            elif r.status_code in (400, 403):
                results.append(_KeyResult("Google", "GOOGLE_API_KEY", False, "認証失敗 (無効なキー)"))
            else:
                results.append(_KeyResult("Google", "GOOGLE_API_KEY", False, f"HTTP {r.status_code}"))
        except Exception as e:
            results.append(_KeyResult("Google", "GOOGLE_API_KEY", False, f"接続エラー: {e}"))
    else:
        results.append(_KeyResult("Google", "GOOGLE_API_KEY", False, "未設定"))

    # --- OpenRouter ---
    openrouter_key = _get("OPENROUTER_API_KEY", oc.openrouter_api_key if oc else "")
    if openrouter_key:
        try:
            r = httpx.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {openrouter_key}"},
                timeout=10.0,
            )
            if r.status_code == 200:
                results.append(_KeyResult("OpenRouter", "OPENROUTER_API_KEY", True, "OK"))
            elif r.status_code == 401:
                results.append(_KeyResult("OpenRouter", "OPENROUTER_API_KEY", False, "認証失敗 (無効なキー)"))
            else:
                results.append(_KeyResult("OpenRouter", "OPENROUTER_API_KEY", False, f"HTTP {r.status_code}"))
        except Exception as e:
            results.append(_KeyResult("OpenRouter", "OPENROUTER_API_KEY", False, f"接続エラー: {e}"))
    else:
        results.append(_KeyResult("OpenRouter", "OPENROUTER_API_KEY", False, "未設定"))

    # --- GitHub Copilot (agent: "copilot" 時に必要) ---
    copilot_token = _get("COPILOT_GITHUB_TOKEN", cp.copilot_github_token if cp else "")
    if copilot_token:
        try:
            r = httpx.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {copilot_token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10.0,
            )
            if r.status_code == 200:
                login = r.json().get("login", "?")
                results.append(
                    _KeyResult("GitHub Copilot", "COPILOT_GITHUB_TOKEN", True, f"OK (@{login})")
                )
            elif r.status_code == 401:
                results.append(
                    _KeyResult(
                        "GitHub Copilot",
                        "COPILOT_GITHUB_TOKEN",
                        False,
                        "認証失敗 (無効なトークン)",
                    )
                )
            else:
                results.append(
                    _KeyResult(
                        "GitHub Copilot",
                        "COPILOT_GITHUB_TOKEN",
                        False,
                        f"HTTP {r.status_code}",
                    )
                )
        except Exception as e:
            results.append(
                _KeyResult("GitHub Copilot", "COPILOT_GITHUB_TOKEN", False, f"接続エラー: {e}")
            )
    else:
        results.append(
            _KeyResult(
                "GitHub Copilot",
                "COPILOT_GITHUB_TOKEN",
                False,
                "未設定 (agent: copilot 時に必須)",
            )
        )

    return results


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

    if not app_config.credentials.github_token:
        click.echo("❌ GITHUB_TOKEN が設定されていません。config.yaml の credentials.github_token を設定してください。", err=True)
        sys.exit(1)

    if not app_config.repositories:
        click.echo("❌ config.yaml に repositories が設定されていません。", err=True)
        sys.exit(1)

    # AI API キー / Copilot トークンのチェック
    oc = app_config.agent.opencode
    if app_config.agent.use == "copilot":
        if not app_config.agent.copilot.copilot_github_token:
            click.echo(
                "⚠️  copilot_github_token が未設定です。"
                " config.yaml の agent.copilot.copilot_github_token に"
                " Fine-grained PAT (\"Copilot Requests\" 権限付き) を設定してください。",
                err=True,
            )
    elif not any([
        oc.anthropic_api_key, oc.anthropic_auth_token,
        oc.openai_api_key, oc.google_api_key, oc.openrouter_api_key,
    ]):
        click.echo(
            "⚠️  AI API キーが未設定です。"
            " config.yaml の agent.opencode セクションに"
            " anthropic_api_key / openai_api_key / google_api_key のいずれかを設定してください。",
            err=True,
        )

    log.info(
        "orchestrator_starting",
        mode=app_config.mode,
        repos=app_config.repositories,
        max_concurrent=app_config.max_concurrent_tasks,
    )

    orchestrator = Orchestrator(config=app_config, github_token=app_config.credentials.github_token)

    if app_config.mode == "polling":
        _run_polling(orchestrator, app_config.polling.interval_sec)
    elif app_config.mode == "webhook":
        _run_webhook(orchestrator, app_config, app_config.credentials.webhook_secret)
    else:
        click.echo(f"❌ 不明なモード: {app_config.mode}", err=True)
        sys.exit(1)


@cli.command("run-once")
@click.pass_context
def run_once(ctx: click.Context) -> None:
    """1回だけポーリングを実行して終了する (デバッグ・テスト用)。"""
    config_path: str = ctx.obj["config_path"]
    app_config = load_config(config_path)

    if not app_config.credentials.github_token:
        click.echo("❌ GITHUB_TOKEN が設定されていません。config.yaml の credentials.github_token を設定してください。", err=True)
        sys.exit(1)

    orchestrator = Orchestrator(config=app_config, github_token=app_config.credentials.github_token)

    log.info("run_once_start", repos=app_config.repositories)
    count = orchestrator.poll_once()
    click.echo(f"✅ {count} 件のタスクをキューに投入しました。完了を待機中...")

    # すべてのタスクが完了するまで待機
    orchestrator.shutdown(wait=True)
    click.echo("✅ 全タスク完了。")


@cli.command("check-keys")
@click.pass_context
def check_keys(ctx: click.Context) -> None:
    """AI API キーおよび Copilot トークンの有効性を実際に検証する。"""
    config_path: str = ctx.obj["config_path"]
    app_config = load_config(config_path)
    click.echo("=== AI API キー / Copilot トークン ヘルスチェック ===\n")
    results = _check_api_keys(app_config)
    all_ok = False
    for r in results:
        icon = "✅" if r.ok else "❌"
        click.echo(f"{icon} {r.provider:<12} ({r.env_var}): {r.message}")
        if r.ok:
            all_ok = True
    click.echo()
    if all_ok:
        click.echo("✅ 少なくとも1つのプロバイダのキーが有効です。")
    else:
        click.echo("❌ 有効な API キーがありません。config.yaml の credentials セクションにキーを設定してください。")
        sys.exit(1)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """設定を確認し、GitHub API への接続をテストする。"""
    config_path: str = ctx.obj["config_path"]
    app_config = load_config(config_path)

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

    opencode_dir = Path(app_config.auth.opencode_dir).expanduser()
    opencode_config_dir = Path(app_config.auth.opencode_config_dir).expanduser()
    opencode_dir_status = "✅" if opencode_dir.exists() else "❌ (not found)"
    opencode_cfg_status = "✅" if opencode_config_dir.exists() else "❌ (not found)"
    click.echo(f"\nOpenCode Dir : {opencode_dir} {opencode_dir_status}")
    click.echo(f"OpenCode Cfg : {opencode_config_dir} {opencode_cfg_status}")

    # GitHub API 接続テスト
    click.echo("\n--- GitHub API ---")
    if not app_config.credentials.github_token:
        click.echo("❌ GITHUB_TOKEN が設定されていません。config.yaml の credentials.github_token を設定してください。")
        return

    try:
        gh = Github(auth=Auth.Token(app_config.credentials.github_token))
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

    # AI API キーチェック
    click.echo("\n--- AI API キー ---")
    click.echo("(各プロバイダの API エンドポイントに接続して検証します...)")
    key_results = _check_api_keys(app_config)
    for r in key_results:
        icon = "✅" if r.ok else ("⚠️ " if r.message == "未設定" else "❌")
        click.echo(f"  {icon} {r.provider:<12} ({r.env_var}): {r.message}")
    any_ok = any(r.ok for r in key_results)
    if any_ok:
        click.echo("→ 少なくとも1つの AI プロバイダキーが有効です。")
    else:
        click.echo("→ ❌ 有効な AI API キーがありません。opencode がタスクを実行できません。")


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
    from orchestrator.github.webhook_server import create_app

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
