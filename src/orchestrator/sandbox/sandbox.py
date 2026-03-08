"""Docker サンドボックスのライフサイクル管理モジュール。

サンドボックスコンテナの作成・実行・クリーンアップを管理する。
- OpenCode 設定ファイルを読み取り専用でマウント
- GITHUB_TOKEN はコンテナに渡さない (push はホスト側で実行)
- ネットワークはホワイトリストで制御
"""
from __future__ import annotations

import shutil
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import docker
import docker.errors
from docker.models.containers import Container

from orchestrator.config import AppConfig, SandboxConfig
from orchestrator.logger import get_logger

log = get_logger(__name__)

# コンテナ内の作業ディレクトリ
CONTAINER_WORKSPACE = "/workspace"


class SandboxError(Exception):
    """サンドボックス操作エラー。"""


class SandboxTimeout(SandboxError):
    """サンドボックスタイムアウトエラー。"""


class Sandbox:
    """個々のサンドボックスコンテナを表すクラス。"""

    def __init__(self, container: Container, work_dir: Path, config: SandboxConfig) -> None:
        self._container = container
        self.work_dir = work_dir  # ホスト側の作業ディレクトリ
        self._config = config
        self._timeout_timer: threading.Timer | None = None

    @property
    def container_id(self) -> str:
        return self._container.short_id

    def exec(self, command: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        """コンテナ内でコマンドを実行する。

        Returns:
            (exit_code, output) のタプル
        """
        log.debug("sandbox_exec", container=self.container_id, command=command)
        # /usr/local/bin にインストールされた Linux 用 opencode バイナリを使用するため
        # PATH を明示的に設定 (/usr/local/bin を先頭に)
        base_env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        merged_env = {**base_env, **(env or {})}
        result = self._container.exec_run(
            command,
            workdir=CONTAINER_WORKSPACE,
            environment=merged_env,
            demux=False,
        )
        output = result.output.decode("utf-8", errors="replace") if result.output else ""
        return result.exit_code, output

    def start_timeout_timer(self) -> None:
        """タイムアウトタイマーを開始する。"""
        timeout = self._config.timeout_sec

        def _kill() -> None:
            log.warning(
                "sandbox_timeout",
                container=self.container_id,
                timeout_sec=timeout,
            )
            try:
                self._container.stop(timeout=5)
            except Exception:
                pass

        self._timeout_timer = threading.Timer(timeout, _kill)
        self._timeout_timer.daemon = True
        self._timeout_timer.start()

    def stop_timeout_timer(self) -> None:
        """タイムアウトタイマーを停止する。"""
        if self._timeout_timer and self._timeout_timer.is_alive():
            self._timeout_timer.cancel()

    def cleanup(self) -> None:
        """コンテナと作業ディレクトリを削除する。"""
        self.stop_timeout_timer()

        try:
            self._container.stop(timeout=10)
        except Exception:
            pass

        try:
            self._container.remove(force=True)
            log.debug("container_removed", container=self.container_id)
        except Exception as e:
            log.warning("container_remove_failed", container=self.container_id, error=str(e))

        # 作業ディレクトリを削除
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)


class SandboxManager:
    """サンドボックスコンテナのファクトリ。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._sandbox_config = config.sandbox
        self._client = docker.from_env()

    @contextmanager
    def create(self, task_id: str) -> Generator[Sandbox, None, None]:
        """サンドボックスを作成し、with ブロック終了時にクリーンアップする。

        Usage:
            with manager.create("issue-42") as sandbox:
                sandbox.exec(["git", "clone", ...])
        """
        sandbox = self._start_container(task_id)
        log.info("sandbox_created", container=sandbox.container_id, task=task_id)

        try:
            sandbox.start_timeout_timer()
            yield sandbox
        finally:
            sandbox.cleanup()
            log.info("sandbox_cleaned_up", container=sandbox.container_id, task=task_id)

    def _start_container(self, task_id: str) -> Sandbox:
        """コンテナを起動して Sandbox インスタンスを返す。"""
        cfg = self._sandbox_config

        # ホスト側の作業ディレクトリを作成
        work_dir = Path(cfg.work_dir_host) / task_id
        work_dir.mkdir(parents=True, exist_ok=True)

        # ボリュームマウント設定
        volumes = self._build_volumes(work_dir)

        # 環境変数 (GITHUB_TOKEN は渡さない)
        environment: dict[str, str] = {}

        # 同名コンテナが残留している場合は強制削除 (前回の失敗時の残骸)
        container_name = f"orchestrator-sandbox-{task_id}"
        try:
            old = self._client.containers.get(container_name)
            log.warning("removing_stale_container", name=container_name, id=old.short_id)
            old.remove(force=True)
        except docker.errors.NotFound:
            pass

        # リソース制限
        try:
            container: Container = self._client.containers.run(
                image=cfg.image,
                command="sleep infinity",  # エージェントが exec で利用
                detach=True,
                volumes=volumes,
                environment=environment,
                mem_limit=cfg.memory_limit,
                nano_cpus=int(cfg.cpu_count * 1e9),
                pids_limit=cfg.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                network_mode="bridge",  # iptables でホワイトリスト制御
                name=container_name,
                labels={"orchestrator": "sandbox", "task": task_id},
            )
        except docker.errors.ImageNotFound:
            raise SandboxError(
                f"サンドボックスイメージ '{cfg.image}' が見つかりません。"
                f" `docker build -t {cfg.image} -f Dockerfile.sandbox .` を実行してください。"
            )
        except docker.errors.APIError as e:
            raise SandboxError(f"コンテナ起動失敗: {e}") from e

        sandbox = Sandbox(container=container, work_dir=work_dir, config=cfg)

        # ホワイトリストが設定されている場合はネットワーク制限を適用
        if cfg.allowed_hosts:
            self._apply_network_restrictions(sandbox)

        return sandbox

    def _build_volumes(self, work_dir: Path) -> dict[str, dict[str, str]]:
        """ボリュームマウント設定を構築する。

        OpenCode バイナリはビルド時にインストール済みなのでイメージマウントは不要。
        API キーは exec の環境変数で渡す。
        """
        volumes: dict[str, dict[str, str]] = {
            str(work_dir): {"bind": CONTAINER_WORKSPACE, "mode": "rw"},
        }
        return volumes

    def _apply_network_restrictions(self, sandbox: Sandbox) -> None:
        """iptables でネットワークをホワイトリスト制御する。

        許可ホストの IP を DNS 解決し、それ以外の外部通信を遮断する。
        コンテナ内で iptables を実行するため NET_ADMIN 権限が一時的に必要。
        Note: 本番環境では専用 network namespace + nftables の使用を推奨。
        """
        allowed_hosts = self._sandbox_config.allowed_hosts
        if not allowed_hosts:
            return

        # DNS ルックアップして IP を取得するスクリプトをコンテナ内で実行
        # MVP では HTTPS のみ許可する簡易実装
        script = (
            "set -e\n"
            "# デフォルトの OUTPUT を DROP に設定\n"
            "iptables -P OUTPUT DROP 2>/dev/null || true\n"
            "# ループバックは許可\n"
            "iptables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true\n"
            "# 確立済み接続は許可\n"
            "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true\n"
        )
        for host in allowed_hosts:
            script += f"# {host} への通信を許可\n"
            script += (
                f"for ip in $(getent hosts {host} | awk '{{print $1}}'); do\n"
                f"  iptables -A OUTPUT -d $ip -p tcp --dport 443 -j ACCEPT 2>/dev/null || true\n"
                f"  iptables -A OUTPUT -d $ip -p tcp --dport 80 -j ACCEPT 2>/dev/null || true\n"
                f"done\n"
            )
        # DNS (UDP 53) は許可
        script += "iptables -A OUTPUT -p udp --dport 53 -j ACCEPT 2>/dev/null || true\n"
        script += "iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT 2>/dev/null || true\n"

        exit_code, output = sandbox.exec(["sh", "-c", script])
        if exit_code != 0:
            log.warning(
                "network_restriction_failed",
                container=sandbox.container_id,
                output=output[:500],
                note="iptables が利用できない環境の可能性があります。ネットワーク制限はスキップされます。",
            )
        else:
            log.info(
                "network_restricted",
                container=sandbox.container_id,
                allowed_hosts=allowed_hosts,
            )
