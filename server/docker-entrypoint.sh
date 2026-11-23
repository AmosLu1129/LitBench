#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 容器入口：api 容器先跑数据库迁移，再 exec 真正的主进程。
#
# 为什么要 entrypoint 而不是在 CMD 里拼命令：
#   * Dockerfile 是 api 和 worker 共用的镜像，worker 的 compose command 是
#     `python -m litbench.workers.run`，作为参数传给 entrypoint；
#   * worker 不跑迁移（RUN_MIGRATIONS=0），靠 compose 里
#     `depends_on: api: condition: service_healthy` 等 api 迁移完 + /api/ready
#     通过后再启动，避免两个容器并发 `alembic upgrade head`。
#
# 默认不开迁移：`docker run <image>` 或本地调试时不应悄悄改库。
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    echo "[entrypoint] RUN_MIGRATIONS=1，先执行 alembic upgrade head"
    alembic upgrade head
    echo "[entrypoint] 迁移完成，启动主进程"
fi

exec "$@"
