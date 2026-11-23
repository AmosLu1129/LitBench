# SPDX-License-Identifier: AGPL-3.0-or-later
"""外部工具链：定位 + 版本门槛（自修批 C，2026-11-16）。

背景：``csl.py`` 与 ``doc_export.py`` 原来各自实现「配置 → 项目内钉版本 → PATH」
的查找，并且只判断「文件存在」不判断版本。系统 apt 里常见的 pandoc 2.x 会让
``--sandbox`` / 新 CLI 行为在运行期才报错；这里固定两件事：

1. 查找逻辑收敛到 ``toolchain.find_tool``（csl 与 doc_export 共用）；
2. pandoc ≥ 3.0 才可用，低于门槛时 CSL/Word/PDF 给出可读的 503 说明，模板导出不受影响。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from litbench import csl, doc_export, toolchain


def _proc(stdout: str, code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


def test_tool_version_parses_and_caches(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "pandoc"
    exe.write_bytes(b"fake")
    calls: list[tuple] = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return _proc("pandoc 3.14\nFeatures: +server +lua\n")

    monkeypatch.setattr(toolchain.subprocess, "run", fake_run)
    assert toolchain.tool_version(str(exe)) == (3, 14)
    assert toolchain.tool_version(str(exe)) == (3, 14)  # 第二次走缓存
    assert len(calls) == 1


def test_tool_version_bad_output_or_nonzero(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "pandoc"
    exe.write_bytes(b"fake")
    monkeypatch.setattr(
        toolchain.subprocess, "run", lambda *a, **k: _proc("not a version")
    )
    assert toolchain.tool_version(str(exe)) is None

    other = tmp_path / "pandoc2"
    other.write_bytes(b"fake")
    monkeypatch.setattr(
        toolchain.subprocess, "run", lambda *a, **k: _proc("pandoc 3.14", code=1)
    )
    assert toolchain.tool_version(str(other)) is None


def test_pandoc_version_gate_blocks_old_binary(monkeypatch) -> None:
    monkeypatch.setattr(csl, "find_pandoc", lambda: "/fake/pandoc")
    monkeypatch.setattr(csl, "pandoc_version", lambda path=None: (2, 19))

    assert csl.pandoc_is_supported() is False
    available, note = csl.csl_engine_status()
    assert available is False
    assert "版本过低" in note and "3.0" in note
    with pytest.raises(csl.CslUnavailable):
        csl.render_bibliography([], "gbt7714")

    caps = doc_export.export_capabilities()
    assert caps["docx"] is False
    assert caps["pandoc"] is False
    assert "版本过低" in caps["note"]


def test_pandoc_version_gate_accepts_modern_binary(monkeypatch) -> None:
    monkeypatch.setattr(csl, "find_pandoc", lambda: "/fake/pandoc")
    monkeypatch.setattr(csl, "pandoc_version", lambda path=None: (3, 14))
    assert csl.pandoc_is_supported() is True
    available, note = csl.csl_engine_status()
    assert available is True
    assert "pandoc" in note
