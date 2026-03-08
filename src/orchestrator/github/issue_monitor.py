"""GitHub Issue の監視とラベル遷移を管理するモジュール。

ai-task ラベル付きの Issue を検出し、ラベルを ai-running に遷移させることで
冪等性（同じ Issue の重複処理防止）を担保する。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from github import Auth, Github, GithubException
from github.Issue import Issue
from github.Repository import Repository

from orchestrator.config import AppConfig, LabelsConfig
from orchestrator.logger import get_logger

log = get_logger(__name__)


@dataclass
class IssueTask:
    """処理対象の Issue タスク。"""

    repo_full_name: str      # "owner/repo"
    issue_number: int
    title: str
    body: str
    labels: list[str]
    comments: list[str] = field(default_factory=list)
    html_url: str = ""

    def to_prompt_context(self) -> str:
        """プロンプト生成用の Issue コンテキスト文字列を返す。"""
        comments_text = ""
        if self.comments:
            comments_text = "\n\n## Comments\n" + "\n---\n".join(self.comments)

        return (
            f"## Issue #{self.issue_number}: {self.title}\n\n"
            f"**Repository**: {self.repo_full_name}\n"
            f"**Labels**: {', '.join(self.labels)}\n"
            f"**URL**: {self.html_url}\n\n"
            f"## Description\n\n{self.body or '(no description provided)'}"
            f"{comments_text}"
        )


class IssueMonitor:
    """GitHub Issue を監視し、処理対象の Issue を取得する。"""

    def __init__(self, github_token: str, config: AppConfig) -> None:
        self._gh = Github(auth=Auth.Token(github_token))
        self._config = config
        self._labels: LabelsConfig = config.labels

    def fetch_pending_issues(self) -> list[IssueTask]:
        """全リポジトリから ai-task ラベル付きの未処理 Issue を取得する。

        取得と同時にラベルを ai-running に変更することで重複処理を防ぐ。
        """
        tasks: list[IssueTask] = []

        for repo_name in self._config.repositories:
            try:
                repo = self._gh.get_repo(repo_name)
                new_tasks = self._fetch_from_repo(repo)
                tasks.extend(new_tasks)
            except GithubException as e:
                log.error(
                    "repo_fetch_failed",
                    repo=repo_name,
                    status=e.status,
                    message=str(e.data),
                )

        return tasks

    def _fetch_from_repo(self, repo: Repository) -> list[IssueTask]:
        """単一リポジトリから Issue を取得する。"""
        tasks: list[IssueTask] = []

        issues = repo.get_issues(
            state="open",
            labels=[self._labels.trigger],
        )

        for issue in issues:
            task = self._transition_to_running(repo, issue)
            if task is not None:
                tasks.append(task)

        return tasks

    def _transition_to_running(self, repo: Repository, issue: Issue) -> IssueTask | None:
        """Issue のラベルを ai-task → ai-running に遷移させる。

        遷移に成功した場合のみ IssueTask を返す。
        すでに ai-running / ai-done / ai-fail の場合はスキップ。
        """
        current_label_names = [lbl.name for lbl in issue.labels]

        # 既に処理中または完了済みの場合はスキップ
        skip_labels = {self._labels.running, self._labels.done, self._labels.fail}
        if skip_labels.intersection(current_label_names):
            return None

        # ai-task ラベルが実際に付いているか確認 (競合状態対策)
        if self._labels.trigger not in current_label_names:
            return None

        try:
            # ラベル遷移: ai-task を削除 → ai-running を追加
            issue.remove_from_labels(self._labels.trigger)
            issue.add_to_labels(self._labels.running)

            log.info(
                "issue_transitioned_to_running",
                repo=repo.full_name,
                issue=issue.number,
                title=issue.title,
            )

            # Issue にコメントを投稿
            issue.create_comment(
                "🤖 **AI Orchestrator** がこの Issue の作業を開始しました。\n\n"
                f"タスクが完了次第、Draft PR を作成します。\n"
                f"_ラベル `{self._labels.running}` が付いている間は処理中です。_"
            )

            # コメント一覧を取得
            comments = [
                c.body
                for c in issue.get_comments()
                if not c.body.startswith("🤖 **AI Orchestrator**")
            ]

            return IssueTask(
                repo_full_name=repo.full_name,
                issue_number=issue.number,
                title=issue.title,
                body=issue.body or "",
                labels=current_label_names,
                comments=comments,
                html_url=issue.html_url,
            )

        except GithubException as e:
            log.error(
                "label_transition_failed",
                repo=repo.full_name,
                issue=issue.number,
                error=str(e),
            )
            return None

    def mark_success(
        self,
        repo_full_name: str,
        issue_number: int,
        pr_url: str,
        pr_number: int,
    ) -> None:
        """Issue を ai-done にマークし、PR リンクをコメント投稿する。"""
        try:
            repo = self._gh.get_repo(repo_full_name)
            issue = repo.get_issue(issue_number)
            issue.remove_from_labels(self._labels.running)
            issue.add_to_labels(self._labels.done)
            issue.create_comment(
                f"✅ **AI Orchestrator** が作業を完了しました！\n\n"
                f"**Draft PR**: {pr_url} (#{pr_number})\n\n"
                f"内容を確認してレビュー・マージしてください。"
            )
            log.info("issue_marked_done", repo=repo_full_name, issue=issue_number, pr=pr_url)
        except GithubException as e:
            log.error("mark_success_failed", repo=repo_full_name, issue=issue_number, error=str(e))

    def mark_failure(
        self,
        repo_full_name: str,
        issue_number: int,
        error_message: str,
    ) -> None:
        """Issue を ai-fail にマークし、エラー内容をコメント投稿する。"""
        try:
            repo = self._gh.get_repo(repo_full_name)
            issue = repo.get_issue(issue_number)
            issue.remove_from_labels(self._labels.running)
            issue.add_to_labels(self._labels.fail)
            issue.create_comment(
                f"❌ **AI Orchestrator** の処理が失敗しました。\n\n"
                f"**エラー内容:**\n```\n{error_message[:2000]}\n```\n\n"
                f"手動で対応するか、`{self._labels.trigger}` ラベルを再付与して再試行してください。"
            )
            log.warning("issue_marked_fail", repo=repo_full_name, issue=issue_number)
        except GithubException as e:
            log.error("mark_failure_failed", repo=repo_full_name, issue=issue_number, error=str(e))
