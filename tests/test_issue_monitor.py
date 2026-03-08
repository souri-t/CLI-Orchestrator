"""issue_monitor.py のユニットテスト。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.config import AppConfig
from orchestrator.github.issue_monitor import IssueMonitor, IssueTask


@pytest.fixture
def config() -> AppConfig:
    cfg = AppConfig()
    cfg.repositories = ["test-owner/test-repo"]
    return cfg


@pytest.fixture
def monitor(config: AppConfig) -> IssueMonitor:
    return IssueMonitor(github_token="test-token", config=config)


class TestIssueTask:
    def test_to_prompt_context_basic(self) -> None:
        task = IssueTask(
            repo_full_name="owner/repo",
            issue_number=42,
            title="Fix the bug",
            body="There is a bug in the code",
            labels=["ai-task", "bug"],
            html_url="https://github.com/owner/repo/issues/42",
        )
        context = task.to_prompt_context()
        assert "Fix the bug" in context
        assert "#42" in context
        assert "owner/repo" in context
        assert "There is a bug in the code" in context

    def test_to_prompt_context_with_comments(self) -> None:
        task = IssueTask(
            repo_full_name="owner/repo",
            issue_number=1,
            title="Test",
            body="body",
            labels=[],
            comments=["Comment 1", "Comment 2"],
        )
        context = task.to_prompt_context()
        assert "Comment 1" in context
        assert "Comment 2" in context

    def test_to_prompt_context_empty_body(self) -> None:
        task = IssueTask(
            repo_full_name="owner/repo",
            issue_number=1,
            title="Test",
            body="",
            labels=[],
        )
        context = task.to_prompt_context()
        assert "(no description provided)" in context


class TestIssueMonitor:
    @patch("orchestrator.github.issue_monitor.Github")
    def test_fetch_pending_issues_skips_wip(
        self, mock_github_cls: MagicMock, monitor: IssueMonitor
    ) -> None:
        """ai-wip ラベルが付いた Issue はスキップされること。"""
        mock_issue = MagicMock()
        mock_issue.labels = [MagicMock(name="ai-wip"), MagicMock(name="ai-task")]
        mock_issue.labels[0].name = "ai-wip"
        mock_issue.labels[1].name = "ai-task"

        mock_repo = MagicMock()
        mock_repo.full_name = "test-owner/test-repo"
        mock_repo.get_issues.return_value = [mock_issue]

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_github_cls.return_value = mock_gh

        # ai-wip が付いているのでスキップ
        monitor._gh = mock_gh
        result = monitor._fetch_from_repo(mock_repo)
        assert result == []

    @patch("orchestrator.github.issue_monitor.Github")
    def test_fetch_pending_issues_returns_task(
        self, mock_github_cls: MagicMock, monitor: IssueMonitor
    ) -> None:
        """ai-task ラベルのみ付いた Issue はタスクとして返されること。"""
        mock_label = MagicMock()
        mock_label.name = "ai-task"

        mock_issue = MagicMock()
        mock_issue.labels = [mock_label]
        mock_issue.number = 10
        mock_issue.title = "Test Issue"
        mock_issue.body = "Test body"
        mock_issue.html_url = "https://github.com/test/10"
        mock_issue.get_comments.return_value = []

        mock_repo = MagicMock()
        mock_repo.full_name = "test-owner/test-repo"
        mock_repo.get_issues.return_value = [mock_issue]

        monitor._gh = MagicMock()
        monitor._gh.get_repo.return_value = mock_repo

        result = monitor._fetch_from_repo(mock_repo)

        assert len(result) == 1
        assert result[0].issue_number == 10
        assert result[0].title == "Test Issue"
        # ラベル遷移が呼ばれたことを確認
        mock_issue.remove_from_labels.assert_called_once_with("ai-task")
        mock_issue.add_to_labels.assert_called_once_with("ai-wip")

    def test_mark_success_updates_labels(self, monitor: IssueMonitor) -> None:
        """mark_success が正しくラベルを更新しコメントを投稿すること。"""
        mock_issue = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        monitor._gh = MagicMock()
        monitor._gh.get_repo.return_value = mock_repo

        monitor.mark_success("owner/repo", 42, "https://github.com/pr/1", 1)

        mock_issue.remove_from_labels.assert_called_once_with("ai-wip")
        mock_issue.add_to_labels.assert_called_once_with("ai-done")
        mock_issue.create_comment.assert_called_once()

    def test_mark_failure_updates_labels(self, monitor: IssueMonitor) -> None:
        """mark_failure が正しくラベルを更新しコメントを投稿すること。"""
        mock_issue = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        monitor._gh = MagicMock()
        monitor._gh.get_repo.return_value = mock_repo

        monitor.mark_failure("owner/repo", 42, "some error")

        mock_issue.remove_from_labels.assert_called_once_with("ai-wip")
        mock_issue.add_to_labels.assert_called_once_with("ai-fail")
