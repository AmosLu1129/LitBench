# SPDX-License-Identifier: MIT
"""配置：默认值、环境变量覆盖、可读错误。"""

from __future__ import annotations

from pathlib import Path

import pytest

from litbench_mcp.config import ConfigError, Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.api_base == "http://127.0.0.1:8000"
    assert settings.transport == "stdio"
    assert settings.rate_limit == 120
    assert settings.rate_window_s == 60.0
    assert settings.max_text_chars == 1200
    assert settings.state_dir.is_absolute()
    assert settings.tokens_path.name == "tokens.json"
    assert settings.audit_path.name == "audit.jsonl"
    assert settings.http_mcp_url.endswith("/mcp")


def test_api_base_trailing_slash_normalized() -> None:
    settings = Settings(api_base="http://127.0.0.1:8000/")
    assert settings.api_base == "http://127.0.0.1:8000"


def test_relative_state_dir_resolved(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(state_dir=".data/mcp")
    assert settings.state_dir == (tmp_path / ".data/mcp").resolve()


def test_from_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LITBENCH_MCP_API", "http://127.0.0.1:9999")
    monkeypatch.setenv("LITBENCH_MCP_API_TOKEN", "internal-token-123456")
    monkeypatch.setenv("LITBENCH_MCP_STATE_DIR", ".data/custom-mcp")
    monkeypatch.setenv("LITBENCH_MCP_RATE_LIMIT", "7")
    monkeypatch.setenv("LITBENCH_MCP_RATE_WINDOW_SECONDS", "11.5")
    monkeypatch.setenv("LITBENCH_MCP_MAX_TEXT_CHARS", "400")
    monkeypatch.setenv("LITBENCH_MCP_REQUIRE_TOKEN", "true")
    settings = Settings.from_env()
    assert settings.api_base == "http://127.0.0.1:9999"
    assert settings.api_token == "internal-token-123456"
    assert settings.state_dir.name == "custom-mcp"
    assert settings.rate_limit == 7
    assert settings.rate_window_s == 11.5
    assert settings.max_text_chars == 400
    assert settings.require_token is True


def test_api_token_defaults_to_empty() -> None:
    assert Settings().api_token == ""


def test_api_token_rejects_short_and_bad_chars() -> None:
    with pytest.raises(ConfigError, match="太短"):
        Settings(api_token="short")
    with pytest.raises(ConfigError, match="ASCII"):
        Settings(api_token="internal token with space 123456")


def test_bad_env_value_is_readable_error(monkeypatch) -> None:
    monkeypatch.setenv("LITBENCH_MCP_RATE_LIMIT", "many")
    with pytest.raises(ConfigError, match="LITBENCH_MCP_RATE_LIMIT"):
        Settings.from_env()


def test_invalid_api_base() -> None:
    with pytest.raises(ConfigError, match="http"):
        Settings(api_base="127.0.0.1:8000")


def test_invalid_transport() -> None:
    with pytest.raises(ConfigError, match="transport"):
        Settings(transport="grpc")  # type: ignore[arg-type]


def test_invalid_rate_limit() -> None:
    with pytest.raises(ConfigError, match="限流"):
        Settings(rate_limit=0)


def test_with_overrides_ignores_none() -> None:
    settings = Settings()
    same = settings.with_overrides(api=None, port=None)
    assert same is settings
    changed = settings.with_overrides(port=9000)
    assert changed.port == 9000
    assert changed.state_dir == settings.state_dir


def test_mcp_path_normalized() -> None:
    settings = Settings(mcp_path="mcp/")
    assert settings.mcp_path == "/mcp"


def test_state_dir_paths_are_absolute(tmp_path) -> None:
    settings = Settings(state_dir=Path(tmp_path))
    assert settings.tokens_path == tmp_path / "tokens.json"
    assert settings.audit_path == tmp_path / "audit.jsonl"
