# SPDX-License-Identifier: AGPL-3.0-or-later
"""设置 API 测试（M2b）：BYO key 的保存/掩码/清除/测试连接。

两条安全断言必须一直成立：
1. 任何 HTTP 响应里都不能出现 API Key 原文（只有掩码）；
2. key 只落在服务端 storage_dir 的配置文件里（测试用 tmp_path）。
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

import litbench.routers.settings as settings_router
from litbench.config import settings
from litbench.main import app

RAW_KEY = "sk-test-abcdef1234567890"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_config_file", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "llm_provider", "mock")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.tmp_path = tmp_path  # type: ignore[attr-defined]
        yield ac


async def test_initial_state_is_mock(client) -> None:
    r = await client.get("/api/settings/llm")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["configured"] is False
    assert data["source"] == "none"
    assert data["has_api_key"] is False
    assert data["api_key_masked"] is None
    protocols = {p["protocol"]: p for p in data["supported_protocols"]}
    assert set(protocols) == {"chat_completion", "anthropic_messages", "openai_responses"}
    assert protocols["chat_completion"]["path"] == "/v1/chat/completions"
    assert protocols["anthropic_messages"]["path"] == "/v1/messages"
    assert protocols["openai_responses"]["path"] == "/v1/responses"
    assert data["note"]


async def test_save_persists_and_masks_key(client) -> None:
    r = await client.put(
        "/api/settings/llm",
        json={
            "protocol": "anthropic_messages",
            "base_url": "https://api.anthropic.com/",
            "model": "claude-sonnet-4-5",
            "api_key": RAW_KEY,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["configured"] is True
    assert data["source"] == "ui"
    assert data["protocol"] == "anthropic_messages"
    assert data["protocol_label"] == "Anthropic Messages"
    assert data["base_url"] == "https://api.anthropic.com"
    assert data["model"] == "claude-sonnet-4-5"
    assert data["has_api_key"] is True
    assert data["api_key_masked"] and data["api_key_masked"] != RAW_KEY
    assert RAW_KEY not in r.text  # 响应里不能出现原文
    assert data["endpoint"] == "https://api.anthropic.com/v1/messages"

    # 服务端文件里有原文（这是唯一允许出现的地方），且不在仓库内（tmp_path）
    cfg_file = settings.llm_config_path
    assert cfg_file.is_file()
    assert RAW_KEY in cfg_file.read_text(encoding="utf-8")

    # GET 也只回掩码
    r = await client.get("/api/settings/llm")
    data = r.json()
    assert data["configured"] is True
    assert RAW_KEY not in r.text
    assert data["api_key_masked"].startswith("sk-t")


async def test_blank_key_keeps_previous(client) -> None:
    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": RAW_KEY,
        },
    )
    first = (await client.get("/api/settings/llm")).json()["api_key_masked"]

    r = await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-reasoner",
            "api_key": "",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["api_key_masked"] == first
    assert data["model"] == "deepseek-reasoner"


async def test_validation_errors(client) -> None:
    r = await client.put(
        "/api/settings/llm",
        json={"protocol": "grpc", "base_url": "https://x", "model": "m", "api_key": "k"},
    )
    assert r.status_code == 422
    assert "协议" in r.json()["detail"]

    r = await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "api.example.com",
            "model": "m",
            "api_key": "k",
        },
    )
    assert r.status_code == 422
    assert "http" in r.json()["detail"]

    r = await client.put(
        "/api/settings/llm",
        json={"protocol": "chat_completion", "base_url": "https://x", "model": "m", "api_key": ""},
    )
    assert r.status_code == 422
    assert "API Key" in r.json()["detail"]

    r = await client.put(
        "/api/settings/llm",
        json={"protocol": "chat_completion", "base_url": "https://x", "model": "", "api_key": "k"},
    )
    assert r.status_code == 422


async def test_delete_returns_to_mock(client) -> None:
    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "openai_responses",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5",
            "api_key": RAW_KEY,
        },
    )
    assert settings.llm_config_path.is_file()
    r = await client.delete("/api/settings/llm")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["configured"] is False
    assert data["source"] == "none"
    assert not settings.llm_config_path.is_file()


async def test_env_fallback_is_reported(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", "https://env.example.com/v1")
    monkeypatch.setattr(settings, "llm_model", "env-model")
    monkeypatch.setattr(settings, "llm_api_key", "env-key-123456")
    data = (await client.get("/api/settings/llm")).json()
    assert data["configured"] is True
    assert data["source"] == "env"
    assert data["model"] == "env-model"
    assert "env" in data["note"].lower() or "环境变量" in data["note"]


async def test_test_connection_uses_form_values_without_saving(client, monkeypatch) -> None:
    captured: dict = {}

    async def fake_test(cfg):
        captured["protocol"] = cfg.protocol
        captured["model"] = cfg.model
        captured["api_key"] = cfg.api_key
        captured["base_url"] = cfg.base_url
        return {
            "ok": True,
            "error": None,
            "protocol": cfg.protocol,
            "model": cfg.model,
            "endpoint": "https://api.example.com/v1/chat/completions",
            "latency_ms": 123,
            "reply": "OK",
            "tokens_in": 4,
            "tokens_out": 1,
        }

    monkeypatch.setattr(settings_router, "test_connection", fake_test)
    r = await client.post(
        "/api/settings/llm/test",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com",
            "model": "test-model",
            "api_key": RAW_KEY,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data["latency_ms"] == 123
    assert captured["api_key"] == RAW_KEY
    assert not settings.llm_config_path.exists()  # 测试连接不落库


async def test_test_connection_requires_configuration(client) -> None:
    r = await client.post("/api/settings/llm/test")
    assert r.status_code == 400
    assert "还没有可测试" in r.json()["detail"]


async def test_config_file_survives_real_json(client) -> None:
    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com",
            "model": "m1",
            "api_key": RAW_KEY,
        },
    )
    raw = json.loads(settings.llm_config_path.read_text(encoding="utf-8"))
    assert raw["protocol"] == "chat_completion"
    assert raw["api_key"] == RAW_KEY
    assert raw["model"] == "m1"
    assert raw["updated_at"]
    # 供要求会话头的网关（OpenCode Go）使用：保存时自动生成，之后复用
    assert raw["session_id"].startswith("ses_")

    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com",
            "model": "m2",
            "api_key": "",
        },
    )
    raw2 = json.loads(settings.llm_config_path.read_text(encoding="utf-8"))
    assert raw2["session_id"] == raw["session_id"]  # 更新配置不换会话 id


def _fake_result(cfg, ok=True):
    return {
        "ok": ok,
        "error": None if ok else "boom",
        "protocol": cfg.protocol,
        "model": cfg.model,
        "endpoint": "https://api.example.com/v1/chat/completions",
        "latency_ms": 42,
        "reply": "OK" if ok else None,
        "tokens_in": 4,
        "tokens_out": 1,
    }


async def test_test_connection_updates_status_when_saved(client, monkeypatch) -> None:
    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com",
            "model": "m1",
            "api_key": RAW_KEY,
        },
    )
    before = (await client.get("/api/settings/llm")).json()
    assert before["last_test_ok"] is False
    assert before["last_tested_at"] is None

    async def ok_test(cfg):
        return _fake_result(cfg, ok=True)

    monkeypatch.setattr(settings_router, "test_connection", ok_test)
    r = await client.post("/api/settings/llm/test")
    assert r.status_code == 200, r.text
    after = (await client.get("/api/settings/llm")).json()
    assert after["last_test_ok"] is True
    assert after["last_tested_at"]
    assert after["updated_at"] == before["updated_at"]  # 只更新状态灯，不改配置更新时间

    async def bad_test(cfg):
        return _fake_result(cfg, ok=False)

    monkeypatch.setattr(settings_router, "test_connection", bad_test)
    await client.post("/api/settings/llm/test")
    assert (await client.get("/api/settings/llm")).json()["last_test_ok"] is False


async def test_save_keeps_status_only_if_config_unchanged(client, monkeypatch) -> None:
    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com",
            "model": "m1",
            "api_key": RAW_KEY,
        },
    )

    async def ok_test(cfg):
        return _fake_result(cfg, ok=True)

    monkeypatch.setattr(settings_router, "test_connection", ok_test)
    await client.post("/api/settings/llm/test")
    assert (await client.get("/api/settings/llm")).json()["last_test_ok"] is True

    # 同配置再保存一次（key 留空）：保留已连接状态
    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com",
            "model": "m1",
            "api_key": "",
        },
    )
    assert (await client.get("/api/settings/llm")).json()["last_test_ok"] is True

    # 换模型：状态重置为未测试
    await client.put(
        "/api/settings/llm",
        json={
            "protocol": "chat_completion",
            "base_url": "https://api.example.com",
            "model": "m2",
            "api_key": "",
        },
    )
    assert (await client.get("/api/settings/llm")).json()["last_test_ok"] is False
