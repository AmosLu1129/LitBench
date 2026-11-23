# SPDX-License-Identifier: AGPL-3.0-or-later
"""下载便携版 typst 到 ``.data/tools/typst/``（M8 PDF 导出用的排版引擎）。

用法（仓库根目录）::

    .venv\\Scripts\\python.exe scripts\\get_typst.py
    .venv\\Scripts\\python.exe scripts\\get_typst.py --force
    .venv\\Scripts\\python.exe scripts\\get_typst.py --mirror https://ghproxy.net/

为什么 PDF 用 typst：API 进程（Python subprocess）拉起 xelatex 时，MiKTeX 25 写不了
自己的日志目录（权限/沙箱），直接失败且不产 PDF（见 docs/09 §0.6）。typst 是单文件
Rust 二进制（MIT），pandoc 以子进程调用即可，不依赖 TeX 安装或字体数据库；中文由
typst 的字体回退渲染（Windows 系统字体；Docker 镜像里装 Noto CJK）。

GitHub 直连不稳时脚本会逐个尝试镜像；SHA-256 始终校验。换版本要同时更新
``VERSION`` 与 ``ASSETS``。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx

VERSION = "0.15.1"
RELEASE_TAG = f"v{VERSION}"

# (sys.platform, 标准化 machine) → (asset 文件名, sha256)
ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("win32", "x86_64"): (
        "typst-x86_64-pc-windows-msvc.zip",
        "19ce3551153c2fe7ee9fa2f95208310c8f4d3209fedb699e0333faf8913f6736",
    ),
    ("linux", "x86_64"): (
        "typst-x86_64-unknown-linux-musl.tar.xz",
        "a6d077d0a95eed5a2eba715b2dae06be954f624ccbf85758a03f389ded33118c",
    ),
    ("linux", "arm64"): (
        "typst-aarch64-unknown-linux-musl.tar.xz",
        "5aa8d74a3d906e60ea12a66ac2f37f8eef1b14cbad7182a745e393a10c23dcee",
    ),
}

# GitHub 直连不稳时的镜像前缀（国内可用；拼在原 URL 前）
MIRRORS = ("", "https://ghproxy.net/", "https://ghfast.top/")


def _machine() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return machine


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _download(url: str, mirror: str) -> bytes:
    print(f"下载 {mirror or 'GitHub 直连'}")
    with httpx.stream(
        "GET", mirror + url, follow_redirects=True, timeout=300
    ) as response:
        response.raise_for_status()
        return b"".join(response.iter_bytes())


def _fetch(url: str) -> bytes:
    errors: list[str] = []
    for mirror in MIRRORS:
        try:
            return _download(url, mirror)
        except Exception as exc:  # noqa: BLE001 - 网络错误换镜像重试
            errors.append(f"{mirror or 'direct'}: {type(exc).__name__}")
    raise SystemExit("所有下载源都失败：" + "；".join(errors))


def _extract(asset: str, payload: bytes, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if destination.resolve() not in target.parents:
                    raise SystemExit(f"压缩包路径非法：{member.filename}")
            archive.extractall(destination)
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                if destination.resolve() not in target.parents:
                    raise SystemExit(f"压缩包路径非法：{member.name}")
            if hasattr(tarfile, "data_filter"):
                archive.extractall(destination, filter="data")
            else:
                archive.extractall(destination)
    executable = "typst.exe" if sys.platform == "win32" else "typst"
    matches = sorted(destination.glob(f"*/{executable}"))
    if not matches:
        raise SystemExit(f"解压后没找到 {executable}（目录：{destination}）")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载便携版 typst（PDF 导出引擎）")
    parser.add_argument(
        "--dest", default=None, help="解压目录（默认 <仓库>/.data/tools/typst）"
    )
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument(
        "--mirror",
        default="",
        help="额外优先尝试的下载镜像前缀，如 https://ghproxy.net/",
    )
    args = parser.parse_args(argv)

    key = (sys.platform, _machine())
    if key not in ASSETS:
        raise SystemExit(
            f"没有 {sys.platform}/{_machine()} 的预置下载；请从 "
            "https://github.com/typst/typst/releases 手动安装 typst 并设 TYPST_PATH"
        )
    asset, expected_sha = ASSETS[key]
    destination = (
        Path(args.dest).expanduser()
        if args.dest
        else _repository_root() / ".data" / "tools" / "typst"
    )
    executable_name = "typst.exe" if sys.platform == "win32" else "typst"
    existing = sorted(destination.glob(f"*/{executable_name}"))
    if existing and not args.force:
        print(f"已存在：{existing[0]}")
        return 0

    url = (
        "https://github.com/typst/typst/releases/download/"
        f"{RELEASE_TAG}/{asset}"
    )
    if args.mirror:
        data = _download(url, args.mirror.rstrip("/") + "/")
    else:
        data = _fetch(url)
    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != expected_sha:
        raise SystemExit(f"SHA-256 不匹配：期望 {expected_sha}，实际 {actual_sha}")
    print(f"SHA-256 校验通过（{len(data) / 1024 / 1024:.1f} MB）")
    extracted = _extract(asset, data, destination)
    print(f"typst 已就位：{extracted}")
    print("API 不需要改 PATH：find_typst() 会自动在 .data/tools/typst/*/ 下找。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
