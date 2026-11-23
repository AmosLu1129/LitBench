# SPDX-License-Identifier: MIT
"""运行配置。

所有配置都可以用环境变量覆盖，CLI 参数优先于环境变量。默认值面向本机开发：
API 在 ``127.0.0.1:8000``，状态（token / 审计）落在仓库的 ``.data/mcp/``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_STATE_DIR = ".data/mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_RATE_LIMIT = 120
DEFAULT_RATE_WINDOW_S = 60.0
DEFAULT_API_TIMEOUT_S = 20.0
DEFAULT_MAX_TEXT_CHARS = 1200
DEFAULT_MAX_HTML_CHARS = 2000
DEFAULT_MAX_SCAN_PAPERS = 2000
DEFAULT_MAX_BLOCKS_SCAN = 3000
DEFAULT_LOG_LEVEL = "INFO"

TRANSPORTS = ("stdio", "streamable-http")

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """配置不合法（可读错误，CLI 直接打印给用户）。"""


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value is not None else None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前是 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，当前是 {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} 必须是 true/false，当前是 {raw!r}")


@dataclass(frozen=True, slots=True)
class Settings:
    """MCP 薄层的全部运行时配置。"""

    api_base: str = DEFAULT_API_BASE
    # LitBench API 的内部服务令牌（M7）：认证打开后 MCP 用它在请求头
    # X-LitBench-Internal-Token 里以 system 身份调 API；空 = 不带（单机模式）。
    # 该值必须与 API 侧 INTERNAL_API_TOKEN 一致；不要写进仓库/日志。
    api_token: str = ""
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIR))
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    mcp_path: str = DEFAULT_MCP_PATH
    transport: Literal["stdio", "streamable-http"] = "stdio"
    # stdio 传输默认不校验 token；置 True 时要求环境变量 LITBENCH_MCP_TOKEN
    require_token: bool = False
    # 每个 actor（HTTP 下 = token label；stdio 下 = actor 名）的滑动窗口限流
    rate_limit: int = DEFAULT_RATE_LIMIT
    rate_window_s: float = DEFAULT_RATE_WINDOW_S
    api_timeout_s: float = DEFAULT_API_TIMEOUT_S
    # 单条证据/表格返回的最大字符数（超出截断并标 truncated）
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    max_html_chars: int = DEFAULT_MAX_HTML_CHARS
    # 标识符（DOI/arXiv/cite_key）在 q 搜不到时最多扫描多少篇文献
    max_scan_papers: int = DEFAULT_MAX_SCAN_PAPERS
    # cite.resolve 单篇最多扫多少个 block 做词面打分
    max_blocks_scan: int = DEFAULT_MAX_BLOCKS_SCAN
    log_level: str = DEFAULT_LOG_LEVEL

    def __post_init__(self) -> None:
        api_base = (self.api_base or "").strip().rstrip("/")
        if not api_base.startswith(("http://", "https://")):
            raise ConfigError(f"API 地址必须以 http:// 或 https:// 开头：{self.api_base!r}")
        object.__setattr__(self, "api_base", api_base)

        # 内部服务令牌会进 HTTP 头：只允许可见 ASCII，防止换行/空格注入或误配；
        # 16 字符起步，部署时用 `secrets.token_urlsafe(32)` 这类随机值。
        api_token = (self.api_token or "").strip()
        if api_token:
            if len(api_token) < 16:
                raise ConfigError("LITBENCH_MCP_API_TOKEN 太短（至少 16 字符）")
            if any(char < "!" or char > "~" for char in api_token):
                raise ConfigError("LITBENCH_MCP_API_TOKEN 只能包含可见 ASCII 字符（不能有空格/换行）")
        object.__setattr__(self, "api_token", api_token)

        if self.transport not in TRANSPORTS:
            raise ConfigError(f"transport 只支持 {' / '.join(TRANSPORTS)}：{self.transport!r}")
        if not (0 < self.port < 65536):
            raise ConfigError(f"端口不合法：{self.port}")
        path = self.mcp_path or DEFAULT_MCP_PATH
        if not path.startswith("/"):
            path = "/" + path
        object.__setattr__(self, "mcp_path", path.rstrip("/") or "/mcp")

        if self.rate_limit < 1:
            raise ConfigError(f"限流次数必须 ≥ 1：{self.rate_limit}")
        if self.rate_window_s <= 0:
            raise ConfigError(f"限流窗口必须 > 0 秒：{self.rate_window_s}")
        if self.api_timeout_s <= 0:
            raise ConfigError(f"API 超时必须 > 0 秒：{self.api_timeout_s}")
        if self.max_text_chars < 80:
            raise ConfigError(f"max_text_chars 太小（< 80）：{self.max_text_chars}")

        state_dir = Path(self.state_dir)
        if not state_dir.is_absolute():
            state_dir = (Path.cwd() / state_dir).resolve()
        object.__setattr__(self, "state_dir", state_dir)

    @property
    def tokens_path(self) -> Path:
        return self.state_dir / "tokens.json"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def http_base_url(self) -> str:
        """HTTP 传输对外暴露的基 URL（也是 MCP OAuth 元数据的资源标识）。"""
        host = self.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def http_mcp_url(self) -> str:
        return f"{self.http_base_url}{self.mcp_path}"

    def with_overrides(self, **overrides: Any) -> Settings:
        """返回替换过部分字段的新配置（None 值忽略，方便直接接 argparse 结果）。"""
        clean = {k: v for k, v in overrides.items() if v is not None}
        if not clean:
            return self
        return replace(self, **clean)

    @classmethod
    def from_env(cls) -> Settings:
        """从环境变量构造配置（CLI 的起点）。

        环境变量：

        - ``LITBENCH_MCP_API``：LitBench API 基地址
        - ``LITBENCH_MCP_API_TOKEN``：LitBench 内部服务令牌（认证打开时用；与 API 的
          ``INTERNAL_API_TOKEN`` 一致）
        - ``LITBENCH_MCP_STATE_DIR``：token/审计目录（默认 ``.data/mcp``）
        - ``LITBENCH_MCP_HOST`` / ``LITBENCH_MCP_PORT`` / ``LITBENCH_MCP_PATH``
        - ``LITBENCH_MCP_TRANSPORT``：``stdio`` / ``streamable-http``
        - ``LITBENCH_MCP_REQUIRE_TOKEN``：stdio 是否强制 Bearer token
        - ``LITBENCH_MCP_RATE_LIMIT`` / ``LITBENCH_MCP_RATE_WINDOW_SECONDS``
        - ``LITBENCH_MCP_API_TIMEOUT_S`` / ``LITBENCH_MCP_MAX_TEXT_CHARS``
        - ``LITBENCH_MCP_MAX_HTML_CHARS`` / ``LITBENCH_MCP_MAX_SCAN_PAPERS``
        - ``LITBENCH_MCP_LOG_LEVEL``
        """
        transport = (_env("LITBENCH_MCP_TRANSPORT") or "stdio").lower()
        if transport not in TRANSPORTS:
            raise ConfigError(
                f"LITBENCH_MCP_TRANSPORT 只支持 {' / '.join(TRANSPORTS)}，当前是 {transport!r}"
            )
        return cls(
            api_base=_env("LITBENCH_MCP_API") or DEFAULT_API_BASE,
            api_token=_env("LITBENCH_MCP_API_TOKEN") or "",
            state_dir=Path(_env("LITBENCH_MCP_STATE_DIR") or DEFAULT_STATE_DIR),
            host=_env("LITBENCH_MCP_HOST") or DEFAULT_HOST,
            port=_env_int("LITBENCH_MCP_PORT", DEFAULT_PORT),
            mcp_path=_env("LITBENCH_MCP_PATH") or DEFAULT_MCP_PATH,
            transport=transport,  # type: ignore[arg-type]
            require_token=_env_bool("LITBENCH_MCP_REQUIRE_TOKEN", False),
            rate_limit=_env_int("LITBENCH_MCP_RATE_LIMIT", DEFAULT_RATE_LIMIT),
            rate_window_s=_env_float("LITBENCH_MCP_RATE_WINDOW_SECONDS", DEFAULT_RATE_WINDOW_S),
            api_timeout_s=_env_float("LITBENCH_MCP_API_TIMEOUT_S", DEFAULT_API_TIMEOUT_S),
            max_text_chars=_env_int("LITBENCH_MCP_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS),
            max_html_chars=_env_int("LITBENCH_MCP_MAX_HTML_CHARS", DEFAULT_MAX_HTML_CHARS),
            max_scan_papers=_env_int("LITBENCH_MCP_MAX_SCAN_PAPERS", DEFAULT_MAX_SCAN_PAPERS),
            log_level=(_env("LITBENCH_MCP_LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper(),
        )


__all__ = ["TRANSPORTS", "ConfigError", "Settings"]
