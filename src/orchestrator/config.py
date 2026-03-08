"""設定管理モジュール。

pydantic-settings を使い、config.yaml + 環境変数から設定を読み込む。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# サブ設定 モデル
# ---------------------------------------------------------------------------


class LabelsConfig(BaseModel):
    """Issue ラベル名の設定。"""

    trigger: str = "ai-task"
    wip: str = "ai-wip"
    done: str = "ai-done"
    fail: str = "ai-fail"
    pr_generated: str = "ai-generated"


class PollingConfig(BaseModel):
    """Polling モードの設定。"""

    interval_sec: int = Field(default=300, ge=30)


class WebhookConfig(BaseModel):
    """Webhook モードの設定。"""

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    path: str = "/webhook"
    secret: str = ""  # 環境変数 WEBHOOK_SECRET から上書き可


class SandboxConfig(BaseModel):
    """Docker サンドボックスの設定。"""

    image: str = "orchestrator-sandbox:latest"
    memory_limit: str = "4g"
    cpu_count: int = Field(default=2, ge=1)
    pids_limit: int = Field(default=256, ge=64)
    timeout_sec: int = Field(default=1800, ge=60)  # 30分
    work_dir_host: Path = Path("/tmp/orchestrator-work")
    # ネットワーク: 許可するホスト (空リストの場合はフル許可 - MVP向け)
    allowed_hosts: list[str] = Field(
        default=[
            "github.com",
            "api.github.com",
            "objects.githubusercontent.com",
            "api.anthropic.com",
            "api.openai.com",
            "api.gemini.com",
            "generativelanguage.googleapis.com",
            "openrouter.ai",
        ]
    )


class OpenCodeConfig(BaseModel):
    """OpenCode CLI の設定。"""

    model: str = ""  # 空の場合は .opencode.json のデフォルトモデルを使用


class PRConfig(BaseModel):
    """Pull Request の設定。"""

    draft: bool = True  # True: Draft PR, False: 通常 PR
    base_branch: str = ""  # 空の場合はリポジトリのデフォルトブランチを使用


class AuthConfig(BaseModel):
    """認証設定 (将来拡張用に予約。現在未使用)。"""

    opencode_dir: Path = Path("~/.opencode").expanduser()
    opencode_config_dir: Path = Path("~/.config/opencode").expanduser()


# ---------------------------------------------------------------------------
# メイン設定
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """アプリケーション全体の設定。"""

    # 監視対象リポジトリ (例: ["owner/repo-a", "owner/repo-b"])
    repositories: list[str] = Field(default_factory=list)

    # 動作モード
    mode: Literal["polling", "webhook"] = "polling"

    # 最大並行タスク数
    max_concurrent_tasks: int = Field(default=2, ge=1)

    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    pr: PRConfig = Field(default_factory=PRConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)


class Settings(BaseSettings):
    """環境変数から読み込む設定。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")

    # config.yaml のパス
    config_file: str = Field(default="config.yaml", alias="CONFIG_FILE")


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """config.yaml を読み込み AppConfig を返す。

    ファイルが存在しない場合はデフォルト値を使用する。
    """
    settings = Settings()
    path = Path(config_path or settings.config_file)

    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        config = AppConfig.model_validate(raw)
    else:
        config = AppConfig()

    # 環境変数で webhook.secret を上書き
    if settings.webhook_secret:
        config.webhook.secret = settings.webhook_secret

    return config


def load_settings() -> Settings:
    """環境変数設定を返す。"""
    return Settings()
