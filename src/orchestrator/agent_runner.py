"""OpenCode CLI をサンドボックス内で実行するモジュール。

Issue の内容からプロンプトを構築し、
`opencode -p` コマンドでコーディングを実行する。
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.config import AppConfig
from orchestrator.issue_monitor import IssueTask
from orchestrator.logger import get_logger
from orchestrator.sandbox import CONTAINER_WORKSPACE, Sandbox

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
DEFAULT_SYSTEM_PROMPT = PROMPTS_DIR / "default_system.md"


class AgentError(Exception):
    """エージェント実行エラー。"""


class AgentRunner:
    """サンドボックス内で OpenCode CLI を実行する。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def run(self, sandbox: Sandbox, task: IssueTask, repo_url: str) -> str:
        """Issue タスクを処理する。

        1. リポジトリを clone
        2. ブランチを作成
        3. opencode -p を実行
        4. git diff を取得して返す

        Returns:
            git diff の文字列 (変更がない場合は空文字列)

        Raises:
            AgentError: 処理失敗時
        """
        branch_name = self._branch_name(task)

        log.info(
            "agent_start",
            task=f"{task.repo_full_name}#{task.issue_number}",
            branch=branch_name,
        )

        # 1. リポジトリ clone
        self._clone_repo(sandbox, repo_url)

        # 2. ブランチ作成
        self._create_branch(sandbox, branch_name)

        # 3. OpenCode CLI 実行
        self._run_opencode(sandbox, task)

        # 4. 変更差分を取得
        diff = self._get_diff(sandbox)

        if not diff.strip():
            log.warning(
                "no_changes",
                task=f"{task.repo_full_name}#{task.issue_number}",
            )
            raise AgentError("OpenCode CLI がファイルに変更を加えませんでした。")

        log.info(
            "agent_success",
            task=f"{task.repo_full_name}#{task.issue_number}",
            diff_lines=len(diff.splitlines()),
        )
        return diff

    def _clone_repo(self, sandbox: Sandbox, repo_url: str) -> None:
        """リポジトリを clone する。"""
        # 作業ディレクトリをクリア
        exit_code, output = sandbox.exec(
            ["sh", "-c", f"rm -rf {CONTAINER_WORKSPACE}/* {CONTAINER_WORKSPACE}/.[!.]*"]
        )

        exit_code, output = sandbox.exec(
            ["git", "clone", "--depth=1", repo_url, "."],
        )
        if exit_code != 0:
            raise AgentError(f"git clone 失敗:\n{output}")
        log.debug("repo_cloned", url=repo_url)

    def _create_branch(self, sandbox: Sandbox, branch_name: str) -> None:
        """新しいブランチを作成する。"""
        exit_code, output = sandbox.exec(
            ["git", "checkout", "-b", branch_name],
        )
        if exit_code != 0:
            raise AgentError(f"ブランチ作成失敗 ({branch_name}):\n{output}")
        log.debug("branch_created", branch=branch_name)

    def _run_opencode(self, sandbox: Sandbox, task: IssueTask) -> None:
        """opencode CLI を非対話モードで実行する。"""
        import os

        prompt = self._build_prompt(task)
        opencode_cfg = self._config.opencode

        # `opencode run` が非対話モードの正しいコマンド
        cmd = ["opencode", "run", prompt]
        if opencode_cfg.model:
            cmd += ["--model", opencode_cfg.model]

        # API キーを環境変数から取得してコンテナに渡す
        api_key_vars = [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_API_KEY",
            "OPENAI_ORG_ID",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
        ]
        env: dict[str, str] = {}
        for key in api_key_vars:
            if val := os.environ.get(key):
                env[key] = val

        # 全ツールを自動承認 (コンテナはサンドボックス内なので安全)
        env["OPENCODE_PERMISSION"] = '{"bash":"allow","write":"allow","read":"allow"}'
        # ターミナル非対話ティップスを無効化
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"

        log.info(
            "opencode_running",
            task=f"{task.repo_full_name}#{task.issue_number}",
            model=opencode_cfg.model or "(config default)",
        )

        exit_code, output = sandbox.exec(cmd, env=env)

        log.info(
            "opencode_exit",
            task=f"{task.repo_full_name}#{task.issue_number}",
            exit_code=exit_code,
            output_tail=output[-2000:],
        )
        if exit_code != 0:
            raise AgentError(
                f"OpenCode CLI が exit code {exit_code} で終了しました。\n"
                f"出力:\n{output[-2000:]}"
            )
        # exit_code=0 でも出力にエラーを含む場合を検知
        _error_patterns = [
            "Error: Model not found",
            "ProviderModelNotFoundError",
            "AuthenticationError",
            "API key",
        ]
        for pat in _error_patterns:
            if pat in output:
                raise AgentError(
                    f"OpenCode CLI がエラーで終了しました。\n出力:\n{output[-2000:]}"
                )

    def _get_diff(self, sandbox: Sandbox) -> str:
        """git diff (ステージング含む) を取得する。"""
        # まず add -A で全変更をステージ
        sandbox.exec(["git", "add", "-A"])

        exit_code, diff = sandbox.exec(["git", "diff", "--cached"])
        if exit_code != 0:
            raise AgentError(f"git diff 取得失敗:\n{diff}")
        return diff

    def _build_prompt(self, task: IssueTask) -> str:
        """Issue の内容からプロンプトを構築する。"""
        system_prompt = ""
        if DEFAULT_SYSTEM_PROMPT.exists():
            system_prompt = DEFAULT_SYSTEM_PROMPT.read_text(encoding="utf-8") + "\n\n"

        return system_prompt + task.to_prompt_context()

    @staticmethod
    def _branch_name(task: IssueTask) -> str:
        """Issue から安全なブランチ名を生成する。"""
        import re

        slug = re.sub(r"[^a-zA-Z0-9\-]", "-", task.title.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
        slug = slug[:50]
        return f"ai/issue-{task.issue_number}-{slug}"

    @staticmethod
    def get_branch_name(task: IssueTask) -> str:
        """外部からブランチ名を取得するためのパブリックメソッド。"""
        return AgentRunner._branch_name(task)
