# ============================================================
# Orchestrator 本体のコンテナイメージ
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# システム依存ライブラリ
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# git の設定 (コンテナ内での操作用)
RUN git config --global user.email "orchestrator@ai-bot" && \
    git config --global user.name "AI Orchestrator"

# Python 依存関係のインストール
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir -e .

# ソースコードをコピー
COPY src/ ./src/
COPY prompts/ ./prompts/

# 作業ディレクトリ
RUN mkdir -p /tmp/orchestrator-work

ENTRYPOINT ["orchestrator"]
CMD ["run"]
