# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM provider 抽象（M2）。

三条原则（docs/15 §6）：

1. **provider 可换、接口不变**：``mock``（无 key 的确定性假响应）/ ``api``（用户在
   LitBench 设置页里填的 BYO 网关，协议见 ``llm_config.PROTOCOLS``）/ ``agent``（M4 预留）。
2. **key 只在服务端**：来自 UI 的配置存在 ``<storage_dir>/llm-config.json``（不进仓库/DB，
   GET 只回掩码）；旧方式仍可用环境变量 ``LLM_BASE_URL/LLM_MODEL/LLM_API_KEY`` 兜底。
3. **调用可审计**：每次返回 tokens/耗时，服务端记到 summary 行上；费用字段留 0（各家计价不同，
   M2b 后续按 provider 补价目表）。

支持并明确区分三种协议（用户在设置页里必须选一个，不猜）：

* ``chat_completion``：OpenAI 兼容 ``/v1/chat/completions``（messages[] / choices[]）；
* ``anthropic_messages``：Anthropic 原生 ``/v1/messages``（x-api-key + content[]）；
* ``openai_responses``：OpenAI ``/v1/responses``（input[] / output[]）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Protocol

from .config import settings
from .llm_config import (
    DEFAULT_TIMEOUT_S,
    LLMConfig,
    LLMConfigError,
    endpoint_url,
    load_config,
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
PROVIDER_NAMES = {
    "chat_completion": "openai-chat",
    "anthropic_messages": "anthropic",
    "openai_responses": "openai-resp",
}
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    """provider 调用失败。错误信息要能直接给用户看。"""


@dataclass(slots=True)
class LLMResult:
    text: str
    parsed: dict | None
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost: float = 0.0
    latency_ms: int = 0


class LLMClient(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        messages: list[dict],
        *,
        json_schema: dict | None = None,
        meta: dict | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult: ...


def vision_message(
    text: str, image_bytes: bytes, *, media_type: str = "image/png"
) -> list[dict]:
    """构造一条带图片的 user 消息（M3 图纠错）。

    内部统一用 ``{"type": "text"}`` / ``{"type": "image", "data": "<base64>"}``，
    由 ``HttpLLMClient._payload`` 按协议转换成各自格式；mock/测试不用关心图片。
    """
    import base64

    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "image",
                    "media_type": media_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                },
            ],
        }
    ]


def vision_message_multi(
    text: str, images: list[tuple[bytes, str, str]]
) -> list[dict]:
    """构造一条带**多张图片**的 user 消息（M3c 框复核）。

    ``images`` 每项是 ``(png_bytes, media_type, label)``；label 会作为图片前的说明文字，
    让模型知道这张图是「整页 / 标注框 / 目标裁图」。内部块格式与 ``vision_message`` 一致，
    三种协议（chat_completion / anthropic_messages / openai_responses）都能转换。
    """
    import base64  # noqa: PLC0415

    content: list[dict] = [{"type": "text", "text": text}]
    for data, media_type, label in images:
        if label:
            content.append({"type": "text", "text": label})
        content.append(
            {
                "type": "image",
                "media_type": media_type or "image/png",
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
    return [{"role": "user", "content": content}]


def _message_text(content) -> str:
    """把消息 content（字符串或块数组）压平成文本，供 token 估算/日志用。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "image":
            parts.append("[image]")
    return "".join(parts)


def _openai_content(content):
    """内部块 → Chat Completions 的 content（文本保持字符串，图文才用数组）。"""
    if not isinstance(content, (list, tuple)):
        return content
    blocks: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image" and block.get("data"):
            media = block.get("media_type") or "image/png"
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{block['data']}"},
                }
            )
        elif isinstance(block.get("text"), str):
            blocks.append({"type": "text", "text": block["text"]})
    return blocks or ""


def _anthropic_content(content):
    """内部块 → Anthropic Messages 的 content 数组。"""
    if not isinstance(content, (list, tuple)):
        return content
    blocks: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image" and block.get("data"):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.get("media_type") or "image/png",
                        "data": block["data"],
                    },
                }
            )
        elif isinstance(block.get("text"), str):
            blocks.append({"type": "text", "text": block["text"]})
    return blocks


def _responses_message_content(content) -> list[dict]:
    """Responses API 的 content 必须是块数组；纯文本也包成 input_text。"""
    converted = _responses_content(content)
    if isinstance(converted, list):
        return converted
    return [{"type": "input_text", "text": "" if content is None else str(content)}]


def _responses_content(content):
    """内部块 → Responses API 的 content 数组（input_text / input_image）。"""
    if not isinstance(content, (list, tuple)):
        return content
    blocks: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image" and block.get("data"):
            media = block.get("media_type") or "image/png"
            blocks.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{media};base64,{block['data']}",
                }
            )
        elif isinstance(block.get("text"), str):
            blocks.append({"type": "input_text", "text": block["text"]})
    return blocks


def parse_json_object(text: str) -> dict | None:
    """从模型输出里尽量取一个 JSON 对象（容忍 ```json 围栏和前后废话）。"""
    cleaned = (text or "").strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _estimate_tokens(messages: list[dict], text: str) -> tuple[int, int]:
    chars_in = sum(len(_message_text(m.get("content"))) for m in messages)
    return max(1, chars_in // 4), max(1, len(text) // 4)


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    rest: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        text = _message_text(content) if not isinstance(content, str) else content
        if role == "system":
            system_parts.append(text)
        else:
            rest.append({"role": role, "content": content})
    return "\n\n".join(system_parts), rest


def _extract_text_list(content) -> str:
    """Anthropic / Responses 的 content 是块数组；兼容纯字符串。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") in ("output_text", "text") and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _short(text: str, limit: int = 300) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


# ---------------------------------------------------------------------------
# BYO 真实 provider：三种协议
# ---------------------------------------------------------------------------
class HttpLLMClient:
    """用户在设置页配置的 API：按 protocol 组请求、解析响应。"""

    def __init__(self, cfg: LLMConfig, *, transport=None) -> None:
        self.cfg = cfg
        self.protocol = cfg.protocol
        self.provider = PROVIDER_NAMES.get(cfg.protocol, "api")
        self.model = cfg.model
        self._transport = transport  # 测试注入 httpx.MockTransport

    # --- 请求组装 ---
    def _host(self) -> str:
        from urllib.parse import urlparse

        return (urlparse(self.cfg.base_url).hostname or "").lower()

    def _headers(self) -> dict[str, str]:
        host = self._host()
        if self.protocol == "anthropic_messages":
            headers = {
                "x-api-key": self.cfg.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
            # Anthropic 原生（api.anthropic.com）只认 x-api-key；兼容网关（OpenCode Console
            # 等）按 OpenAI 风格认 Bearer。两个都带上互不冲突，省得用户填错认证方式。
            if not host.endswith("anthropic.com"):
                headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        else:
            headers = {
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
            }
        headers["User-Agent"] = "litbench/0.0.1 (server-side literature summarizer)"
        # OpenCode Go / Console 要求每个客户端发稳定会话头（路由 + prompt cache）；
        # 其它厂商忽略未知头即可。session_id 在保存配置时生成，之后复用。
        if host.endswith("opencode.ai"):
            session_id = self.cfg.session_id or (
                "ses_" + hashlib.sha1(  # noqa: S324 — 只做稳定标识，不做安全用途
                    (self.cfg.base_url + "|" + self.cfg.model).encode("utf-8")
                ).hexdigest()
            )
            headers["x-opencode-session"] = session_id
        return headers

    def _payload(
        self, messages: list[dict], json_schema: dict | None, max_tokens: int | None = None
    ) -> dict:
        limit = max_tokens or settings.llm_max_tokens
        if self.protocol == "chat_completion":
            payload: dict = {
                "model": self.cfg.model,
                "messages": [
                    {
                        "role": m.get("role", "user"),
                        "content": _openai_content(m.get("content", "")),
                    }
                    for m in messages
                ],
                "max_tokens": limit,
            }
            return payload
        if self.protocol == "anthropic_messages":
            system, rest = _split_system(messages)
            payload = {
                "model": self.cfg.model,
                "max_tokens": limit,
                "messages": [
                    {"role": m["role"], "content": _anthropic_content(m["content"])}
                    for m in rest
                ],
            }
            if system:
                payload["system"] = system
            return payload
        # openai_responses
        system, rest = _split_system(messages)
        payload = {
            "model": self.cfg.model,
            "input": [
                {"role": m["role"], "content": _responses_message_content(m["content"])}
                for m in rest
            ],
            "max_output_tokens": limit,
        }
        if system:
            payload["instructions"] = system
        return payload

    # --- 响应解析 ---
    @staticmethod
    def _parse_usage(payload: dict) -> tuple[int, int, float]:
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            return 0, 0, 0.0
        cost = usage.get("cost")
        try:
            cost_f = float(cost) if cost is not None else 0.0
        except (TypeError, ValueError):
            cost_f = 0.0
        return (
            int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            cost_f,
        )

    def _parse_text(self, payload: dict) -> str:
        if self.protocol == "chat_completion":
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise LLMError("响应里没有 choices（不是 Chat Completions 协议？）")
            message = choices[0].get("message") or {}
            content = message.get("content")
            text = _extract_text_list(content) if not isinstance(content, str) else content
            if not text:
                # 推理模型（如 deepseek-v4.1-flash）先花 token 在 reasoning_content 上：
                # 预算不够时 content 会是空的，finish_reason=length。这是用户能自己修的问题。
                finish = str(choices[0].get("finish_reason") or "")
                if message.get("reasoning_content") or finish == "length":
                    raise LLMError(
                        "模型只输出了推理过程、没有正文（输出预算被推理耗尽）。"
                        "请把该次任务的 max_tokens 调高，或换非推理模型"
                    )
                raise LLMError("响应里没有文本内容")
            return text
        if self.protocol == "anthropic_messages":
            content = payload.get("content")
            text = _extract_text_list(content)
            if not text:
                raise LLMError("响应里没有 content[].text（不是 Anthropic Messages 协议？）")
            return text
        # openai_responses
        output = payload.get("output")
        if not isinstance(output, list):
            raise LLMError("响应里没有 output（不是 Responses 协议？）")
        parts: list[str] = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                parts.append(_extract_text_list(item.get("content")))
            elif isinstance(item, dict) and item.get("type") == "refusal":
                raise LLMError(f"模型拒绝回答：{_short(str(item.get('refusal') or ''))}")
        text = "".join(parts)
        if not text:
            raise LLMError("响应里没有 output_text")
        return text

    async def complete(
        self,
        messages: list[dict],
        *,
        json_schema: dict | None = None,
        meta: dict | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        import httpx

        started = time.perf_counter()
        try:
            url = endpoint_url(self.cfg.base_url, self.protocol)
        except LLMConfigError as exc:
            raise LLMError(str(exc)) from exc
        payload = self._payload(messages, json_schema, max_tokens)
        try:
            async with httpx.AsyncClient(
                timeout=timeout_s or self.cfg.timeout_s or DEFAULT_TIMEOUT_S,
                transport=self._transport,
            ) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise LLMError(f"连不上 {url}：{type(exc).__name__}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(
                f"API 返回 HTTP {resp.status_code}：{_short(resp.text)}（端点 {url}）"
            )
        try:
            body = resp.json()
        except ValueError as exc:  # 不是 JSON
            raise LLMError(f"API 返回的不是 JSON：{_short(resp.text)}") from exc
        if not isinstance(body, dict):
            raise LLMError("API 返回格式不是 JSON 对象")
        text = self._parse_text(body)
        tokens_in, tokens_out, cost = self._parse_usage(body)
        return LLMResult(
            text=text,
            parsed=parse_json_object(text),
            provider=self.provider,
            model=self.cfg.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# Mock / 旧 env 兜底
# ---------------------------------------------------------------------------
class MockLLM:
    """确定性 mock provider：不联网、不花钱，输出可被测试精确断言。

    * 默认从 ``meta`` 里拿本章节真实 block_id 作为证据（这样证据校验器会通过）；
    * 测试可传 ``evidence_ids`` / ``raw`` / ``confidence`` / ``fail`` 造异常分支。
    """

    provider = "mock"

    def __init__(
        self,
        model: str = "mock-section-summary-v1",
        *,
        evidence_ids: list[str] | None = None,
        raw: str | None = None,
        confidence: float = 0.95,
        fail: str | None = None,
    ) -> None:
        self.model = model
        self._evidence_ids = evidence_ids
        self._raw = raw
        self._confidence = confidence
        self._fail = fail

    async def complete(
        self,
        messages: list[dict],
        *,
        json_schema: dict | None = None,
        meta: dict | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        started = time.perf_counter()
        if self._fail:
            raise LLMError(self._fail)
        meta = meta or {}
        if self._raw is not None:
            text = self._raw
        else:
            evidence = (
                list(self._evidence_ids)
                if self._evidence_ids is not None
                else list(meta.get("evidence_ids") or [])[:3]
            )
            title = str(meta.get("section_title") or "本节")
            texts = [str(t) for t in meta.get("texts") or []]
            n_blocks = len(meta.get("block_ids") or [])
            lead = texts[0][:60] if texts else "该章节暂无可读正文"
            payload = {
                "summary": f"（mock 摘要）{title}：本节共 {n_blocks} 个证据块。{lead}",
                "key_points": [
                    f"覆盖 {len(evidence)} 条原文证据",
                    f"章节区间 {meta.get('page_start', 0)}–{meta.get('page_end', 0)} 页",
                ],
                "evidence_block_ids": evidence,
                "confidence": self._confidence,
            }
            text = json.dumps(payload, ensure_ascii=False)
        tokens_in, tokens_out = _estimate_tokens(messages, text)
        return LLMResult(
            text=text,
            parsed=parse_json_object(text),
            provider=self.provider,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=0.0,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _env_config() -> LLMConfig | None:
    """旧方式的兜底：环境变量 LLM_BASE_URL/LLM_MODEL/LLM_API_KEY（OpenAI 兼容）。"""
    if settings.llm_base_url and settings.llm_api_key:
        return LLMConfig(
            protocol="chat_completion",
            base_url=settings.llm_base_url,
            model=settings.llm_model or "",
            api_key=settings.llm_api_key,
            timeout_s=settings.llm_timeout_s,
        )
    return None


def effective_config() -> tuple[str, LLMConfig | None]:
    """当前生效的配置来源：``ui``（设置页保存的）> ``env``（环境变量）> ``mock``。"""
    cfg = load_config()
    if cfg is not None:
        return "ui", cfg
    env = _env_config()
    if env is not None:
        return "env", env
    return "mock", None


def get_client(provider: str | None = None, *, transport=None) -> LLMClient:
    """解析当前生效的 provider：UI 配置 > env 配置 > mock（provider=mock 可强制 mock）。"""
    if (provider or "").strip().lower() == "mock":
        return MockLLM()
    _source, cfg = effective_config()
    if cfg is not None:
        return HttpLLMClient(cfg, transport=transport)
    name = (provider or settings.llm_provider or "mock").strip().lower()
    if name == "mock":
        return MockLLM()
    raise LLMError(
        "还没有配置模型：请先在右上角「设置」里填 base_url / model / API key"
    )


async def complete_with_budget_retry(
    client: LLMClient,
    messages: list[dict],
    *,
    json_schema: dict | None = None,
    meta: dict | None = None,
    max_tokens: int | None = None,
) -> LLMResult:
    """推理模型输出预算被 reasoning 吃光时，翻倍 max_tokens 重试一次。

    真机实测（deepseek-v4.1-flash + 图片）：同样 prompt 有时 2k 够、有时 4k 都不够
    （reasoning 长度不稳定）。失败是「模型没给正文」，重试一次成本可控；还不行就
    把可读错误抛给用户，让他调 max_tokens / 换非推理模型。
    """
    try:
        return await client.complete(
            messages, json_schema=json_schema, meta=meta, max_tokens=max_tokens
        )
    except LLMError as exc:
        if "推理过程" not in str(exc) or not max_tokens:
            raise
        # 重试：预算翻倍 + 给更长的超时（8000 token 的推理可能 >60s，真机实测）
        cfg = getattr(client, "cfg", None)
        base_timeout = float(getattr(cfg, "timeout_s", None) or 60.0)
        return await client.complete(
            messages,
            json_schema=json_schema,
            meta=meta,
            max_tokens=min(int(max_tokens) * 2, 12000),
            timeout_s=max(base_timeout, 60.0) * 2,
        )


async def test_connection(cfg: LLMConfig, *, transport=None) -> dict:
    """设置页「测试连接」：发一条最小请求，返回可读结果（不落库、不记费用）。"""
    client = HttpLLMClient(cfg, transport=transport)
    endpoint = endpoint_url(cfg.base_url, cfg.protocol)
    started = time.perf_counter()
    try:
        result = await client.complete(
            [{"role": "user", "content": "只回复两个字母：OK"}],
        )
    except LLMError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "protocol": cfg.protocol,
            "model": cfg.model,
            "endpoint": endpoint,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    return {
        "ok": True,
        "error": None,
        "protocol": cfg.protocol,
        "model": cfg.model,
        "endpoint": endpoint,
        "latency_ms": result.latency_ms,
        "reply": _short(result.text, 200),
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
    }


__all__ = [
    "HttpLLMClient",
    "LLMClient",
    "LLMError",
    "LLMResult",
    "MockLLM",
    "complete_with_budget_retry",
    "get_client",
    "parse_json_object",
    "test_connection",
    "vision_message",
    "vision_message_multi",
]
