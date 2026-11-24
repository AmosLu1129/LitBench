# SPDX-License-Identifier: AGPL-3.0-or-later
"""外部工具（pandoc / typst）的定位与版本检查。

为什么单独一层：``csl.py`` 与 ``doc_export.py`` 各自实现过一遍「配置 → 项目内钉版本
→ PATH」的查找逻辑，容易出现「一个改了另一个没改」。这里收敛成一份，并补上
**版本门槛**：``--sandbox`` / ``--citeproc`` 需要 pandoc ≥ 3.0，系统里 apt 装的老版
pandoc 会让导出在运行期才报错；现在启动/请求时就能给出可读的「版本过低」。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import settings

# 仓库根目录（server/litbench/toolchain.py → parents[2]），只用于开发期找 .data/tools
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
# 结果缓存：同一个可执行文件不重复 fork；文件被替换（mtime 变）时自动失效。
_VERSION_CACHE: dict[tuple[str, int], tuple[int, int] | None] = {}

MIN_PANDOC_VERSION = (3, 0)


def executable_name(name: str) -> str:
    """按平台补 .exe（Windows）。"""
    return f"{name}.exe" if os.name == "nt" else name


def find_tool(
    name: str,
    configured: str | None,
    subdir: str,
    executable: str,
) -> str | None:
    """配置 → 项目内钉版本 → PATH（项目内优先，避免 PATH 上的旧版本）。"""
    configured = (configured or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)
        return shutil.which(configured)
    bundled: list[Path] = []
    for tools in (
        settings.storage_dir / "tools" / subdir,
        _PROJECT_ROOT / ".data" / "tools" / subdir,
    ):
        if tools.is_dir():
            for pattern in (f"*/{executable}", f"*/bin/{executable}"):
                bundled.extend(
                    path for path in tools.glob(pattern) if path.is_file()
                )
    if bundled:
        # 多个版本共存时用最新的（按修改时间，不按字符串排序，3.14 要排在 3.9 前）
        return str(max(bundled, key=lambda path: path.stat().st_mtime))
    return shutil.which(name)


def tool_version(path: str, *, timeout_s: float = 5.0) -> tuple[int, int] | None:
    """跑 ``path --version`` 取前两段版本号；失败/不可解析返回 None。"""
    try:
        stat = Path(path).stat()
        cache_key = (str(Path(path).resolve()), stat.st_mtime_ns)
    except OSError:
        cache_key = (path, 0)
    if cache_key in _VERSION_CACHE:
        return _VERSION_CACHE[cache_key]
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _VERSION_CACHE[cache_key] = None
        return None
    match = _VERSION_RE.search(proc.stdout or "")
    version = (int(match.group(1)), int(match.group(2))) if match else None
    if proc.returncode != 0:
        version = None
    _VERSION_CACHE[cache_key] = version
    return version


__all__ = [
    "MIN_PANDOC_VERSION",
    "executable_name",
    "find_tool",
    "tool_version",
]
