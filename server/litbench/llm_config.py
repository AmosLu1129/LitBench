# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM 的 BYO 配置（M2b）：用户在 LitBench 界面上自己填 base_url / model / API key。

安全边界（比「key 放环境变量」更窄，但仍然只留在服务端）：

* key 只写服务端文件 ``<storage_dir>/llm-config.json``（``.data/`` 已在 .gitignore，
  不进仓库、不进数据库、不打日志）；
* ``GET /api/settings/llm`` 只返回掩码（``sk-a…bcd1``）和 has_api_key，**原文永不回传前端**；
* 文件权限按 0600 创建（Windows 上 chmod 只作 best-effort，仍靠「目录不进仓库」兜底）；
* 支持三种协议（用户必须明确知道自己在接哪一种，不能猜）：

  - ``chat_completion``：OpenAI 兼容 ``POST {base}/v1/chat/completions``
    （OpenAI / DeepSeek / DSH 网关 / 大多数兼容网关）；
  - ``anthropic_messages``：Anthropic 原生 ``POST {base}/v1/messages``
    （``x-api-key`` + ``anthropic-version``，Claude 系列）；
  - ``openai_responses``：OpenAI 新的 ``POST {base}/v1/responses``。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

# 协议登记表：后端 / 前端共用（前端从 GET /api/settings/llm 的 supported_protocols 读）
PROTOCOLS: dict[str, dict[str, str]] = {
    "chat_completion": {
        "label": "OpenAI Chat Completions",
        "path": "/v1/chat/completions",
        "hint": "OpenAI / DeepSeek / DSH 兼容网关 / 大多数第三方网关；请求体是 messages[]，响应取 choices[0].message.content",
    },
    "anthropic_messages": {
        "label": "Anthropic Messages",
        "path": "/v1/messages",
        "hint": "Anthropic Claude 原生协议；请求头用 x-api-key + anthropic-version: 2023-06-01，响应取 content[0].text",
    },
    "openai_responses": {
        "label": "OpenAI Responses",
        "path": "/v1/responses",
        "hint": "OpenAI 新的 Responses API；请求体是 input[]，响应取 output[] 里的 output_text",
    },
}
DEFAULT_PROTOCOL = "chat_completion"
DEFAULT_TIMEOUT_S = 60.0
MIN_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 300.0


class LLMConfigError(ValueError):
    """配置不合法（缺字段 / 协议不认识 / base_url 不是 http(s)）。"""


@dataclass(slots=True)
class LLMConfig:
    protocol: str = DEFAULT_PROTOCOL
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    created_at: str | None = None
    updated_at: str | None = None
    # 给「要求会话头」的网关用（如 OpenCode Go 的 x-opencode-session）：保存配置时自动生成，
    # 之后同一实例的所有请求复用它，方便上游做路由/提示词缓存。
    session_id: str = ""
    # 最近一次「测试连接」的结果：顶栏状态灯用；只有测试请求会改它
    last_test_ok: bool = False
    last_tested_at: str | None = None
    extra: dict = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        return asdict(self)


def _config_file() -> Path:
    return settings.llm_config_path


def load_config() -> LLMConfig | None:
    """读服务端配置文件；不存在/坏 JSON/未知协议都当作未配置（不让启动挂掉）。"""
    path = _config_file()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    protocol = str(raw.get("protocol") or "")
    if protocol not in PROTOCOLS:
        return None
    api_key = str(raw.get("api_key") or "")
    if not str(raw.get("base_url") or "") or not str(raw.get("model") or "") or not api_key:
        return None
    try:
        timeout_s = float(raw.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    timeout_s = min(max(timeout_s, MIN_TIMEOUT_S), MAX_TIMEOUT_S)
    extra = raw.get("extra")
    session_id = str(raw.get("session_id") or "")
    return LLMConfig(
        protocol=protocol,
        base_url=str(raw["base_url"]).strip().rstrip("/"),
        model=str(raw["model"]).strip(),
        api_key=api_key,
        timeout_s=timeout_s,
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        session_id=session_id,
        last_test_ok=bool(raw.get("last_test_ok", False)),
        last_tested_at=raw.get("last_tested_at"),
        extra=extra if isinstance(extra, dict) else {},
    )


def save_config(cfg: LLMConfig, *, touch_updated: bool = True) -> None:
    """原子写 + 0600。文件在 .data/（gitignore），不进仓库、不进 DB。

    ``touch_updated=False`` 用于只更新测试结果的状态灯，不把「配置更新时间」弄脏。
    """
    cfg.base_url = cfg.base_url.strip().rstrip("/")
    if not cfg.session_id:
        cfg.session_id = "ses_" + uuid.uuid4().hex
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if touch_updated:
        cfg.updated_at = datetime.now(timezone.utc).isoformat()
    if not cfg.created_at:
        cfg.created_at = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(cfg.to_json_dict(), ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".llm-config-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp_name, 0o600)  # Windows 上是 best-effort
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def delete_config() -> bool:
    path = _config_file()
    if path.exists():
        path.unlink(missing_ok=True)
        return True
    return False


def mask_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}…{api_key[-4:]}"


def endpoint_url(base_url: str, protocol: str) -> str:
    """把用户填的 base_url 归一成具体端点。

    允许用户填三种形式（都常见）：
    * ``https://api.deepseek.com`` → 追加 ``/v1/chat/completions``；
    * ``https://api.openai.com/v1`` → 追加 ``/chat/completions``；
    * 已经是完整端点（``.../v1/chat/completions``）→ 原样使用。
    """
    info = PROTOCOLS.get(protocol)
    if info is None:
        raise LLMConfigError(f"不支持的协议：{protocol}")
    base = (base_url or "").strip().rstrip("/")
    path = info["path"]
    if base.endswith(path):
        return base
    if base.endswith("/v1"):
        return base + path[len("/v1") :]
    return base + path


def validate_config(
    *,
    protocol: str,
    base_url: str,
    model: str,
    api_key: str | None,
    have_existing_key: bool,
    timeout_s: float | None = None,
) -> tuple[str, str, str, float]:
    """校验 PUT 入参，返回 (protocol, base_url, model, timeout_s)。key 单独处理。"""
    if protocol not in PROTOCOLS:
        raise LLMConfigError(
            f"不支持的协议：{protocol}（支持 {' / '.join(PROTOCOLS)}）"
        )
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMConfigError("base_url 不能为空")
    if not base.startswith(("http://", "https://")):
        raise LLMConfigError("base_url 必须以 http:// 或 https:// 开头")
    if any(ch.isspace() for ch in base):
        raise LLMConfigError("base_url 不能包含空白字符")
    name = (model or "").strip()
    if not name:
        raise LLMConfigError("model 不能为空")
    if not have_existing_key and not (api_key or "").strip():
        raise LLMConfigError("首次配置必须填 API Key")
    timeout = DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    if not (MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S):
        raise LLMConfigError(f"timeout 需在 {MIN_TIMEOUT_S:g}–{MAX_TIMEOUT_S:g} 秒之间")
    return protocol, base, name, timeout


def supported_protocols() -> list[dict]:
    return [
        {"protocol": key, "label": info["label"], "path": info["path"], "hint": info["hint"]}
        for key, info in PROTOCOLS.items()
    ]
