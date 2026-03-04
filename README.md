# Orchestrator

GitHub Issue に `ai-task` ラベルを付けると、GitHub Copilot CLI が Docker サンドボックス内で自動的にコーディングし、Draft PR を作成する自動化 Orchestrator です。

## アーキテクチャ

### システム全体図

```
┌─────────────────────────────────────────────────────────────────────┐
│                        GitHub Repository                            │
│                                                                     │
│   Issue #42                          Pull Request #43               │
│   [Title] Fix NullPointerException   [Draft] fix: #42 Fix Null...  │
│   [Label] ai-task ──────────────┐    [Label] ai-generated          │
│                                 │    [Body]  Closes #42             │
│                                 │          ▲                        │
└─────────────────────────────────┼──────────┼────────────────────────┘
                                  │ poll /   │ git push + PR create
                                  │ webhook  │ (GITHUB_TOKEN)
┌─────────────────────────────────▼──────────┼────────────────────────┐
│                     Orchestrator (Host)     │                        │
│                                            │                        │
│  ┌──────────────┐   ┌───────────────────┐  │                        │
│  │ Issue        │   │ Task Pipeline     │  │                        │
│  │ Monitor      │──▶│                   │  │                        │
│  │              │   │  1. SandboxManager│  │                        │
│  │ (Polling 5m  │   │  2. AgentRunner   │  │                        │
│  │  or Webhook) │   │  3. GitOps ───────┼──┘                        │
│  └──────────────┘   │  4. PRManager     │                           │
│                     └────────┬──────────┘                           │
│  ┌──────────────┐            │ docker run                           │
│  │ State        │            │ (Docker Socket Mount)                │
│  │ (GitHub      │            │                                      │
│  │  Labels)     │            │                                      │
│  └──────────────┘            │                                      │
└─────────────────────────────┬┼──────────────────────────────────────┘
                              ││
              ┌───────────────▼▼──────────────────────────────────┐
              │         Docker Sandbox Container                   │
              │                                                    │
              │  User: agent (uid=1000, non-root)                  │
              │  Limits: mem=4G, cpu=2, pids=256, timeout=30min    │
              │                                                    │
              │  Mounts (read-only):                               │
              │    ~/.copilot/              → /home/agent/.copilot/│
              │    ~/.config/github-copilot → /home/agent/.config/ │
              │                                                    │
              │  ┌──────────────────────────────────────────────┐  │
              │  │  /workspace  (git clone されたリポジトリ)    │  │
              │  │                                              │  │
              │  │  $ copilot -p "{issue内容}" \               │  │
              │  │      --autopilot --yolo \                    │  │
              │  │      --max-autopilot-continues 20            │  │
              │  │                                              │  │
              │  │  ※ GITHUB_TOKEN なし (git push 不可)        │  │
              │  └──────────────────────────────────────────────┘  │
              │                                                    │
              │  Network: Copilot API エンドポイントのみ許可        │
              │    ✅ github.com / api.github.com                  │
              │    ✅ copilot-proxy.githubusercontent.com          │
              │    ❌ その他の外部通信                              │
              └────────────────────────────────────────────────────┘
```

### 処理フロー

```
          GitHub Issue                    Orchestrator                Docker Sandbox
               │                               │                           │
               │  1. ユーザーが Issue 作成      │                           │
               │     + ai-task ラベル付与       │                           │
               │                               │                           │
               │  2. poll / Webhook で検出 ──▶ │                           │
               │                               │  3. ラベル遷移             │
               │◀──────────────────────────── │     ai-task → ai-wip      │
               │  「作業開始」コメント投稿       │                           │
               │                               │  4. コンテナ起動 ─────────▶│
               │                               │     ~/.copilot/ マウント   │ 5. git clone
               │                               │                           │ 6. git checkout -b ai/issue-42-*
               │                               │                           │ 7. copilot --autopilot
               │                               │                           │    (コーディング実行)
               │                               │◀────────────────────────  │ 8. git diff 返却
               │                               │  9. コンテナ破棄           │
               │                               │ 10. ホスト側で git push    │
               │                               │     (GITHUB_TOKEN 使用)   │
               │                               │ 11. Draft PR 作成         │
               │◀──────────────────────────── │                           │
               │  ai-wip → ai-done             │                           │
               │  PR リンクコメント投稿         │                           │
```

### 状態遷移（GitHub Issue ラベル）

```
   ユーザーが付与
        │
        ▼
   ┌─────────┐    Orchestrator 検出     ┌─────────┐
   │ ai-task │ ──────────────────────▶ │ ai-wip  │
   └─────────┘                         └────┬────┘
                                            │
                        ┌───────────────────┤
                        │                   │
                   成功  ▼              失敗  ▼
                ┌──────────┐        ┌──────────┐
                │ ai-done  │        │ ai-fail  │
                │ (PR作成) │        │ (エラー) │
                └──────────┘        └────┬─────┘
                                         │
                              ai-task を再付与で再処理可能
```

### コンポーネント構成

| コンポーネント | ファイル | 役割 |
|---|---|---|
| Issue Monitor | `issue_monitor.py` | Issue のポーリング・ラベル遷移・コメント投稿 |
| Sandbox Manager | `sandbox.py` | Docker コンテナのライフサイクル管理 |
| Agent Runner | `agent_runner.py` | `copilot --autopilot` 実行・プロンプト構築 |
| Git Ops | `git_ops.py` | ホスト側での `git push` (GITHUB_TOKEN 使用) |
| PR Manager | `pr_manager.py` | Draft PR 作成・ラベル付与 |
| Task Pipeline | `trigger.py` | 上記を結合するパイプライン・並行実行制御 |
| Webhook Server | `webhook_server.py` | FastAPI で GitHub Webhook を受信 |
| CLI | `main.py` | `orchestrator run/run-once/status` コマンド |



---

## 仕組み（概要）

```
Issue (ai-task) → Orchestrator 検出 → Docker サンドボックス起動
→ copilot --autopilot 実行 → ホスト側で git push → Draft PR 作成
```

Issue のラベルが状態管理を兼ねます：

| ラベル | 意味 |
|---|---|
| `ai-task` | 処理対象（このラベルを付けると処理開始） |
| `ai-wip` | 処理中 |
| `ai-done` | 完了（Draft PR 作成済み） |
| `ai-fail` | 失敗（コメントにエラー内容を記載） |

## セキュリティ設計

- **サンドボックス分離**: コーディングは Docker コンテナ内でのみ実行
- **GITHUB_TOKEN はコンテナに渡さない**: `git push` と PR 作成はホスト側で実行
- **Copilot 認証**: `~/.copilot/` と `~/.config/github-copilot/` を読み取り専用マウント
- **ネットワーク制限**: Copilot API エンドポイントのみ許可（設定可能）
- **リソース制限**: メモリ 4GB、CPU 2コア、PID 256、タイムアウト 30分
- **必ず Draft PR**: AI 生成コードは常に Draft として作成。人間のレビューが必須

## 必要なもの

- Docker Desktop (または Docker Engine)
- GitHub アカウント + Personal Access Token (`GITHUB_TOKEN`)
- GitHub Copilot サブスクリプション
- `~/.copilot/` または `~/.config/github-copilot/` に Copilot 認証情報

## セットアップ

### 1. サンドボックスイメージをビルド

```bash
docker build -t orchestrator-sandbox:latest -f Dockerfile.sandbox .
```

### 2. 設定ファイルを作成

```bash
cp config.example.yaml config.yaml
# config.yaml を編集してリポジトリを設定
```

```yaml
repositories:
  - "your-org/your-repo"

mode: "polling"  # または "webhook"
```

### 3. 環境変数を設定

```bash
export GITHUB_TOKEN="ghp_your_personal_access_token"
```

`.env` ファイルも使用可能：

```env
GITHUB_TOKEN=ghp_your_personal_access_token
WEBHOOK_SECRET=your_webhook_secret  # webhook モードのみ
```

### 4. 起動

**Docker Compose で起動（推奨）:**

```bash
GITHUB_TOKEN=ghp_xxx docker compose up -d
```

**直接実行（開発時）:**

```bash
pip install -e .
orchestrator run
```

## 使い方

### Issue を処理させる

監視対象リポジトリで Issue を作成し、`ai-task` ラベルを付けます。

```
タイトル: Fix the null pointer exception in UserService
本文:
When a user logs in with an invalid email format,
the application throws a NullPointerException at UserService.java:42.
Expected: proper validation error message.
```

Orchestrator が検出すると：
1. ラベルが `ai-task` → `ai-wip` に変わる
2. Issue に「🤖 作業を開始しました」コメントが投稿される
3. Docker サンドボックスで `copilot --autopilot` が実行される
4. 変更がブランチ `ai/issue-{number}-{slug}` にプッシュされる
5. Draft PR が作成され、Issue に `ai-done` ラベルが付く

失敗した場合は `ai-fail` ラベルと、エラー内容のコメントが投稿されます。`ai-task` ラベルを再付与すると再処理されます。

### ステータス確認

```bash
orchestrator status
```

### 1回だけ実行（デバッグ用）

```bash
orchestrator run-once
```

## 設定リファレンス

主な設定項目（詳細は `config.example.yaml` 参照）：

| 設定 | デフォルト | 説明 |
|---|---|---|
| `mode` | `polling` | `polling` または `webhook` |
| `polling.interval_sec` | `300` | ポーリング間隔（秒） |
| `webhook.port` | `8080` | Webhook サーバーポート |
| `sandbox.timeout_sec` | `1800` | コンテナタイムアウト（秒） |
| `sandbox.memory_limit` | `4g` | コンテナメモリ上限 |
| `copilot.max_autopilot_continues` | `20` | Copilot の最大自律ステップ数 |

## Webhook モードのセットアップ

1. `config.yaml` で `mode: "webhook"` に設定
2. `WEBHOOK_SECRET` 環境変数を設定
3. サーバーを外部公開（ngrok / リバースプロキシなど）
4. GitHub リポジトリの Settings > Webhooks で追加：
   - Payload URL: `https://your-server/webhook`
   - Content type: `application/json`
   - Secret: `WEBHOOK_SECRET` と同じ値
   - Events: `Issues` のみ選択

## ログ確認

```bash
# Docker Compose の場合
docker compose logs -f orchestrator

# 直接実行の場合
orchestrator --log-level DEBUG run
```

## テスト実行

```bash
pip install -e ".[dev]"
pytest
```

## プロジェクト構成

```
orchestrator/
├── src/orchestrator/
│   ├── main.py              # CLI エントリーポイント
│   ├── config.py            # 設定管理
│   ├── issue_monitor.py     # Issue 監視・ラベル遷移
│   ├── sandbox.py           # Docker サンドボックス管理
│   ├── agent_runner.py      # Copilot CLI 実行
│   ├── git_ops.py           # ホスト側 Git 操作
│   ├── pr_manager.py        # Draft PR 作成
│   ├── trigger.py           # パイプラインコア
│   └── webhook_server.py    # Webhook サーバー
├── prompts/
│   └── default_system.md    # Copilot へのシステムプロンプト
├── tests/
├── Dockerfile               # Orchestrator 本体イメージ
├── Dockerfile.sandbox       # サンドボックスイメージ
├── docker-compose.yml
└── config.example.yaml
```
