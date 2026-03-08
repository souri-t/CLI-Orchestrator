"""設定管理モジュール。

config.yaml から設定を読み込む。認証情報も config.yaml の credentials セクションで管理する。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

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
            "copilot-proxy.githubusercontent.com",  # GitHub Copilot CLI 用
        ]
    )


class OpenCodeConfig(BaseModel):
    """OpenCode CLI の設定。"""

    model: str = ""  # 空の場合は .opencode.json のデフォルトモデルを使用

    # 使用する AI プロバイダの API キー (agent: "opencode" 時に必要)
    anthropic_api_key: str = ""      # sk-ant-api03-...
    anthropic_auth_token: str = ""   # anthropic_api_key の代替トークン
    openai_api_key: str = ""         # sk-...
    openai_org_id: str = ""          # org-... (オプション)
    google_api_key: str = ""         # AIza...
    openrouter_api_key: str = ""     # sk-or-v1-...


class CopilotConfig(BaseModel):
    """GitHub Copilot CLI の設定。"""

    model: str = ""  # 空の場合は Copilot のデフォルトモデルを使用
    max_autopilot_continues: int = Field(default=20, ge=1)  # 最大自律継続ステップ数

    # GitHub Copilot CLI 用 Fine-grained PAT (agent: "copilot" 時に必須)
    # GitHub Settings で "Copilot Requests" 権限付き PAT (github_pat_) を発行すること
    copilot_github_token: str = ""


class AgentConfig(BaseModel):
    """AI エージェント CLI の設定。"""

    # 使用するエージェント: "opencode" または "copilot"
    use: Literal["opencode", "copilot"] = "opencode"

    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    copilot: CopilotConfig = Field(default_factory=CopilotConfig)


class PRConfig(BaseModel):
    """Pull Request の設定。"""

    draft: bool = True  # True: Draft PR, False: 通常 PR
    base_branch: str = ""  # 空の場合はリポジトリのデフォルトブランチを使用


class AuthConfig(BaseModel):
    """認証設定 (将来拡張用に予約。現在未使用)。"""

    opencode_dir: Path = Path("~/.opencode").expanduser()
    opencode_config_dir: Path = Path("~/.config/opencode").expanduser()


class CredentialsConfig(BaseModel):
    """認証情報の設定。.env の代わりに config.yaml の credentials セクションで管理する。"""

    # GitHub 連携
    github_token: str = ""       # GitHub API トークン (Fine-grained PAT または Classic PAT)
    webhook_secret: str = ""     # Webhook HMAC-SHA256 シークレット (webhook モード時に必須)


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
    agent: AgentConfig = Field(default_factory=AgentConfig)
    pr: PRConfig = Field(default_factory=PRConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """config.yaml を読み込み AppConfig を返す。

    config_path が省略された場合は環境変数 CONFIG_FILE (デフォルト: config.yaml) を使用する。
    ファイルが存在しない場合はデフォルト値を使用する。
    """
    path = Path(config_path or os.environ.get("CONFIG_FILE", "config.yaml"))

    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        config = AppConfig.model_validate(raw)
    else:
        config = AppConfig()

    # credentials.webhook_secret が設定済みで webhook.secret が未設定の場合は反映
    if config.credentials.webhook_secret and not config.webhook.secret:
        config.webhook.secret = config.credentials.webhook_secret

    return config
