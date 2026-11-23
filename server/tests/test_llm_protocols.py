# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM 协议适配单测（M2b）：三种协议各自怎么组请求、怎么解析响应、怎么报错。

用 ``httpx.MockTransport`` 断言**真正发出去的 URL/headers/body**，不发网络请求。
"""

from __future__ import annotations

import json

import httpx
import pytest

from litbench.llm import (
    HttpLLMClient,
    LLMError,
    LLMResult,
    complete_with_budget_retry,
    vision_message,
)
from litbench.llm import test_connection as llm_test_connection
from litbench.llm_config import LLMConfig, LLMConfigError, endpoint_url


def _cfg(protocol: str, base_url: str = "https://api.example.com") -> LLMConfig:
    return LLMConfig(
        protocol=protocol,
        base_url=base_url,
        model="test-model",
        api_key="sk-test-123456",
        timeout_s=30,
    )


def test_endpoint_url_normalization() -> None:
    assert (
        endpoint_url("https://api.deepseek.com", "chat_completion")
        == "https://api.deepseek.com/v1/chat/completions"
    )
    assert (
        endpoint_url("https://api.openai.com/v1", "chat_completion")
        == "https://api.openai.com/v1/chat/completions"
    )
    assert (
        endpoint_url("https://api.openai.com/v1/chat/completions", "chat_completion")
        == "https://api.openai.com/v1/chat/completions"
    )
    assert (
        endpoint_url("https://api.anthropic.com", "anthropic_messages")
        == "https://api.anthropic.com/v1/messages"
    )
    assert (
        endpoint_url("https://api.openai.com/v1", "openai_responses")
        == "https://api.openai.com/v1/responses"
    )
    with pytest.raises(LLMConfigError):
        endpoint_url("https://x", "nope")


async def test_chat_completion_request_and_parse() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": '{"summary": "你好"}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22},
            },
        )

    client = HttpLLMClient(
        _cfg("chat_completion", "https://api.example.com/v1"),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "写摘要"}]
    )
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-123456"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["messages"][1] == {"role": "user", "content": "写摘要"}
    assert result.text == '{"summary": "你好"}'
    assert result.parsed == {"summary": "你好"}
    assert result.tokens_in == 11 and result.tokens_out == 22
    assert result.provider == "openai-chat"
    assert result.model == "test-model"


async def test_anthropic_messages_request_and_parse() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        )

    client = HttpLLMClient(
        _cfg("anthropic_messages", "https://api.anthropic.com"), transport=httpx.MockTransport(handler)
    )
    result = await client.complete(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]
    )
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-test-123456"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["system"] == "你是助手"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert result.text == "OK"
    assert result.tokens_in == 7 and result.tokens_out == 3
    assert result.provider == "anthropic"


async def test_openai_responses_request_and_parse() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}],
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )

    client = HttpLLMClient(
        _cfg("openai_responses", "https://api.openai.com/v1"), transport=httpx.MockTransport(handler)
    )
    result = await client.complete(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]
    )
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert seen["body"]["instructions"] == "你是助手"
    assert seen["body"]["input"][0]["content"][0]["type"] == "input_text"
    assert result.text == "OK"
    assert result.provider == "openai-resp"


async def test_error_status_and_bad_payload_are_readable() -> None:
    def status_401(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"error":"invalid api key"}')

    client = HttpLLMClient(_cfg("chat_completion"), transport=httpx.MockTransport(status_401))
    with pytest.raises(LLMError) as excinfo:
        await client.complete([{"role": "user", "content": "hi"}])
    assert "HTTP 401" in str(excinfo.value)
    assert "invalid api key" in str(excinfo.value)

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    client = HttpLLMClient(_cfg("chat_completion"), transport=httpx.MockTransport(not_json))
    with pytest.raises(LLMError) as excinfo:
        await client.complete([{"role": "user", "content": "hi"}])
    assert "不是 JSON" in str(excinfo.value)

    def wrong_shape(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})

    client = HttpLLMClient(_cfg("chat_completion"), transport=httpx.MockTransport(wrong_shape))
    with pytest.raises(LLMError) as excinfo:
        await client.complete([{"role": "user", "content": "hi"}])
    assert "choices" in str(excinfo.value)


async def test_test_connection_reports_ok_and_failure() -> None:
    def ok_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )

    cfg = _cfg("chat_completion", "https://api.example.com")
    ok = await llm_test_connection(cfg, transport=httpx.MockTransport(ok_handler))
    assert ok["ok"] is True
    assert ok["endpoint"] == "https://api.example.com/v1/chat/completions"
    assert ok["reply"] == "OK"
    assert ok["protocol"] == "chat_completion"

    def fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    bad = await llm_test_connection(cfg, transport=httpx.MockTransport(fail_handler))
    assert bad["ok"] is False
    assert "HTTP 500" in bad["error"]


# ---------------------------------------------------------------------------
# OpenCode Go / Console 兼容（用户实际要接的场景）
# ---------------------------------------------------------------------------
def _opencode_cfg(protocol: str, model: str) -> LLMConfig:
    base = (
        "https://opencode.ai/inference/go/anthropic/v1"
        if protocol == "anthropic_messages"
        else "https://opencode.ai/inference/go/openai/v1"
    )
    return LLMConfig(
        protocol=protocol,
        base_url=base,
        model=model,
        api_key="oc_sk_test_1234567890",
        timeout_s=30,
        session_id="ses_test1234567890",
    )


async def test_opencode_go_chat_completions() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 1},
            },
        )

    client = HttpLLMClient(
        _opencode_cfg("chat_completion", "glm-5.3"), transport=httpx.MockTransport(handler)
    )
    result = await client.complete([{"role": "user", "content": "你好"}])
    assert seen["url"] == "https://opencode.ai/inference/go/openai/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer oc_sk_test_1234567890"
    assert seen["headers"]["x-opencode-session"] == "ses_test1234567890"
    assert "litbench" in seen["headers"]["user-agent"]
    assert seen["body"]["model"] == "glm-5.3"
    assert result.text == "OK"


async def test_opencode_go_anthropic_messages_dual_auth() -> None:
    """OpenCode Console 的 /anthropic/v1/messages 认 Bearer；原生 Anthropic 只认 x-api-key。"""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 4, "output_tokens": 1},
            },
        )

    client = HttpLLMClient(
        _opencode_cfg("anthropic_messages", "claude-haiku-5-5"),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete([{"role": "user", "content": "你好"}])
    assert seen["url"] == "https://opencode.ai/inference/go/anthropic/v1/messages"
    assert seen["headers"]["x-api-key"] == "oc_sk_test_1234567890"
    assert seen["headers"]["authorization"] == "Bearer oc_sk_test_1234567890"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["headers"]["x-opencode-session"] == "ses_test1234567890"
    assert result.text == "OK"


async def test_opencode_go_responses() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "OK"}]}
                ],
                "usage": {"input_tokens": 6, "output_tokens": 1},
            },
        )

    client = HttpLLMClient(
        _opencode_cfg("openai_responses", "gpt-6-luna"), transport=httpx.MockTransport(handler)
    )
    result = await client.complete([{"role": "user", "content": "你好"}])
    assert seen["url"] == "https://opencode.ai/inference/go/openai/v1/responses"
    assert seen["headers"]["x-opencode-session"] == "ses_test1234567890"
    assert result.text == "OK"


def test_endpoint_url_for_opencode_go() -> None:
    assert (
        endpoint_url("https://opencode.ai/inference/go/openai/v1", "chat_completion")
        == "https://opencode.ai/inference/go/openai/v1/chat/completions"
    )
    assert (
        endpoint_url("https://opencode.ai/inference/go/openai/v1", "openai_responses")
        == "https://opencode.ai/inference/go/openai/v1/responses"
    )
    assert (
        endpoint_url("https://opencode.ai/inference/go/anthropic/v1", "anthropic_messages")
        == "https://opencode.ai/inference/go/anthropic/v1/messages"
    )


# ---------------------------------------------------------------------------
# 多模态（M3 图纠错）：图片消息在三种协议里各自怎么发
# ---------------------------------------------------------------------------
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def _vision_messages() -> list[dict]:
    return vision_message("这张图是什么？", _IMAGE_BYTES, media_type="image/png")


async def test_chat_completion_vision_payload_and_cost() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 40,
                    "cost": 0.00042,
                },
            },
        )

    client = HttpLLMClient(
        _cfg("chat_completion", "https://api.example.com/v1"),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(_vision_messages(), max_tokens=1600)
    content = seen["body"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "这张图是什么？"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert seen["body"]["max_tokens"] == 1600  # 单次任务的输出预算覆盖全局默认
    assert result.tokens_in == 900 and result.tokens_out == 40
    assert result.cost == pytest.approx(0.00042)  # 网关回传的真实费用要记下来


async def test_anthropic_vision_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "OK"}], "usage": {}},
        )

    client = HttpLLMClient(
        _cfg("anthropic_messages", "https://api.anthropic.com"),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(_vision_messages())
    content = seen["body"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "这张图是什么？"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"


async def test_responses_vision_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "OK"}]}
                ],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )

    client = HttpLLMClient(
        _cfg("openai_responses", "https://api.openai.com/v1"),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(_vision_messages())
    content = seen["body"]["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


async def test_complete_with_budget_retry_doubles_once() -> None:
    """推理模型预算不够时自动翻倍重试一次；其它错误不重试。"""

    class Flaky:
        provider = "fake"
        model = "fake"

        def __init__(self) -> None:
            self.calls: list[int | None] = []

        async def complete(
            self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None
        ):
            self.calls.append(max_tokens)
            if len(self.calls) == 1:
                raise LLMError("模型只输出了推理过程、没有正文（输出预算被推理耗尽）")
            return LLMResult(
                text="{}", parsed={}, provider="fake", model="fake", tokens_in=1, tokens_out=1
            )

    flaky = Flaky()
    await complete_with_budget_retry(
        flaky, [{"role": "user", "content": "hi"}], max_tokens=1000
    )
    assert flaky.calls == [1000, 2000]

    class Broken:
        provider = "fake"
        model = "fake"

        async def complete(self, messages, *, json_schema=None, meta=None, max_tokens=None):
            raise LLMError("API 返回 HTTP 401：bad key")

    with pytest.raises(LLMError) as excinfo:
        await complete_with_budget_retry(
            Broken(), [{"role": "user", "content": "hi"}], max_tokens=1000
        )
    assert "401" in str(excinfo.value)


async def test_reasoning_only_reply_gives_actionable_error() -> None:
    """deepseek-v4.1-flash 这类推理模型：预算被 reasoning 吃光时 content 为空。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "想了很久……"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 100},
            },
        )

    client = HttpLLMClient(_cfg("chat_completion"), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError) as excinfo:
        await client.complete([{"role": "user", "content": "hi"}])
    assert "推理" in str(excinfo.value)
    assert "max_tokens" in str(excinfo.value)

