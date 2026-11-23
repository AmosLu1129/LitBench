#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 从 scripts/backup.sh 的备份目录恢复整个部署（数据库 + storage + MCP token 卷）。
#
# 用法：
#   bash scripts/restore.sh .data/backups/deploy-20261115T...Z
#   bash scripts/restore.sh <备份目录> --yes     # 非交互（验收/自动化用）
#
# 语义：这是「整库回滚」——当前数据库会被 DROP 后从 dump 重建，storage 卷会被清空后
# 解包。执行前请确认备份目录正确；脚本会在开头停下来要求输入 yes。
# .env 不在备份里，沿用当前部署的 .env（密码/内部令牌要自己保管）。
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP=${1:-}
shift || true
ASSUME_YES=0
for arg in "$@"; do
    [ "$arg" = "--yes" ] && ASSUME_YES=1
done

if [ -z "$BACKUP" ] || [ ! -f "${BACKUP}/postgres.dump" ]; then
    echo "用法：bash scripts/restore.sh <备份目录> [--yes]" >&2
    echo "备份目录里必须至少有 postgres.dump（scripts/backup.sh 的产物）" >&2
    exit 2
fi

PROJECT=$(awk '/^name:[[:space:]]*/{print $2; exit}' compose.yaml)
PROJECT=${COMPOSE_PROJECT_NAME:-${PROJECT:-litbench}}
PGUSER=${POSTGRES_USER:-litbench}
PGDB=${POSTGRES_DB:-litbench}
DATA_VOL="${PROJECT}_data"
MCP_VOL="${PROJECT}_mcp_state"

if [ "$ASSUME_YES" -ne 1 ]; then
    echo "将把数据库 ${PGDB} 和 storage 卷 ${DATA_VOL} 覆盖为备份：${BACKUP}"
    read -r -p "确认继续？输入 yes：" answer
    [ "$answer" = "yes" ] || { echo "已取消"; exit 1; }
fi

# 记住哪些可选 profile 服务本来在跑（恢复后要一起拉起来，不能悄悄停掉）
SERVICES_RUNNING=$(docker ps --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null || true)
LAN_WAS_RUNNING=0
MCP_WAS_RUNNING=0
grep -qx 'proxy' <<< "$SERVICES_RUNNING" && LAN_WAS_RUNNING=1
grep -qx 'mcp' <<< "$SERVICES_RUNNING" && MCP_WAS_RUNNING=1

echo "== 停止应用容器（保留 postgres）"
docker compose stop api worker web >/dev/null 2>&1 || true
# 如果开了 lan/mcp profile，也一并停掉；不存在的服务不会报错
docker compose --profile lan --profile mcp stop proxy mcp >/dev/null 2>&1 || true

echo "== 恢复 Postgres"
docker compose exec -T postgres psql -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"${PGDB}\" WITH (FORCE);" \
    -c "CREATE DATABASE \"${PGDB}\" OWNER \"${PGUSER}\";"
docker compose exec -T postgres pg_restore -U "$PGUSER" -d "$PGDB" --no-owner \
    < "${BACKUP}/postgres.dump"

API_IMAGE=$(docker compose images -q api 2>/dev/null | head -n1 || true)
API_IMAGE=${API_IMAGE:-"${PROJECT}-api:latest"}

echo "== 恢复 storage 卷（data）"
docker run --rm -i -v "${DATA_VOL}:/data" "$API_IMAGE" \
    sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null || true; tar xzf - -C /data' \
    < "${BACKUP}/storage.tar.gz"

if [ -f "${BACKUP}/mcp-state.tar.gz" ] && docker volume inspect "$MCP_VOL" >/dev/null 2>&1; then
    echo "== 恢复 MCP token/审计卷（mcp_state）"
    docker run --rm -i -u 0 -v "${MCP_VOL}:/data" "$API_IMAGE" \
        sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null || true; tar xzf - -C /data' \
        < "${BACKUP}/mcp-state.tar.gz"
fi

echo "== 启动"
docker compose up -d
[ "$LAN_WAS_RUNNING" = "1" ] && docker compose --profile lan up -d proxy
[ "$MCP_WAS_RUNNING" = "1" ] && docker compose --profile mcp up -d mcp

echo "== 等待 api healthy"
API_ID=$(docker compose ps -q api)
for _ in $(seq 1 60); do
    STATE=$(docker inspect --format '{{.State.Health.Status}}' "$API_ID" 2>/dev/null || echo starting)
    [ "$STATE" = "healthy" ] && { echo "api healthy"; break; }
    sleep 2
done
docker compose ps
echo "恢复完成。若启动失败：docker compose logs api --tail 100"
