# SPDX-License-Identifier: MIT
"""审计日志（JSONL）。

每次工具调用追加一行::

    {"ts": "...", "actor": "dsh", "tool": "library.search",
     "params_digest": "<sha256>", "params_preview": "…≤80 字…",
     "ok": true, "error_code": null, "duration_ms": 12}

**不记录文献正文**：只记录「调用参数」的摘要，结果（block 文本 / 表格 HTML /
摘要正文）永远不进审计。params 里若出现 token/secret 一类字段会被脱敏，
preview 压成单行并截到 80 字。

审计写失败不能拖垮工具调用：只往 stderr 打一条 warning。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("litbench_mcp.audit")

PREVIEW_MAX_CHARS = 80
# 参数里的敏感键：预览/摘要都不保留原值
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "access_token",
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "secret_key",
    }
)
# 单个字符串参与 digest 前的上限（防止异常大的参数把文件/内存撑爆）
_PARAM_STRING_CAP = 2000


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    """递归脱敏/截断参数，只用于审计摘要（不用于业务逻辑）。"""
    if key is not None and key.lower() in SENSITIVE_KEYS:
        return "***"
    if isinstance(value, str):
        return value[:_PARAM_STRING_CAP]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _sanitize(v, key=str(k)) for k, v in value.items()}
    return str(value)[:_PARAM_STRING_CAP]


def summarize_params(params: dict[str, Any] | None) -> tuple[str, str]:
    """返回 ``(params_digest, params_preview)``。

    digest 是参数（脱敏后）规范化 JSON 的 SHA-256，用来在不泄露内容的前提下关联
    「同一次调用」；preview 是同一份 JSON 压成单行后最多 80 字。
    """
    sanitized = _sanitize(params or {})
    canonical = json.dumps(
        sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    preview = " ".join(canonical.split())
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[: PREVIEW_MAX_CHARS - 1] + "…"
    return digest, preview


class AuditLogger:
    """追加写 ``audit.jsonl``；任何 I/O 错误只告警，不影响工具返回。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def write(
        self,
        *,
        actor: str,
        tool: str,
        params: dict[str, Any] | None,
        ok: bool,
        error_code: str | None,
        duration_ms: int,
    ) -> None:
        digest, preview = summarize_params(params)
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "actor": (actor or "unknown")[:64],
            "tool": tool,
            "params_digest": digest,
            "params_preview": preview,
            "ok": bool(ok),
            "error_code": error_code,
            "duration_ms": int(duration_ms),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - 磁盘满/权限问题
            logger.warning("审计日志写入失败（%s）：%s", self.path, exc)


__all__ = ["PREVIEW_MAX_CHARS", "SENSITIVE_KEYS", "AuditLogger", "summarize_params"]
