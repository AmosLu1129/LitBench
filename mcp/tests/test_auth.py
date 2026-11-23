# SPDX-License-Identifier: MIT
"""token / scope 管理与 CLI。"""

from __future__ import annotations

import hashlib
import json

import pytest

from litbench_mcp.auth import (
    READ_SCOPE,
    TOKEN_PREFIX,
    TokenError,
    TokenStore,
)
from litbench_mcp.cli import main


def test_issue_stores_only_sha256(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    issued = store.issue(scope=READ_SCOPE, label="dsh")

    assert issued.token.startswith(TOKEN_PREFIX)
    assert len(issued.token) > 40
    assert issued.rotated is False
    assert issued.record.label == "dsh"
    assert issued.record.scopes == (READ_SCOPE,)
    assert issued.record.created_at

    raw_text = store.path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    assert payload["version"] == 1
    record = payload["tokens"][0]
    assert set(record) == {"label", "scopes", "created_at", "token_sha256"}
    assert record["token_sha256"] == hashlib.sha256(issued.token.encode()).hexdigest()
    # 明文绝不落盘
    assert issued.token not in raw_text


def test_verify_and_scope(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    issued = store.issue(scope=READ_SCOPE, label="dsh")

    record = store.verify(issued.token)
    assert record is not None
    assert record.label == "dsh"
    assert READ_SCOPE in record.scopes

    assert store.verify("lbmcp_wrong") is None
    assert store.verify("") is None


async def test_verify_token_implements_sdk_access_token(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    issued = store.issue(scope=READ_SCOPE, label="dsh")
    access = await store.verify_token(issued.token)
    assert access is not None
    assert access.client_id == "dsh"
    assert access.scopes == [READ_SCOPE]
    assert access.claims == {"mcp_token_label": "dsh"}
    assert await store.verify_token("bad") is None


def test_list_does_not_expose_secret_or_hash(tmp_path, capsys) -> None:
    state = tmp_path / "state"
    store = TokenStore(state / "tokens.json")
    issued = store.issue(scope=READ_SCOPE, label="dsh")
    capsys.readouterr()  # 丢掉签发输出

    code = main(["token", "list", "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert code == 0
    assert "dsh" in out
    assert READ_SCOPE in out
    assert issued.token not in out
    assert issued.record.token_sha256 not in out


def test_revoke(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    issued = store.issue(scope=READ_SCOPE, label="dsh")
    removed = store.revoke("dsh")
    assert removed.label == "dsh"
    assert store.verify(issued.token) is None
    assert store.list() == []

    with pytest.raises(TokenError, match="没有 label"):
        store.revoke("dsh")


def test_reissue_same_label_rotates(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    first = store.issue(scope=READ_SCOPE, label="dsh")
    second = store.issue(scope=READ_SCOPE, label="dsh")
    assert second.rotated is True
    assert store.verify(first.token) is None
    assert store.verify(second.token) is not None
    assert len(store.list()) == 1


def test_issue_rejects_unknown_scope_and_empty_label(tmp_path) -> None:
    store = TokenStore(tmp_path / "tokens.json")
    with pytest.raises(TokenError, match="scope"):
        store.issue(scope="root", label="dsh")
    with pytest.raises(TokenError, match="label"):
        store.issue(scope=READ_SCOPE, label="  ")


def test_corrupt_token_file_is_readable_error(tmp_path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("{not-json", encoding="utf-8")
    store = TokenStore(path)
    with pytest.raises(TokenError, match="token 文件"):
        store.list()


def test_cli_issue_prints_plaintext_once_and_revoke(tmp_path, capsys) -> None:
    state = tmp_path / "state"
    code = main(["token", "issue", "--label", "dsh", "--scope", READ_SCOPE, "--state-dir", str(state)])
    captured = capsys.readouterr()
    assert code == 0
    token = captured.out.strip()
    assert token.startswith(TOKEN_PREFIX)
    # 明文只在 stdout 出现一次；stderr 只有提示
    assert token not in captured.err
    assert token not in (state / "tokens.json").read_text(encoding="utf-8")

    code = main(["token", "revoke", "dsh", "--state-dir", str(state)])
    captured = capsys.readouterr()
    assert code == 0
    assert "已撤销" in captured.err
    assert TokenStore(state / "tokens.json").list() == []


def test_cli_issue_rotates_with_warning(tmp_path, capsys) -> None:
    state = tmp_path / "state"
    assert main(["token", "issue", "--label", "dsh", "--state-dir", str(state)]) == 0
    first = capsys.readouterr().out.strip()
    assert main(["token", "issue", "--label", "dsh", "--state-dir", str(state)]) == 0
    captured = capsys.readouterr()
    second = captured.out.strip()
    assert second != first
    assert "轮换" in captured.err


def test_cli_revoke_unknown_label_returns_2(tmp_path, capsys) -> None:
    state = tmp_path / "state"
    code = main(["token", "revoke", "nobody", "--state-dir", str(state)])
    captured = capsys.readouterr()
    assert code == 2
    assert "没有 label" in captured.err
