"""ホスト側の Git 操作モジュール。

サンドボックスコンテナで生成されたコード変更を
ホスト側からリモートリポジトリにプッシュする。
GITHUB_TOKEN はホスト側でのみ使用し、サンドボックスには渡さない。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from git import GitCommandError, Repo

from orchestrator.logger import get_logger

log = get_logger(__name__)


class GitError(Exception):
    """Git 操作エラー。"""


class GitOps:
    """ホスト側 Git 操作を担当するクラス。"""

    def __init__(self, github_token: str) -> None:
        self._token = github_token

    def push_changes(
        self,
        repo_full_name: str,
        branch_name: str,
        work_dir: Path,
        commit_message: str,
    ) -> None:
        """サンドボックスの作業ディレクトリからホスト側で変更をプッシュする。

        サンドボックスコンテナの work_dir には clone されたリポジトリと
        copilot が加えた変更が含まれている。
        ここではその差分をコミットして認証付き URL でプッシュする。

        Args:
            repo_full_name: "owner/repo"
            branch_name: プッシュ先ブランチ名
            work_dir: サンドボックスの作業ディレクトリ (ホスト側パス)
            commit_message: コミットメッセージ
        """
        if not work_dir.exists():
            raise GitError(f"作業ディレクトリが存在しません: {work_dir}")

        try:
            repo = Repo(work_dir)
        except Exception as e:
            raise GitError(f"Git リポジトリが見つかりません: {work_dir}\n{e}") from e

        # 認証付きのリモート URL を設定
        remote_url = self._build_auth_url(repo_full_name)

        try:
            # 現在のブランチを確認
            current_branch = repo.active_branch.name
            if current_branch != branch_name:
                log.warning(
                    "branch_mismatch",
                    expected=branch_name,
                    actual=current_branch,
                )

            # ステージングと確認
            repo.index.add(["*"])
            if not repo.index.diff("HEAD") and not repo.untracked_files:
                log.warning("nothing_to_commit", branch=branch_name)
                return

            # コミット
            repo.index.commit(
                commit_message,
                author_date="now",
                commit_date="now",
            )
            log.info("committed", branch=branch_name, message=commit_message)

            # リモートを一時的に認証付き URL で更新してプッシュ
            origin = repo.remote("origin")
            original_url = origin.url
            origin.set_url(remote_url)

            try:
                origin.push(refspec=f"{branch_name}:{branch_name}", force=False)
                log.info("pushed", repo=repo_full_name, branch=branch_name)
            finally:
                # 認証情報を含む URL を元に戻す (作業ディレクトリはこの後削除されるが念のため)
                origin.set_url(original_url)

        except GitCommandError as e:
            raise GitError(f"Git 操作失敗: {e}") from e

    def _build_auth_url(self, repo_full_name: str) -> str:
        """PAT を含んだ HTTPS URL を構築する。"""
        return f"https://x-access-token:{self._token}@github.com/{repo_full_name}.git"

    @staticmethod
    def build_commit_message(issue_number: int, title: str) -> str:
        """Conventional Commits 形式のコミットメッセージを生成する。"""
        safe_title = title.replace("\n", " ").strip()
        return f"fix: resolve #{issue_number} - {safe_title}"
