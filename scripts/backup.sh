#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 备份 LitBench 部署：Postgres 数据库 + storage 卷（PDF/图片/LLM 配置）+ MCP token 卷。
#
# 用法（在 WSL/Linux 仓库根目录）：
#   bash scripts/backup.sh                       # 默认写到 .data/backups/deploy-<UTC时间>/
#   bash scripts/backup.sh /mnt/nas/litbench-20261115
#
# 产物（可作为恢复单元整体拷走）：
#   postgres.dump      pg_dump -Fc（自定义格式，pg_restore 用）
#   storage.tar.gz     data 卷（/data：pdfs、images、llm-config.json）
#   mcp-state.tar.gz   mcp_state 卷（只有存在时才备份）
#   compose.yaml / git-rev.txt / compose-ps.txt / SHA256SUMS.txt
#
# 不含 .env（里面有密码/内部令牌），恢复后要单独保管/重新放置。
set -euo pipefail

cd "$(dirname "$0")/.."

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=${1:-".data/backups/deploy-${STAMP}"}
mkdir -p "$DEST"

PROJECT=$(awk '/^name:[[:space:]]*/{print $2; exit}' compose.yaml)
PROJECT=${COMPOSE_PROJECT_NAME:-${PROJECT:-litbench}}
PGUSER=${POSTGRES_USER:-litbench}
PGDB=${POSTGRES_DB:-litbench}
DATA_VOL="${PROJECT}_data"
MCP_VOL="${PROJECT}_mcp_state"

echo "== 项目 ${PROJECT} → 备份到 ${DEST}"

echo "== 1/3 Postgres pg_dump -Fc"
docker compose exec -T postgres pg_dump -U "$PGUSER" -d "$PGDB" -Fc > "${DEST}/postgres.dump"

# 用项目自己的 api 镜像跑一次性容器读卷：镜像里一定有 tar，且不必额外拉 busybox
API_IMAGE=$(docker compose images -q api 2>/dev/null | head -n1 || true)
API_IMAGE=${API_IMAGE:-"${PROJECT}-api:latest"}

echo "== 2/3 storage 卷（data）"
docker run --rm -v "${DATA_VOL}:/src:ro" "$API_IMAGE" \
    sh -c 'tar czf - -C /src .' > "${DEST}/storage.tar.gz"

if docker volume inspect "$MCP_VOL" >/dev/null 2>&1; then
    echo "== 3/3 MCP token/审计卷（mcp_state）"
    # 卷里的文件属主是 mcp 用户（uid 10002），这里用 root 读，避免权限问题
    docker run --rm -u 0 -v "${MCP_VOL}:/src:ro" "$API_IMAGE" \
        sh -c 'tar czf - -C /src .' > "${DEST}/mcp-state.tar.gz"
else
    echo "== 3/3 跳过 mcp_state（卷不存在）"
fi

cp compose.yaml "${DEST}/compose.yaml"
git rev-parse HEAD > "${DEST}/git-rev.txt" 2>/dev/null || echo "unknown" > "${DEST}/git-rev.txt"
docker compose ps > "${DEST}/compose-ps.txt" 2>&1 || true
{
    echo "created_utc=${STAMP}"
    echo "project=${PROJECT}"
    echo "postgres=${PGDB}"
    echo "storage_volume=${DATA_VOL}"
} > "${DEST}/MANIFEST.txt"

(
    cd "$DEST"
    find . -maxdepth 1 -type f ! -name 'SHA256SUMS.txt' -print0 \
        | sort -z | xargs -0 sha256sum > SHA256SUMS.txt
)

echo "== 完成："
ls -lh "$DEST"
echo "恢复：bash scripts/restore.sh ${DEST}"
