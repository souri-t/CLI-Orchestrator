"""GitHub Pull Request の作成と管理を担当するモジュール。

AI が生成したコードは必ず Draft PR として作成し、
人間によるレビューを必須にする。
"""
from __future__ import annotations

from dataclasses import dataclass

from github import Auth, Github, GithubException

from orchestrator.github.issue_monitor import IssueTask
from orchestrator.logger import get_logger

log = get_logger(__name__)

PR_BODY_TEMPLATE = """\
## 概要

このPRはAI Orchestratorによって自動生成されました。

**関連Issue**: Closes #{issue_number}

---

## 変更内容

{change_summary}

---

> ⚠️ **注意**: このPRはAIが生成したコードを含みます。
> マージ前に必ず内容を確認してください。
>
> - 使用エージェント: OpenCode
> - 生成日時: {generated_at}
"""


@dataclass
class PRResult:
    """PR 作成結果。"""

    pr_number: int
    pr_url: str
    title: str


class PRManager:
    """Pull Request の作成・管理を担当するクラス。"""

    def __init__(self, github_token: str) -> None:
        self._gh = Github(auth=Auth.Token(github_token))

    def create_draft_pr(
        self,
        task: IssueTask,
        branch_name: str,
        base_branch: str = "main",
        change_summary: str = "(AI による自動生成)",
        draft: bool = True,
    ) -> PRResult:
        """Pull Request を作成する。

        Args:
            task: 対象の Issue タスク
            branch_name: PR のソースブランチ
            base_branch: マージ先ブランチ (デフォルト: main)
            change_summary: 変更内容のサマリ
            draft: True の場合 Draft PR、False の場合通常 PR

        Returns:
            PRResult (PR番号, URL, タイトル)

        Raises:
            GithubException: PR 作成失敗時
        """
        from datetime import datetime, timezone

        repo = self._gh.get_repo(task.repo_full_name)

        # デフォルトブランチを取得
        try:
            base = repo.default_branch
        except Exception:
            base = base_branch

        pr_title = f"fix: #{task.issue_number} {task.title}"
        pr_body = PR_BODY_TEMPLATE.format(
            issue_number=task.issue_number,
            change_summary=change_summary,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )

        try:
            pr = repo.create_pull(
                title=pr_title,
                body=pr_body,
                head=branch_name,
                base=base,
                draft=draft,
            )

            # ai-generated ラベルを付与
            try:
                pr.add_to_labels("ai-generated")
            except GithubException:
                # ラベルが存在しない場合は作成してから付与
                try:
                    repo.create_label(
                        name="ai-generated",
                        color="8A2BE2",
                        description="AI Orchestrator によって生成された PR",
                    )
                    pr.add_to_labels("ai-generated")
                except GithubException as e:
                    log.warning("label_create_failed", error=str(e))

            result = PRResult(
                pr_number=pr.number,
                pr_url=pr.html_url,
                title=pr.title,
            )
            log.info(
                "pr_created",
                repo=task.repo_full_name,
                issue=task.issue_number,
                pr=pr.number,
                url=pr.html_url,
                draft=draft,
            )
            return result

        except GithubException as e:
            log.error(
                "pr_create_failed",
                repo=task.repo_full_name,
                branch=branch_name,
                status=e.status,
                message=str(e.data),
            )
            raise
