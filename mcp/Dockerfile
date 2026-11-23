# SPDX-License-Identifier: MIT
# MCP 出口容器：只装 MIT 薄层（mcp + httpx），不含 AGPL 服务端代码。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY mcp/ ./
RUN pip install .

# 非 root 运行；/mcp-state 先建好并 chown，空命名卷首次挂载会继承属主，
# 这样容器里的 mcp 用户能写 token.json / audit.jsonl。
RUN useradd --create-home --uid 10002 mcp \
    && mkdir -p /mcp-state \
    && chown -R mcp:mcp /app /mcp-state
USER mcp

ENV LITBENCH_MCP_STATE_DIR=/mcp-state
EXPOSE 8765

ENTRYPOINT ["litbench-mcp"]
CMD ["serve"]
