# SPDX-License-Identifier: AGPL-3.0-or-later
"""下载便携版 pandoc 到 ``.data/tools/pandoc/``（M8 CSL 引擎 / docx 导出用）。

用法（在仓库根目录）::

    .venv\\Scripts\\python.exe scripts\\get_pandoc.py
    .venv\\Scripts\\python.exe scripts\\get_pandoc.py --version 3.14 --force

为什么不把 pandoc 做成 pip 依赖：pandoc 是 Haskell 可执行文件（GPL-2.0），
只作独立子进程调用（见 THIRD_PARTY.md）；开发机用这个脚本取便携版，
Docker 镜像在 ``server/Dockerfile`` 里装同一版本。找不到 pandoc 时
CSL 引擎返回 503，13 种手写模板不受影响。

版本与 SHA-256 是钉死的：换版本时要同时更新本文件的 ``ASSETS``。
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

DEFAULT_VERSION = "3.14"

# (sys.platform, 标准化后的 machine) → (asset 文件名, sha256)
ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("win32", "x86_64"): (
        "pandoc-3.14-windows-x86_64.zip",
        "0144b96e8c7b023fd382ec5ef5bdc475e873f1019d6fa72502492df2c9a94a3b",
    ),
    ("linux", "x86_64"): (
        "pandoc-3.14-linux-amd64.tar.gz",
        "c5f4720cdefb3f7bdc5d07202ef579feef0d1370773c873804dd8fdaa8c4f1b8",
    ),
    ("linux", "arm64"): (
        "pandoc-3.14-linux-arm64.tar.gz",
        "c692f3d78a415741bbbac4cdbbbe26fa7edca1f43d2301b982fbf4009cc8cf7f",
    ),
    ("darwin", "arm64"): (
        "pandoc-3.14-arm64-macOS.zip",
        "58d6593884fec9d4424b641b92503c316d9c36decb2891ff8d73ae74dd3b6304",
    ),
    ("darwin", "x86_64"): (
        "pandoc-3.14-x86_64-macOS.zip",
        "891708579061e7b1c1a6ce0df30ebb3cb2b6d0ca045ed3c8554ab830c1e0c270",
    ),
}


def _machine() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return machine


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _download(url: str) -> bytes:
    print(f"下载 {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        chunks = []
        total = 0
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total // (5 * 1024 * 1024) != (total - len(chunk)) // (5 * 1024 * 1024):
                print(f"  {total / 1024 / 1024:.0f} MB…")
    return b"".join(chunks)


def _find_pandoc_in(root: Path) -> Path | None:
    """在解压目录中查找 pandoc（Windows 在顶层，Linux/macOS 在 bin/ 下）。"""
    executable = "pandoc.exe" if sys.platform == "win32" else "pandoc"
    matches = sorted(
        path
        for pattern in (f"*/{executable}", f"*/bin/{executable}")
        for path in root.glob(pattern)
        if path.is_file()
    )
    return matches[0] if matches else None


def _extract(asset: str, payload: bytes, destination: Path) -> Path:
    """解压到 destination，返回 pandoc 可执行文件路径（做路径穿越检查）。"""
    destination.mkdir(parents=True, exist_ok=True)
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if destination.resolve() not in target.parents:
                    raise SystemExit(f"压缩包路径非法：{member.filename}")
            archive.extractall(destination)
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(destination, filter="data")
            else:  # Python 3.11 兜底：手动检查
                for member in archive.getmembers():
                    target = (destination / member.name).resolve()
                    if destination.resolve() not in target.parents:
                        raise SystemExit(f"压缩包路径非法：{member.name}")
                archive.extractall(destination)
    extracted = _find_pandoc_in(destination)
    if extracted is None:
        executable = "pandoc.exe" if sys.platform == "win32" else "pandoc"
        raise SystemExit(f"解压后没找到 {executable}（目录：{destination}）")
    return extracted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载便携版 pandoc（CSL 引擎用）")
    parser.add_argument(
        "--version", default=DEFAULT_VERSION, help="pandoc 版本（默认 3.14）"
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="解压目录（默认 <仓库>/.data/tools/pandoc）",
    )
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args(argv)

    if args.version != DEFAULT_VERSION:
        raise SystemExit(
            f"本脚本只钉了 pandoc {DEFAULT_VERSION} 的 SHA-256；换版本请先更新 "
            "scripts/get_pandoc.py 的 ASSETS"
        )
    key = (sys.platform, _machine())
    if key not in ASSETS:
        raise SystemExit(
            f"没有 {sys.platform}/{_machine()} 的预置下载；请从 "
            "https://github.com/jgm/pandoc/releases 手动安装 pandoc 并设 PANDOC_PATH"
        )
    asset, expected_sha = ASSETS[key]
    destination = (
        Path(args.dest).expanduser()
        if args.dest
        else _repository_root() / ".data" / "tools" / "pandoc"
    )
    target_dir = destination / f"pandoc-{args.version}"
    exe_name = "pandoc.exe" if sys.platform == "win32" else "pandoc"
    existing = next(
        (p for p in (target_dir / exe_name, target_dir / "bin" / exe_name) if p.is_file()),
        None,
    )
    if existing is not None and not args.force:
        print(f"已存在：{existing}")
        print(f"找到的 pandoc：{existing}")
        return 0

    url = (
        "https://github.com/jgm/pandoc/releases/download/"
        f"{args.version}/{asset}"
    )
    payload = _download(url)
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != expected_sha:
        raise SystemExit(f"SHA-256 不匹配：期望 {expected_sha}，实际 {actual_sha}")
    print(f"SHA-256 校验通过（{len(payload) / 1024 / 1024:.1f} MB）")
    extracted = _extract(asset, payload, destination)
    print(f"pandoc 已就位：{extracted}")
    print("API 不需要改 PATH：find_pandoc() 会自动在 .data/tools/pandoc/*/（含 bin/）下找。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
