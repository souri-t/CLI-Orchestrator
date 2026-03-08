"""AI エージェント CLI をサンドボックス内で実行するモジュール。

Issue の内容からプロンプトを構築し、設定されたエージェント CLIでコーディングを実行する。

対応エージェント:
  - opencode: OpenCode CLI (`opencode run <prompt>`)
  - copilot:  GitHub Copilot CLI (`copilot -p <prompt> --autopilot`)
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.config import AppConfig
from orchestrator.github.issue_monitor import IssueTask
from orchestrator.logger import get_logger
from orchestrator.sandbox.sandbox import CONTAINER_WORKSPACE, Sandbox

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"
DEFAULT_SYSTEM_PROMPT = PROMPTS_DIR / "default_system.md"


class AgentError(Exception):
    """エージェント実行エラー。"""


class AgentRunner:
    """サンドボックス内で AI エージェント CLI を実行する。"""

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
            agent=self._config.agent.use,
        )

        # 1. リポジトリ clone
        self._clone_repo(sandbox, repo_url)

        # 2. ブランチ作成
        self._create_branch(sandbox, branch_name)

        # 3. エージェント CLI 実行 (設定されたエージェントに応じて切り替え)
        self._run_agent(sandbox, task)

        # 4. 変更差分を取得
        diff = self._get_diff(sandbox)

        if not diff.strip():
            log.warning(
                "no_changes",
                task=f"{task.repo_full_name}#{task.issue_number}",
            )
            raise AgentError(f"{self._agent_display_name} がファイルに変更を加えませんでした。")

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

    @property
    def _agent_display_name(self) -> str:
        """エージェントの表示名を返す。"""
        return {"opencode": "OpenCode CLI", "copilot": "GitHub Copilot CLI"}.get(
            self._config.agent.use, self._config.agent.use
        )

    def _run_agent(self, sandbox: Sandbox, task: IssueTask) -> None:
        """設定されたエージェント CLI にディスパッチする。"""
        if self._config.agent.use == "copilot":
            self._run_copilot(sandbox, task)
        else:
            self._run_opencode(sandbox, task)

    def _run_opencode(self, sandbox: Sandbox, task: IssueTask) -> None:
        """opencode CLI を非対話モードで実行する。"""
        prompt = self._build_prompt(task)
        opencode_cfg = self._config.agent.opencode

        # `opencode run` が非対話モードの正しいコマンド
        cmd = ["opencode", "run", prompt]
        if opencode_cfg.model:
            cmd += ["--model", opencode_cfg.model]

        # API キーを config.yaml の agent.opencode セクションから取得してコンテナに渡す
        creds = self._config.agent.opencode
        env: dict[str, str] = {}
        for key, val in [
            ("ANTHROPIC_API_KEY", creds.anthropic_api_key),
            ("ANTHROPIC_AUTH_TOKEN", creds.anthropic_auth_token),
            ("OPENAI_API_KEY", creds.openai_api_key),
            ("OPENAI_ORG_ID", creds.openai_org_id),
            ("GOOGLE_API_KEY", creds.google_api_key),
            ("OPENROUTER_API_KEY", creds.openrouter_api_key),
        ]:
            if val:
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

    def _run_copilot(self, sandbox: Sandbox, task: IssueTask) -> None:
        """GitHub Copilot CLI を非対話モードで実行する。"""
        prompt = self._build_prompt(task)
        copilot_cfg = self._config.agent.copilot

        cmd = [
            "copilot",
            "-p", prompt,
            "--autopilot",
            "--yolo",
            "--no-ask-user",
            "--max-autopilot-continues", str(copilot_cfg.max_autopilot_continues),
            "--deny-tool", "shell(git push)",  # push はホスト側で実行するため禁止
        ]
        if copilot_cfg.model:
            cmd += ["--model", copilot_cfg.model]

        # Copilot の認証は COPILOT_GITHUB_TOKEN のみ。
        # ホストの copilot login の OAuth トークンはキーチェーン保存のため環境変数で渡せないので、
        # GitHub Settings で発行した Fine-grained PAT ("Copilot Requests" 権限付き) を使用する。
        env: dict[str, str] = {}
        if token := self._config.agent.copilot.copilot_github_token:
            env["COPILOT_GITHUB_TOKEN"] = token

        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"

        log.info(
            "copilot_running",
            task=f"{task.repo_full_name}#{task.issue_number}",
            model=copilot_cfg.model or "(copilot default)",
            max_continues=copilot_cfg.max_autopilot_continues,
        )

        exit_code, output = sandbox.exec(cmd, env=env)

        log.info(
            "copilot_exit",
            task=f"{task.repo_full_name}#{task.issue_number}",
            exit_code=exit_code,
            output_tail=output[-2000:],
        )
        if exit_code != 0:
            raise AgentError(
                f"GitHub Copilot CLI が exit code {exit_code} で終了しました。\n"
                f"出力:\n{output[-2000:]}"
            )
        # exit_code=0 でも出力にエラーを含む場合を検知
        _error_patterns = [
            "AuthenticationError",
            "authentication failed",
            "token",
            "permission",
            "Unauthorized",
            "Bad credentials",
        ]
        output_lower = output.lower()
        for pat in _error_patterns:
            if pat.lower() in output_lower and exit_code != 0:
                raise AgentError(
                    f"GitHub Copilot CLI が認証エラーで終了しました。\n出力:\n{output[-2000:]}"
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
