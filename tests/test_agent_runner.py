"""agent_runner.py のユニットテスト。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.config import AgentConfig, AppConfig, CopilotConfig
from orchestrator.github.issue_monitor import IssueTask
from orchestrator.sandbox.agent_runner import AgentError, AgentRunner


@pytest.fixture
def config() -> AppConfig:
    return AppConfig()


@pytest.fixture
def runner(config: AppConfig) -> AgentRunner:
    return AgentRunner(config)


@pytest.fixture
def task() -> IssueTask:
    return IssueTask(
        repo_full_name="owner/repo",
        issue_number=42,
        title="Fix null pointer exception",
        body="The app crashes when input is None",
        labels=["ai-task"],
        html_url="https://github.com/owner/repo/issues/42",
    )


class TestBranchName:
    def test_basic(self, task: IssueTask) -> None:
        branch = AgentRunner.get_branch_name(task)
        assert branch.startswith("ai/issue-42-")
        assert "fix-null-pointer-exception" in branch

    def test_special_chars_removed(self) -> None:
        task = IssueTask(
            repo_full_name="owner/repo",
            issue_number=1,
            title="Fix: bug! (urgent) & important",
            body="",
            labels=[],
        )
        branch = AgentRunner.get_branch_name(task)
        # 特殊文字はハイフンに置換される
        assert "!" not in branch
        assert "&" not in branch
        assert "(" not in branch

    def test_long_title_truncated(self) -> None:
        task = IssueTask(
            repo_full_name="owner/repo",
            issue_number=1,
            title="a" * 100,
            body="",
            labels=[],
        )
        branch = AgentRunner.get_branch_name(task)
        # ブランチ名が適切な長さに収まること
        assert len(branch) <= 80


class TestAgentRunner:
    def test_build_prompt_includes_issue_content(
        self, runner: AgentRunner, task: IssueTask
    ) -> None:
        """プロンプトに Issue の内容が含まれること。"""
        prompt = runner._build_prompt(task)
        assert "Fix null pointer exception" in prompt
        assert "The app crashes when input is None" in prompt
        assert "#42" in prompt

    def test_run_raises_on_clone_failure(
        self, runner: AgentRunner, task: IssueTask
    ) -> None:
        """clone 失敗時に AgentError が発生すること。"""
        from orchestrator.sandbox.agent_runner import AgentError

        mock_sandbox = MagicMock()
        mock_sandbox.exec.return_value = (1, "fatal: not a git repository")

        with pytest.raises(AgentError, match="git clone 失敗"):
            runner.run(mock_sandbox, task, "https://github.com/owner/repo.git")

    def test_run_raises_when_no_changes(
        self, runner: AgentRunner, task: IssueTask
    ) -> None:
        """変更がない場合に AgentError が発生すること。"""
        from orchestrator.sandbox.agent_runner import AgentError

        mock_sandbox = MagicMock()

        def exec_side_effect(cmd: list[str], **kwargs: object) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "rm -rf" in cmd_str:
                return (0, "")
            if "git clone" in cmd_str:
                return (0, "Cloning...")
            if "git checkout" in cmd_str:
                return (0, "")
            if "opencode" in cmd_str:
                return (0, "Done!")
            if "git add" in cmd_str:
                return (0, "")
            if "git diff" in cmd_str:
                return (0, "")  # 変更なし
            return (0, "")

        mock_sandbox.exec.side_effect = exec_side_effect

        with pytest.raises(AgentError, match="変更を加えませんでした"):
            runner.run(mock_sandbox, task, "https://github.com/owner/repo.git")


class TestCopilotRunner:
    """agent: copilot 時の AgentRunner テスト。"""

    @pytest.fixture
    def copilot_config(self) -> AppConfig:
        return AppConfig(agent=AgentConfig(use="copilot"))

    @pytest.fixture
    def copilot_runner(self, copilot_config: AppConfig) -> AgentRunner:
        return AgentRunner(copilot_config)

    @pytest.fixture
    def task(self) -> IssueTask:
        return IssueTask(
            repo_full_name="owner/repo",
            issue_number=7,
            title="Add input validation",
            body="Inputs should be validated before processing",
            labels=["ai-task"],
            html_url="https://github.com/owner/repo/issues/7",
        )

    def test_agent_display_name(self, copilot_runner: AgentRunner) -> None:
        """Copilot エージェントの表示名が正しいこと。"""
        assert copilot_runner._agent_display_name == "GitHub Copilot CLI"

    def test_run_raises_when_no_changes(
        self, copilot_runner: AgentRunner, task: IssueTask
    ) -> None:
        """変更がない場合に AgentError が発生すること。"""
        mock_sandbox = MagicMock()

        def exec_side_effect(cmd: list[str], **kwargs: object) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "rm -rf" in cmd_str:
                return (0, "")
            if "git clone" in cmd_str:
                return (0, "Cloning...")
            if "git checkout" in cmd_str:
                return (0, "")
            if "copilot" in cmd_str:
                return (0, "Done!")
            if "git add" in cmd_str:
                return (0, "")
            if "git diff" in cmd_str:
                return (0, "")  # 変更なし
            return (0, "")

        mock_sandbox.exec.side_effect = exec_side_effect

        with pytest.raises(AgentError, match="変更を加えませんでした"):
            copilot_runner.run(mock_sandbox, task, "https://github.com/owner/repo.git")

    def test_run_copilot_uses_correct_command(
        self, copilot_runner: AgentRunner, task: IssueTask
    ) -> None:
        """copilot コマンドが正しい引数で呼ばれること。"""
        mock_sandbox = MagicMock()
        captured_commands: list[list[str]] = []

        def exec_side_effect(cmd: list[str], **kwargs: object) -> tuple[int, str]:
            captured_commands.append(cmd)
            cmd_str = " ".join(cmd)
            if "rm -rf" in cmd_str:
                return (0, "")
            if "git clone" in cmd_str:
                return (0, "Cloning...")
            if "git checkout" in cmd_str:
                return (0, "")
            if "copilot" in cmd_str:
                return (0, "Done!")
            if "git add" in cmd_str:
                return (0, "")
            if "git diff" in cmd_str:
                return (0, "diff --git a/foo.py b/foo.py\n+x = 1")
            return (0, "")

        mock_sandbox.exec.side_effect = exec_side_effect

        copilot_runner.run(mock_sandbox, task, "https://github.com/owner/repo.git")

        # copilot コマンドが呼ばれたことを確認
        copilot_cmds = [c for c in captured_commands if c and c[0] == "copilot"]
        assert len(copilot_cmds) == 1
        copilot_cmd = copilot_cmds[0]
        assert "-p" in copilot_cmd
        assert "--autopilot" in copilot_cmd
        assert "--yolo" in copilot_cmd
        assert "--no-ask-user" in copilot_cmd

    def test_run_copilot_passes_token_env(
        self, task: IssueTask
    ) -> None:
        """credentials.copilot_github_token が sandbox.exec の env に渡されること。"""
        # credentials に token を設定した runner を作成
        config_with_token = AppConfig(
            agent=AgentConfig(
                use="copilot",
                copilot=CopilotConfig(copilot_github_token="github_pat_test123"),
            ),
        )
        runner_with_token = AgentRunner(config_with_token)

        mock_sandbox = MagicMock()
        captured_envs: list[dict[str, str]] = []

        def exec_side_effect(
            cmd: list[str], env: dict[str, str] | None = None, **kwargs: object
        ) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if env:
                captured_envs.append(env)
            if "rm -rf" in cmd_str:
                return (0, "")
            if "git clone" in cmd_str:
                return (0, "Cloning...")
            if "git checkout" in cmd_str:
                return (0, "")
            if "copilot" in cmd_str:
                return (0, "Done!")
            if "git add" in cmd_str:
                return (0, "")
            if "git diff" in cmd_str:
                return (0, "diff --git a/foo.py b/foo.py\n+x = 1")
            return (0, "")

        mock_sandbox.exec.side_effect = exec_side_effect

        runner_with_token.run(mock_sandbox, task, "https://github.com/owner/repo.git")

        # Copilot 実行時に COPILOT_GITHUB_TOKEN が env に含まれること
        assert any(
            "COPILOT_GITHUB_TOKEN" in env and env["COPILOT_GITHUB_TOKEN"] == "github_pat_test123"
            for env in captured_envs
        )

    def test_run_raises_on_nonzero_exit(
        self, copilot_runner: AgentRunner, task: IssueTask
    ) -> None:
        """Copilot CLI が非ゼロ exit code を返した場合に AgentError が発生すること。"""
        mock_sandbox = MagicMock()

        def exec_side_effect(cmd: list[str], **kwargs: object) -> tuple[int, str]:
            cmd_str = " ".join(cmd)
            if "rm -rf" in cmd_str:
                return (0, "")
            if "git clone" in cmd_str:
                return (0, "Cloning...")
            if "git checkout" in cmd_str:
                return (0, "")
            if "copilot" in cmd_str:
                return (1, "Error: authentication failed")
            return (0, "")

        mock_sandbox.exec.side_effect = exec_side_effect

        with pytest.raises(AgentError, match="exit code 1"):
            copilot_runner.run(mock_sandbox, task, "https://github.com/owner/repo.git")

