# SPDX-License-Identifier: AGPL-3.0-or-later
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 模型权重缓存目录，由 compose 挂卷复用，避免每次重建都重新下载
    HF_HOME=/models

WORKDIR /app

# 增强档（Docling + torch，约 +2GB）默认不装，构建时用 --build-arg INSTALL_DOCLING=1 打开
ARG INSTALL_DOCLING=0

COPY server/requirements.txt ./requirements.txt
COPY server/requirements-docling.txt ./requirements-docling.txt
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_DOCLING" = "1" ]; then pip install -r requirements-docling.txt; fi

# M8：CSL 引用渲染 / docx 导出用的 pandoc（GPL-2.0，只作子进程调用，不链接）。
# 固定版本 + SHA-256（与 scripts/get_pandoc.py 的开发机版本一致）：Debian 自带的
# pandoc 太旧，CSL 输出会和开发机/验收结果不一致。支持 amd64 / arm64。
# GitHub 直连在部分网络会 SSL EOF/卡住：先走 ghproxy/ghfast 镜像，最后才直连；
# 每个下载都校验上面的 SHA-256，镜像只负责传输、不能改变内容。
ARG PANDOC_VERSION=3.14
ARG PANDOC_SHA256_AMD64=c5f4720cdefb3f7bdc5d07202ef579feef0d1370773c873804dd8fdaa8c4f1b8
ARG PANDOC_SHA256_ARM64=c692f3d78a415741bbbac4cdbbbe26fa7edca1f43d2301b982fbf4009cc8cf7f
RUN set -eux; \
    arch="${TARGETARCH:-$(uname -m)}"; \
    case "$arch" in \
      amd64|x86_64) arch=amd64; sha="$PANDOC_SHA256_AMD64" ;; \
      arm64|aarch64) arch=arm64; sha="$PANDOC_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    url="https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-${arch}.tar.gz"; \
    ok=0; \
    for base in "https://ghproxy.net/" "https://ghfast.top/" ""; do \
      if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 10 --max-time 120 -o /tmp/pandoc.tar.gz "${base}${url}" \
         && echo "${sha}  /tmp/pandoc.tar.gz" | sha256sum -c -; then ok=1; break; fi; \
      rm -f /tmp/pandoc.tar.gz; \
    done; \
    [ "$ok" = "1" ] || { echo "pandoc download failed" >&2; exit 1; }; \
    tar -xzf /tmp/pandoc.tar.gz -C /opt; \
    ln -s "/opt/pandoc-${PANDOC_VERSION}/bin/pandoc" /usr/local/bin/pandoc; \
    pandoc --version | head -n 1; \
    rm -f /tmp/pandoc.tar.gz; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

# M8 PDF 导出：typst（MIT，单文件子进程，pandoc 按 PATH 找它）+ Noto CJK 字体
# （PDF 里的中文字形来自它；不装 TeX，API 进程也就不用面对 MiKTeX 权限问题）。
ARG TYPST_VERSION=0.15.1
ARG TYPST_SHA256_AMD64=a6d077d0a95eed5a2eba715b2dae06be954f624ccbf85758a03f389ded33118c
ARG TYPST_SHA256_ARM64=5aa8d74a3d906e60ea12a66ac2f37f8eef1b14cbad7182a745e393a10c23dcee
RUN set -eux; \
    arch="${TARGETARCH:-$(uname -m)}"; \
    case "$arch" in \
      amd64|x86_64) triple="x86_64-unknown-linux-musl"; sha="$TYPST_SHA256_AMD64" ;; \
      arm64|aarch64) triple="aarch64-unknown-linux-musl"; sha="$TYPST_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl fonts-noto-cjk; \
    url="https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-${triple}.tar.xz"; \
    ok=0; \
    for base in "https://ghproxy.net/" "https://ghfast.top/" ""; do \
      if curl -fsSL --retry 2 --retry-delay 2 --retry-all-errors --connect-timeout 10 --max-time 120 -o /tmp/typst.tar.xz "${base}${url}" \
         && echo "${sha}  /tmp/typst.tar.xz" | sha256sum -c -; then ok=1; break; fi; \
      rm -f /tmp/typst.tar.xz; \
    done; \
    [ "$ok" = "1" ] || { echo "typst download failed" >&2; exit 1; }; \
    tar -xJf /tmp/typst.tar.xz -C /opt; \
    cp "$(find /opt -maxdepth 2 -type f -name typst | head -n 1)" /usr/local/bin/typst; \
    chmod +x /usr/local/bin/typst; \
    typst --version; \
    rm -f /tmp/typst.tar.xz; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

COPY server/ ./

# 非 root 运行；/data 是持久卷，/models 是模型缓存卷。
# entrypoint 要能 exec：Windows/检出树上没有可执行位，镜像里显式 chmod。
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data /models \
 && chmod +x /app/docker-entrypoint.sh \
 && chown -R app:app /app /data /models
USER app

EXPOSE 8000

# 就绪探针查一次库（/health 不碰库，DB 挂了也 200，不适合做容器健康判定）。
# worker 继承同一个 HEALTHCHECK 但没监听 8000，由 compose 单独 disable。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=3).status==200 else 1)"

# api 容器启动前执行迁移（compose 里 RUN_MIGRATIONS=1）；worker 传 0 只 exec 主进程。
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "litbench.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
