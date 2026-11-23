# SPDX-License-Identifier: MIT
"""命令行入口。

    litbench-mcp serve --api http://127.0.0.1:8000 --transport stdio
    litbench-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
    litbench-mcp token issue --scope agent-read --label dsh
    litbench-mcp token list
    litbench-mcp token revoke dsh

约定：stdout 只用于协议/必要输出（stdio 传输时绝不能污染 stdout），
提示信息一律走 stderr。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from . import __version__
from .api import LitBenchAPI
from .auth import (
    READ_SCOPE,
    TokenError,
    TokenStore,
    set_stdio_actor,
)
from .config import TRANSPORTS, ConfigError, Settings
from .server import create_server, run_server

logger = logging.getLogger("litbench_mcp.cli")

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="litbench-mcp",
        description="LitBench 只读 MCP 出口（stdio / Streamable HTTP）",
    )
    parser.add_argument("--version", action="version", version=f"litbench-mcp {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动 MCP server（默认 stdio）")
    serve.add_argument("--api", help="LitBench API 基地址（默认 http://127.0.0.1:8000）")
    serve.add_argument(
        "--transport", choices=TRANSPORTS, help="stdio（默认）或 streamable-http"
    )
    serve.add_argument("--host", help="HTTP 监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, help="HTTP 端口（默认 8765）")
    serve.add_argument("--mcp-path", help="HTTP 路径（默认 /mcp）")
    serve.add_argument("--state-dir", help="token/审计目录（默认 .data/mcp）")
    serve.add_argument(
        "--require-token",
        action="store_true",
        help="stdio 也强制 Bearer token（从 LITBENCH_MCP_TOKEN 读取）",
    )
    serve.add_argument("--rate-limit", type=int, help="限流次数（默认 120）")
    serve.add_argument("--rate-window", type=float, help="限流窗口秒数（默认 60）")
    serve.add_argument("--log-level", choices=_LOG_LEVELS, help="日志级别")

    token = subparsers.add_parser("token", help="管理 token（读/写 scope）")
    token_sub = token.add_subparsers(dest="token_command", required=True)

    issue = token_sub.add_parser("issue", help="签发 token（明文只打印这一次）")
    issue.add_argument("--label", required=True, help="调用方名字，如 dsh / claude（唯一）")
    issue.add_argument(
        "--scope",
        default=READ_SCOPE,
        help=(
            "权限 scope，逗号分隔可多选（默认 agent-read；写侧用 "
            "agent-read,agent-propose 或 agent-read,agent-task）"
        ),
    )
    issue.add_argument("--state-dir", help="token 目录（默认 .data/mcp）")

    list_parser = token_sub.add_parser("list", help="列出已签发 token 的元数据（不含明文/hash）")
    list_parser.add_argument("--state-dir", help="token 目录（默认 .data/mcp）")

    revoke = token_sub.add_parser("revoke", help="按 label 撤销 token")
    revoke.add_argument("label", help="要撤销的 label")
    revoke.add_argument("--state-dir", help="token 目录（默认 .data/mcp）")

    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {
        "api_base": args.api,
        "transport": args.transport,
        "host": args.host,
        "port": args.port,
        "mcp_path": args.mcp_path,
        "state_dir": Path(args.state_dir) if args.state_dir else None,
        "rate_limit": args.rate_limit,
        "rate_window_s": args.rate_window,
        "log_level": args.log_level.upper() if args.log_level else None,
    }
    if args.require_token:
        overrides["require_token"] = True
    return Settings.from_env().with_overrides(**overrides)


def _close_api_client(api: LitBenchAPI) -> None:
    """尽力关闭 httpx client（server.run 结束后通常进程就要退出）。"""
    try:
        asyncio.run(api.aclose())
    except Exception as exc:  # noqa: BLE001 - loop 已关闭等，不影响退出码
        logger.debug("关闭 API 客户端失败（可忽略）：%s", exc)


def _checks_stdio_token(settings: Settings, store: TokenStore) -> None:
    """stdio + --require-token：从环境变量读一次 token 并校验。"""
    if not settings.require_token:
        set_stdio_actor("stdio")
        return
    raw = (os.environ.get("LITBENCH_MCP_TOKEN") or "").strip()
    if not raw:
        raise TokenError(
            "stdio 已开启 --require-token，但没有设置 LITBENCH_MCP_TOKEN；"
            "先运行 `litbench-mcp token issue --scope agent-read --label dsh` 再注入环境变量"
        )
    record = store.verify(raw)
    if record is None:
        raise TokenError("LITBENCH_MCP_TOKEN 无效或已撤销；重新运行 litbench-mcp token issue")
    if READ_SCOPE not in record.scopes:
        raise TokenError(f"token {record.label!r} 缺少 {READ_SCOPE} scope")
    set_stdio_actor(record.label)
    print(f"litbench-mcp: stdio token 校验通过（actor={record.label}）", file=sys.stderr)


def _command_serve(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    _configure_logging(settings.log_level)
    store = TokenStore(settings.tokens_path)
    _checks_stdio_token(settings, store)

    api = LitBenchAPI(
        settings.api_base,
        timeout_s=settings.api_timeout_s,
        api_token=settings.api_token,
    )
    server = create_server(settings, api=api, token_store=store)
    print(
        f"litbench-mcp: transport={settings.transport} api={settings.api_base} "
        f"state_dir={settings.state_dir}",
        file=sys.stderr,
    )
    if settings.transport == "streamable-http":
        print(
            f"litbench-mcp: 监听 {settings.http_mcp_url}（Authorization: Bearer <token>，"
            f"scope={READ_SCOPE}）",
            file=sys.stderr,
        )
    try:
        run_server(server, settings)
    finally:
        _close_api_client(api)
    return 0


def _state_dir_from_args(args: argparse.Namespace) -> Path:
    if getattr(args, "state_dir", None):
        return Settings.from_env().with_overrides(state_dir=Path(args.state_dir)).state_dir
    return Settings.from_env().state_dir


def _command_token(args: argparse.Namespace) -> int:
    store = TokenStore(_state_dir_from_args(args) / "tokens.json")
    action = args.token_command
    if action == "issue":
        issued = store.issue(scope=args.scope, label=args.label)
        if issued.rotated:
            print(
                f"litbench-mcp: label={issued.record.label!r} 已有 token，已轮换（旧值立即失效）",
                file=sys.stderr,
            )
        # 明文只打印这一次，之后文件里只有 sha256
        print(issued.token)
        print(
            f"litbench-mcp: label={issued.record.label} scope={','.join(issued.record.scopes)} "
            f"created_at={issued.record.created_at} 文件={store.path}",
            file=sys.stderr,
        )
        return 0
    if action == "list":
        records = store.list()
        if not records:
            print(f"litbench-mcp: {store.path} 里还没有 token", file=sys.stderr)
            return 0
        for record in records:
            print(f"{record.label}\t{','.join(record.scopes)}\t{record.created_at}")
        return 0
    if action == "revoke":
        removed = store.revoke(args.label)
        print(f"litbench-mcp: 已撤销 label={removed.label}（{store.path}）", file=sys.stderr)
        return 0
    raise ConfigError(f"未知 token 子命令：{action!r}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口；返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return _command_serve(args)
        if args.command == "token":
            return _command_token(args)
    except (ConfigError, TokenError) as exc:
        print(f"litbench-mcp: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - Ctrl+C 正常退出
        with suppress(Exception):
            print("litbench-mcp: 已停止", file=sys.stderr)
        return 130
    parser.error(f"未知命令：{args.command!r}")
    return 2


__all__ = ["build_parser", "main"]
