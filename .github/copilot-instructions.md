# Project Guidelines — CLI Orchestrator

GitHub Issue (`ai-task` ラベル) → Docker サンドボックスで OpenCode CLI を自動実行 → Draft PR 作成する自動化パイプライン。

## Code Style

- Python 3.11+、`from __future__ import annotations` を全モジュールで使用
- Ruff: line-length=100, rules `E,F,I,UP` — `ruff check .` / `ruff format .`
- mypy strict モード — `mypy src/`
- docstring・ログメッセージ・Issue コメントは **日本語**
- 構造化ログは structlog: `log = get_logger(__name__)` をモジュールトップに配置
- モジュール固有の例外クラスを定義 (`AgentError`, `SandboxError`, `GitError` 等)
- 設定は Pydantic `BaseModel` / `BaseSettings` で YAML + 環境変数の二層管理 ([src/orchestrator/config.py](src/orchestrator/config.py))

## Architecture

```
src/orchestrator/
├── config.py, logger.py          # 基盤ユーティリティ (全モジュールから参照)
├── main.py                        # Click CLI エントリーポイント
├── trigger.py                     # パイプラインコア (TaskPipeline / Orchestrator)
├── git_ops.py                     # ホスト側 git push
├── github/                        # GitHub 連携
│   ├── issue_monitor.py           #   Issue 監視・ラベル遷移
│   ├── pr_manager.py              #   Draft PR 作成
│   └── webhook_server.py          #   FastAPI Webhook サーバー
└── sandbox/                       # Docker サンドボックス
    ├── sandbox.py                 #   コンテナライフサイクル管理
    └── agent_runner.py            #   OpenCode CLI 実行
```

処理フロー:
```
GitHub Issue → [Polling/Webhook] → Orchestrator.submit_task()
  → TaskPipeline.process():
    1. sandbox.SandboxManager.create() — Docker コンテナ起動
    2. sandbox.AgentRunner.run()       — clone → checkout -b → opencode run → git diff
    3. GitOps.push_changes()           — ホスト側で git push
    4. github.PRManager.create_draft_pr()   — Draft PR 作成
    5. github.IssueMonitor.mark_success/mark_failure()
```

- **ラベル駆動ステートマシン**: `ai-task` → `ai-running` → `ai-done`/`ai-fail` (外部 DB 不要)
- **コア処理は同期**、Webhook/ヘルスチェックのみ async (FastAPI)
- **並行制御**: `ThreadPoolExecutor` + `Semaphore` ([src/orchestrator/trigger.py](src/orchestrator/trigger.py))
- **GITHUB_TOKEN はサンドボックスに渡さない** — push/PR はホスト側のみで実行

## Build and Test

```bash
pip install -e ".[dev]"          # 依存インストール
pytest                           # テスト (asyncio_mode="auto")
ruff check . && ruff format .    # リント & フォーマット
mypy src/                        # 型チェック

# Docker
docker build -t orchestrator-sandbox:latest -f Dockerfile.sandbox .
docker compose build && docker compose up -d

# ユーティリティ
docker compose run --rm orchestrator check-keys   # APIキー検証
docker compose run --rm orchestrator run-once     # 単発実行
```

## Project Conventions

- **git 操作は `subprocess.run()`**: Docker 共有ボリュームで GitPython の `repo.index.add()` が `PermissionError` を起こすため直接 git CLI を使用 ([src/orchestrator/git_ops.py](src/orchestrator/git_ops.py))
- **`safe.directory=*`**: サンドボックス内 (`agent` uid=1000) とホスト側 (`root`) でユーザーが異なるため必須
- **Conventional Commits**: `fix: resolve #N - title` 形式
- **OpenCode CLI**: `opencode run <prompt>` を非対話モードで実行。全ツール自動承認 (`OPENCODE_PERMISSION` 環境変数)  ([src/orchestrator/sandbox/agent_runner.py](src/orchestrator/sandbox/agent_runner.py))
- **サブパッケージ構成**: GitHub 連携は `orchestrator.github.*`、Docker サンドボックスは `orchestrator.sandbox.*` としてインポート
- **テスト**: `unittest.mock` (`MagicMock`, `patch`)、Webhook は `TestClient`、CLI は `CliRunner`。`@patch` のターゲットパスも新サブパッケージ構成に合わせること (例: `orchestrator.github.issue_monitor.Github`)

## Security

- AI APIキーは `sandbox.exec()` の `env` パラメータでコンテナに渡す (ボリュームマウント不可)
- コンテナ隔離: `cap_drop=["ALL"]`, `no-new-privileges`, `pids_limit=256`
- ネットワーク: iptables ホワイトリスト (AI API + GitHub の HTTPS/DNS のみ)
- Webhook: HMAC-SHA256 署名検証 ([src/orchestrator/webhook_server.py](src/orchestrator/webhook_server.py))
- AI 生成コードは必ず Draft PR (人間レビュー必須)、`--force-with-lease` で誤上書き防止
