# SPDX-License-Identifier: AGPL-3.0-or-later
"""全局测试配置：默认关闭认证。

团队共享（M7）引入账号/会话后，几乎所有既有 API 测试都不带 cookie。
认证默认关闭（``LITBENCH_AUTH=0``），测试也保持关闭；专门的认证测试会自己打开它。
"""

from __future__ import annotations

import pytest

from litbench.config import settings


@pytest.fixture(autouse=True)
def _disable_auth_by_default(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    # 轻量 OCR 会懒加载 RapidOCR 模型；常规测试默认关掉，专门测试自己 monkeypatch 打开，
    # 避免每个测试都真跑一遍模型/图表。
    monkeypatch.setattr(settings, "lite_ocr_enabled", False)
    monkeypatch.setattr(settings, "ocr_cross_validate", False)
    yield
