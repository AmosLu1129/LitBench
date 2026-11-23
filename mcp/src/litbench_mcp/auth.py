# SPDX-License-Identifier: MIT
"""Token / scope / 限流。

安全模型（M4 read-only）：

* 明文 token 只在 ``litbench-mcp token issue`` 时打印一次，落盘只存 SHA-256；
* 文件里只有 ``label / scope / created_at / token_sha256``，没有 id、没有原值；
* HTTP 传输由 MCP SDK 的 Bearer 认证中间件校验 token 与 scope（401/403），
  本模块实现 SDK 的 ``TokenVerifier`` 协议；
* stdio 默认不强制 token（actor=stdio）；``--require-token`` 时从
  ``LITBENCH_MCP_TOKEN`` 读一次并校验；
* 每个 actor 一个滑动窗口限流器（默认 120 次 / 60 秒），超限抛 MCP 错误
  ``rate_limited``（JSON-RPC error，而不是静默丢弃）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.shared.exceptions import MCPError

TOKEN_PREFIX = "lbmcp_"
TOKEN_BYTES = 32  # token_urlsafe(32) ≈ 43 字符

# 已知 scope。M4 只读批只要求 agent-read；M5 起新增两个写 scope：
#   agent-propose = 投递提案（proposal.create/list/get）
#   agent-task    = 领任务 / 心跳 / 完成 / 失败（task.*）
# HTTP 传输仍要求 agent-read（读是基线），写工具再在工具层校验对应写 scope；
# 因此「能写」的 HTTP token 要同时签发 agent-read 和一个写 scope。
KNOWN_SCOPES = ("agent-read", "agent-propose", "agent-task", "human")
READ_SCOPE = "agent-read"
PROPOSE_SCOPE = "agent-propose"
TASK_SCOPE = "agent-task"

# MCP 实现自定义错误码区间 -32000..-32099；限流用 -32010。
RATE_LIMITED_CODE = -32010

_STATE_VERSION = 1


class TokenError(ValueError):
    """token 管理失败（可读错误，CLI 直接打印）。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ensure_private_file(path: Path) -> None:
    """尽力把 token 文件权限收到 0600（Windows 上 chmod 基本是 no-op，失败不算错）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - 平台差异
        pass


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """一条签发记录（不含明文）。"""

    label: str
    scopes: tuple[str, ...]
    created_at: str
    token_sha256: str


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """签发结果：明文只在这次返回里出现。"""

    token: str
    record: TokenRecord
    rotated: bool = False


class TokenStore:
    """``.data/mcp/tokens.json`` 的读写。

    label 唯一：同一个 label 再签发即轮换（旧 token 立刻失效），
    这样 agent 重装/换机器时不需要手工先 revoke。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # verify_token 可能被多个请求并发调用；文件读很小，加锁保证一致。
        self._lock = threading.Lock()

    # -- 文件 ---------------------------------------------------------
    def _load(self) -> list[TokenRecord]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TokenError(f"token 文件损坏（{self.path}）：{exc}") from exc
        if not isinstance(raw, dict):
            raise TokenError(f"token 文件格式不对（{self.path}）：期望 JSON 对象")
        if raw.get("version") != _STATE_VERSION:
            raise TokenError(
                f"token 文件版本不支持（{self.path}）：{raw.get('version')!r}，期望 {_STATE_VERSION}"
            )
        items = raw.get("tokens")
        if not isinstance(items, list):
            raise TokenError(f"token 文件格式不对（{self.path}）：tokens 不是数组")
        records: list[TokenRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            token_hash = str(item.get("token_sha256") or "").strip()
            if not label or not token_hash:
                continue
            scopes = item.get("scopes")
            if not isinstance(scopes, list):
                scopes = [READ_SCOPE]
            records.append(
                TokenRecord(
                    label=label,
                    scopes=tuple(str(scope) for scope in scopes),
                    created_at=str(item.get("created_at") or ""),
                    token_sha256=token_hash,
                )
            )
        return records

    def _save(self, records: list[TokenRecord]) -> None:
        payload = {
            "version": _STATE_VERSION,
            "tokens": [
                {
                    "label": record.label,
                    "scopes": list(record.scopes),
                    "created_at": record.created_at,
                    "token_sha256": record.token_sha256,
                }
                for record in records
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        _ensure_private_file(self.path)

    # -- 管理 ---------------------------------------------------------
    def issue(self, *, scope: str, label: str) -> IssuedToken:
        label = (label or "").strip()
        if not label:
            raise TokenError("label 不能为空（例如 dsh / claude）")
        if len(label) > 64:
            raise TokenError("label 太长（最多 64 字符）")
        # 支持逗号分隔的多 scope：「agent-read,agent-propose」= 可读可投提案
        requested = [item.strip() for item in (scope or "").split(",") if item.strip()]
        if not requested:
            raise TokenError("scope 不能为空")
        invalid = [item for item in requested if item not in KNOWN_SCOPES]
        if invalid:
            raise TokenError(
                f"scope 只支持 {' / '.join(KNOWN_SCOPES)}，收到 {', '.join(invalid)}"
            )
        scopes = tuple(dict.fromkeys(requested))
        with self._lock:
            records = self._load()
            token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
            record = TokenRecord(
                label=label,
                scopes=scopes,
                created_at=_now_iso(),
                token_sha256=_hash_token(token),
            )
            rotated = any(existing.label == label for existing in records)
            records = [existing for existing in records if existing.label != label]
            records.append(record)
            self._save(records)
        return IssuedToken(token=token, record=record, rotated=rotated)

    def list(self) -> list[TokenRecord]:
        with self._lock:
            return self._load()

    def revoke(self, identifier: str) -> TokenRecord:
        """按 label 撤销；返回被撤销的记录（不存在则报错）。"""
        identifier = (identifier or "").strip()
        if not identifier:
            raise TokenError("要撤销的 label 不能为空")
        with self._lock:
            records = self._load()
            target = next((record for record in records if record.label == identifier), None)
            if target is None:
                raise TokenError(f"没有 label 为 {identifier!r} 的 token")
            self._save([record for record in records if record.label != identifier])
        return target

    # -- 校验 ---------------------------------------------------------
    def verify(self, token: str) -> TokenRecord | None:
        """常量时间比对 SHA-256；找不到返回 None。"""
        candidate = (token or "").strip()
        if not candidate:
            return None
        digest = _hash_token(candidate)
        matched: TokenRecord | None = None
        for record in self.list():
            # 用 compare_digest 避免按字节短路泄露 hash 前缀
            if hmac.compare_digest(digest, record.token_sha256):
                matched = record
        return matched

    async def verify_token(self, token: str) -> AccessToken | None:
        """MCP SDK ``TokenVerifier`` 协议实现（Bearer 中间件调用）。"""
        record = self.verify(token)
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record.label,
            scopes=list(record.scopes),
            claims={"mcp_token_label": record.label},
        )


# ---------------------------------------------------------------------------
# actor：HTTP 取 Bearer token 的 label，stdio 用配置的 actor 名
# ---------------------------------------------------------------------------
_stdio_actor = "stdio"


def set_stdio_actor(actor: str) -> None:
    """stdio 模式下把 actor 设为签发 token 的 label（或默认 stdio）。"""
    global _stdio_actor
    _stdio_actor = (actor or "stdio").strip() or "stdio"


def current_actor() -> str:
    """当前调用者：HTTP 传输下是 token label，stdio 下是配置的 actor。"""
    token = get_access_token()
    if token is not None:
        label = (token.claims or {}).get("mcp_token_label") or token.client_id
        if label:
            return str(label)
    return _stdio_actor


def current_scopes() -> frozenset[str] | None:
    """当前调用的 token scope 集合；stdio（没有 AccessToken）返回 None = 不受限。

    写工具用 ``None`` 作为「本机 stdio，视为已授权」：stdio 由用户自己拉起进程，
    和 Web UI 同权限；要收紧就用 ``--require-token`` 或走 HTTP。
    """
    token = get_access_token()
    if token is None:
        return None
    scopes = token.scopes or []
    if isinstance(scopes, str):  # 兼容 SDK 某些版本把 scope 存成空格分隔字符串
        scopes = scopes.split()
    return frozenset(str(scope) for scope in scopes)


def has_scope(scope: str) -> bool:
    """stdio 视为有全部权限；HTTP 按 token 的 scope 判断（human 通吃）。"""
    scopes = current_scopes()
    if scopes is None:
        return True
    return scope in scopes or "human" in scopes


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------
class RateLimitedError(MCPError):
    """超限：以 MCP 错误返回（error.data.error_code == "rate_limited"）。"""

    def __init__(self, actor: str, *, limit: int, window_s: float, retry_after_s: float) -> None:
        self.actor = actor
        self.limit = limit
        self.window_s = window_s
        self.retry_after_s = round(max(retry_after_s, 0.0), 1)
        super().__init__(
            code=RATE_LIMITED_CODE,
            message=(
                f"rate_limited: actor={actor} 每 {window_s:g} 秒最多 {limit} 次工具调用；"
                f"请 {self.retry_after_s:g} 秒后重试"
            ),
            data={
                "error_code": "rate_limited",
                "actor": actor,
                "limit": limit,
                "window_s": window_s,
                "retry_after_s": self.retry_after_s,
            },
        )


class SlidingWindowRateLimiter:
    """每 actor 一个滑动窗口（单调时钟），窗口内保留最近 ``limit`` 次调用时间。"""

    def __init__(
        self,
        *,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, actor: str) -> None:
        """记一次调用；超限抛 :class:`RateLimitedError`（不记入）。"""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(actor, deque())
            cutoff = now - self.window_s
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry_after = events[0] + self.window_s - now
                raise RateLimitedError(
                    actor,
                    limit=self.limit,
                    window_s=self.window_s,
                    retry_after_s=retry_after,
                )
            events.append(now)

    def remaining(self, actor: str) -> int:
        now = self._clock()
        with self._lock:
            events = self._events.get(actor)
            if not events:
                return self.limit
            cutoff = now - self.window_s
            while events and events[0] <= cutoff:
                events.popleft()
            return max(0, self.limit - len(events))


__all__ = [
    "KNOWN_SCOPES",
    "PROPOSE_SCOPE",
    "RATE_LIMITED_CODE",
    "READ_SCOPE",
    "TASK_SCOPE",
    "TOKEN_PREFIX",
    "IssuedToken",
    "RateLimitedError",
    "SlidingWindowRateLimiter",
    "TokenError",
    "TokenRecord",
    "TokenStore",
    "current_actor",
    "current_scopes",
    "has_scope",
    "set_stdio_actor",
]
