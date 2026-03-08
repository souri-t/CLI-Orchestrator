"""ホスト側の Git 操作モジュール。

サンドボックスコンテナで生成されたコード変更を
ホスト側からリモートリポジトリにプッシュする。
GITHUB_TOKEN はホスト側でのみ使用し、サンドボックスには渡さない。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from orchestrator.logger import get_logger

log = get_logger(__name__)

# トークンをマスクする正規表現パターン (HTTPS URL 中の :token@ をマスク)
_TOKEN_RE = re.compile(r"(https?://[^:]+:)([^@]+)(@)")


def _mask_token(text: str) -> str:
    """URL に含まれるトークンを *** でマスクする。"""
    return _TOKEN_RE.sub(r"\1***\3", text)


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

        gitpython の index.add() は Docker 共有ボリューム上で chmod を試みて
        PermissionError になる場合があるため、git コマンドを直接 subprocess で呼び出す。

        Args:
            repo_full_name: "owner/repo"
            branch_name: プッシュ先ブランチ名
            work_dir: サンドボックスの作業ディレクトリ (ホスト側パス)
            commit_message: コミットメッセージ
        """
        if not work_dir.exists():
            raise GitError(f"作業ディレクトリが存在しません: {work_dir}")

        def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                # safe.directory=* : 異なるユーザーが所有するリポジトリも操作を許可
                # (sandbox が agent ユーザーで clone, orchestrator は root で実行)
                ["git", "-C", str(work_dir), "-c", "safe.directory=*"] + args,
                capture_output=True,
                text=True,
            )
            if check and result.returncode != 0:
                # URL 中のトークンをマスクしてログに出力 (secrets スキャン対策)
                safe_args = [_mask_token(a) for a in args]
                safe_stderr = _mask_token(result.stderr or result.stdout)
                raise GitError(
                    f"git {' '.join(safe_args)} 失敗 (exit {result.returncode}):\n{safe_stderr}"
                )
            return result

        try:
            # fileMode を無効化: uid が違うファイルへの chmod を回避
            _run(["config", "core.fileMode", "false"])

            # ステージング (コンテナ内で git add -A 済みだが念のため)
            _run(["add", "-A"])

            # 変更がなければスキップ
            status = _run(["status", "--porcelain"], check=False)
            diff_cached = _run(["diff", "--cached", "--stat"], check=False)
            if not status.stdout.strip() and not diff_cached.stdout.strip():
                log.warning("nothing_to_commit", branch=branch_name)
                return

            # コミット (author/email を設定)
            _run([
                "-c", "user.name=AI Orchestrator",
                "-c", "user.email=orchestrator@ai-bot",
                "commit", "-m", commit_message,
            ])
            log.info("committed", branch=branch_name, message=commit_message)

            # 認証付き URL でプッシュ
            remote_url = self._build_auth_url(repo_full_name)
            # リモートブランチの追跡情報を事前に取得して stale info を防ぐ
            # (前回の AI 実行でブランチが残っていると --force-with-lease が拒否するため)
            _run(
                ["fetch", remote_url, f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}"],
                check=False,  # ブランチが存在しない場合は無視
            )
            _run(["push", remote_url, f"HEAD:{branch_name}", "--force-with-lease"])
            log.info("pushed", repo=repo_full_name, branch=branch_name)

        except GitError:
            raise
        except Exception as e:
            raise GitError(f"Git 操作中に予期しないエラー: {e}") from e

    def _build_auth_url(self, repo_full_name: str) -> str:
        """PAT を含んだ HTTPS URL を構築する。"""
        return f"https://x-access-token:{self._token}@github.com/{repo_full_name}.git"

    @staticmethod
    def build_commit_message(issue_number: int, title: str) -> str:
        """Conventional Commits 形式のコミットメッセージを生成する。"""
        safe_title = title.replace("\n", " ").strip()
        return f"fix: resolve #{issue_number} - {safe_title}"
