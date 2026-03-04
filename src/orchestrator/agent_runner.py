"""GitHub Copilot CLI をサンドボックス内で実行するモジュール。

Issue の内容からプロンプトを構築し、
`copilot --autopilot --yolo` コマンドでコーディングを実行する。
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
    """サンドボックス内で Copilot CLI を実行する。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def run(self, sandbox: Sandbox, task: IssueTask, repo_url: str) -> str:
        """Issue タスクを処理する。

        1. リポジトリを clone
        2. ブランチを作成
        3. copilot --autopilot を実行
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

        # 3. Copilot CLI 実行
        self._run_copilot(sandbox, task)

        # 4. 変更差分を取得
        diff = self._get_diff(sandbox)

        if not diff.strip():
            log.warning(
                "no_changes",
                task=f"{task.repo_full_name}#{task.issue_number}",
            )
            raise AgentError("Copilot CLI がファイルに変更を加えませんでした。")

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

    def _run_copilot(self, sandbox: Sandbox, task: IssueTask) -> None:
        """copilot CLI を autopilot モードで実行する。"""
        prompt = self._build_prompt(task)
        copilot_cfg = self._config.copilot

        cmd = [
            "copilot",
            "--autopilot",
            "--yolo",
            "--max-autopilot-continues",
            str(copilot_cfg.max_autopilot_continues),
            "-p",
            prompt,
        ]
        if copilot_cfg.model:
            cmd += ["--model", copilot_cfg.model]

        log.info(
            "copilot_running",
            task=f"{task.repo_full_name}#{task.issue_number}",
            max_continues=copilot_cfg.max_autopilot_continues,
        )

        exit_code, output = sandbox.exec(cmd)

        # copilot は exit_code が 0 でなくても部分的に成功している場合がある
        # git diff で実際の変更を確認するため、ここではログのみ
        if exit_code != 0:
            log.warning(
                "copilot_nonzero_exit",
                exit_code=exit_code,
                output_tail=output[-1000:],
            )
        else:
            log.debug("copilot_output_tail", output=output[-500:])

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
