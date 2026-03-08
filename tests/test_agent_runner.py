"""agent_runner.py のユニットテスト。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.sandbox.agent_runner import AgentRunner
from orchestrator.config import AppConfig
from orchestrator.github.issue_monitor import IssueTask


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

