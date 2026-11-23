# SPDX-License-Identifier: AGPL-3.0-or-later
"""批 F 清理的回归：死配置已接线/删除、seed_dev 安全护栏、依赖不再包含 pgvector。"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from litbench import adapters
from litbench.config import Settings, settings
from litbench.labeling import TextQuality, needs_text_layer_audit

ROOT = Path(__file__).resolve().parents[2]


def test_text_quality_min_threshold_is_respected() -> None:
    doc = SimpleNamespace(text_layer=True, figures=[])
    quality = TextQuality(score=0.5)
    assert needs_text_layer_audit(doc, quality, min_score=0.6) is True
    assert needs_text_layer_audit(doc, quality, min_score=0.4) is False


def test_docling_ocr_lang_is_forwarded(monkeypatch) -> None:
    captured: dict = {}

    class FakeDocling:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)

    monkeypatch.setattr(adapters, "DoclingParser", FakeDocling)
    adapters.get_parser("docling", ocr=True, ocr_lang="english")
    assert captured["ocr"] is True
    assert captured["ocr_lang"] == "english"


def test_dead_infra_config_removed() -> None:
    # valkey 从未被使用：配置项已删，compose 也不再起 valkey 容器
    assert "valkey_url" not in Settings.model_fields
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "valkey" not in compose.lower()
    assert "WORKER_CONCURRENCY" not in compose
    assert "postgres:17" in compose

    # pgvector 没有向量用法：从依赖里移除
    requirements = (ROOT / "server/requirements.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "server/pyproject.toml").read_text(encoding="utf-8")
    assert "pgvector" not in requirements
    assert "pgvector" not in pyproject


def test_seed_dev_refuses_foreign_db(tmp_path) -> None:
    """seed_dev 只允许 .data/dev.db；传别的路径必须拒绝（防止误清真实库）。"""

    other = tmp_path / "real.db"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/seed_dev.py"), "--db", str(other)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "拒绝" in (proc.stdout + proc.stderr)


def test_gc_orphans_dry_run_is_default() -> None:
    """gc_orphans 默认 dry-run：不给 --apply 时不能删数据。"""
    script = (ROOT / "scripts/gc_orphans.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--apply", action="store_true"' in script
    assert 'if not args.apply:' in script


def test_settings_still_has_docling_ocr_lang() -> None:
    assert settings.docling_ocr_lang
    assert settings.text_quality_min > 0
