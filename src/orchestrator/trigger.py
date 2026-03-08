"""Issue タスクを処理するパイプラインのコアモジュール。

polling / webhook の両モードに共通の処理ロジックを提供する。
"""
from __future__ import annotations

import concurrent.futures
import threading

from orchestrator.agent_runner import AgentError, AgentRunner
from orchestrator.config import AppConfig
from orchestrator.git_ops import GitError, GitOps
from orchestrator.issue_monitor import IssueMonitor, IssueTask
from orchestrator.logger import get_logger
from orchestrator.pr_manager import PRManager
from orchestrator.sandbox import SandboxError, SandboxManager, SandboxTimeout

log = get_logger(__name__)


class TaskPipeline:
    """単一の Issue タスクを処理するパイプライン。

    sandbox → agent → git → PR の順で処理する。
    """

    def __init__(
        self,
        config: AppConfig,
        github_token: str,
        sandbox_manager: SandboxManager,
        agent_runner: AgentRunner,
        git_ops: GitOps,
        pr_manager: PRManager,
        issue_monitor: IssueMonitor,
    ) -> None:
        self._config = config
        self._github_token = github_token
        self._sandbox_manager = sandbox_manager
        self._agent_runner = agent_runner
        self._git_ops = git_ops
        self._pr_manager = pr_manager
        self._issue_monitor = issue_monitor

    def process(self, task: IssueTask) -> None:
        """Issue タスクを処理する。

        成功時: ai-done ラベル + PR リンクコメント
        失敗時: ai-fail ラベル + エラーコメント
        """
        task_id = f"{task.repo_full_name.replace('/', '-')}-{task.issue_number}"
        branch_name = AgentRunner.get_branch_name(task)
        repo_url = f"https://github.com/{task.repo_full_name}.git"

        log.info(
            "task_start",
            repo=task.repo_full_name,
            issue=task.issue_number,
            title=task.title,
            branch=branch_name,
        )

        try:
            with self._sandbox_manager.create(task_id) as sandbox:
                # 1. エージェントを実行して差分を取得
                _diff = self._agent_runner.run(sandbox, task, repo_url)

                # 2. ホスト側で push
                commit_msg = GitOps.build_commit_message(task.issue_number, task.title)
                self._git_ops.push_changes(
                    repo_full_name=task.repo_full_name,
                    branch_name=branch_name,
                    work_dir=sandbox.work_dir,
                    commit_message=commit_msg,
                )

            # 3. Draft PR 作成 (サンドボックスの外で実行)
            pr_result = self._pr_manager.create_draft_pr(
                task=task,
                branch_name=branch_name,
            )

            # 4. Issue を ai-done にマーク
            self._issue_monitor.mark_success(
                repo_full_name=task.repo_full_name,
                issue_number=task.issue_number,
                pr_url=pr_result.pr_url,
                pr_number=pr_result.pr_number,
            )

            log.info(
                "task_completed",
                repo=task.repo_full_name,
                issue=task.issue_number,
                pr=pr_result.pr_number,
            )

        except SandboxTimeout as e:
            error_msg = f"タイムアウト ({self._config.sandbox.timeout_sec}秒) で処理を中断しました。"
            log.error("task_timeout", repo=task.repo_full_name, issue=task.issue_number)
            self._issue_monitor.mark_failure(task.repo_full_name, task.issue_number, error_msg)

        except (SandboxError, AgentError, GitError) as e:
            log.error(
                "task_failed",
                repo=task.repo_full_name,
                issue=task.issue_number,
                error=str(e),
            )
            self._issue_monitor.mark_failure(task.repo_full_name, task.issue_number, str(e))

        except Exception as e:
            log.exception(
                "task_unexpected_error",
                repo=task.repo_full_name,
                issue=task.issue_number,
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
            self._issue_monitor.mark_failure(
                task.repo_full_name,
                task.issue_number,
                f"予期しないエラー: {type(e).__name__}: {e}",
            )


class Orchestrator:
    """Orchestrator のコアクラス。

    polling / webhook 両モードで使用される。
    タスクの並行実行を管理する。
    """

    def __init__(self, config: AppConfig, github_token: str) -> None:
        self._config = config
        self._github_token = github_token
        self._semaphore = threading.Semaphore(config.max_concurrent_tasks)

        # 各コンポーネントを初期化
        self._sandbox_manager = SandboxManager(config)
        self._agent_runner = AgentRunner(config)
        self._git_ops = GitOps(github_token)
        self._pr_manager = PRManager(github_token)
        self._issue_monitor = IssueMonitor(github_token, config)
        self._pipeline = TaskPipeline(
            config=config,
            github_token=github_token,
            sandbox_manager=self._sandbox_manager,
            agent_runner=self._agent_runner,
            git_ops=self._git_ops,
            pr_manager=self._pr_manager,
            issue_monitor=self._issue_monitor,
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=config.max_concurrent_tasks,
            thread_name_prefix="orchestrator-worker",
        )

    @property
    def issue_monitor(self) -> IssueMonitor:
        return self._issue_monitor

    def submit_task(self, task: IssueTask) -> None:
        """タスクを非同期で処理キューに投入する。"""
        self._executor.submit(self._run_with_semaphore, task)

    def _run_with_semaphore(self, task: IssueTask) -> None:
        """セマフォで並行数を制限しながらタスクを実行する。"""
        with self._semaphore:
            self._pipeline.process(task)

    def poll_once(self) -> int:
        """全リポジトリを1回ポーリングし、新規タスクを処理キューに投入する。

        Returns:
            投入したタスク数
        """
        tasks = self._issue_monitor.fetch_pending_issues()

        if not tasks:
            log.debug("no_pending_issues")
            return 0

        log.info("tasks_found", count=len(tasks))
        for task in tasks:
            self.submit_task(task)

        return len(tasks)

    def shutdown(self, wait: bool = True) -> None:
        """実行中のタスクが完了するまで待機してシャットダウンする。"""
        log.info("orchestrator_shutting_down", wait=wait)
        self._executor.shutdown(wait=wait)
