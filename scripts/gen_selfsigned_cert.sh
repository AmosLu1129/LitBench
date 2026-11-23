#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 生成局域网自签 TLS 证书（测试/内网用；正式环境换内部 CA / Let's Encrypt）。
#
# 用法（在 WSL/Linux 里跑）：
#   bash scripts/gen_selfsigned_cert.sh                      # 只含 localhost/127.0.0.1
#   bash scripts/gen_selfsigned_cert.sh 192.168.1.50 litbench.lan
#
# 产物：.data/certs/server.crt + server.key（.data/ 已在 .gitignore）。
# compose 的 proxy 服务会把 .data/certs 只读挂进 nginx。
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=".data/certs"
mkdir -p "$OUT"

SAN="DNS:localhost,IP:127.0.0.1"
for host in "$@"; do
    if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        SAN="${SAN},IP:${host}"
    else
        SAN="${SAN},DNS:${host}"
    fi
done

openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
    -keyout "$OUT/server.key" \
    -out "$OUT/server.crt" \
    -subj "/CN=litbench-local" \
    -addext "subjectAltName=${SAN}" >/dev/null 2>&1
chmod 600 "$OUT/server.key"

echo "证书已生成：$OUT/server.crt（SAN=${SAN}）"
echo "启动：docker compose --profile lan up -d"
echo "注意：HTTPS 时 .env 里 AUTH_COOKIE_SECURE=true"
