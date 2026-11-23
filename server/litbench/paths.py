# SPDX-License-Identifier: AGPL-3.0-or-later
"""文件路径守卫。

**凡是把「库里的路径字符串」变成「读文件」的地方，都必须过这里。**
库是可被导入、被迁移、被人手改的；一处漏校验就是任意文件读取。

写库时的约定（``to_storage_relative``）：

- 一律存**相对 storage 根**的路径，用 ``/`` 分隔。这样换机器、换挂载点
  （``D:/.data`` → 容器里的 ``/data``）不用改库；存绝对路径则一换环境就全 404。
- 读的时候两种都认：相对路径按 storage 根解释，绝对路径原样校验。
  老数据（seed 脚本写的是绝对路径）因此不用迁移。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException


def resolve_data_path(root: Path, raw_path: str) -> Path:
    """把库里的路径解析成绝对路径，并确认没有越界。不检查文件是否存在。

    越界抛 ``PermissionError``，路径非法抛 ``ValueError``——**不发 HTTP 异常**，
    这样 worker、脚本也能复用它，由调用方决定怎么翻译成自己的错误类型。
    """
    p = Path(raw_path)
    target = p if p.is_absolute() else root / p
    try:
        resolved = target.resolve()
        allowed = root.resolve()
    except OSError as exc:  # pragma: no cover — 路径非法（含 NUL 等）
        raise ValueError(f"invalid path: {raw_path!r}") from exc

    if resolved != allowed and allowed not in resolved.parents:
        raise PermissionError(f"path outside storage root: {raw_path!r}")
    return resolved


def resolve_within(root: Path, raw_path: str) -> Path:
    """``resolve_data_path`` 的 HTTP 版本：越界 403、缺失 404。"""
    try:
        resolved = resolve_data_path(root, raw_path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="path outside storage root") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return resolved


def resolve_within_subdir(root: Path, raw_path: str, subdir: str) -> Path:
    """解析路径并要求它落在 ``root/subdir`` 里。

    库里的路径统一按 **storage 根**解释（不管是 PDF 还是图片），这里再加一道
    「只允许读某个子目录」的收窄。别让调用方各自解释相对路径——图片接口曾经
    按 ``image_dir`` 解释、解析器按 storage 根写，前缀叠一层就全 404。
    """
    resolved = resolve_data_path(root, raw_path)  # 越界抛 PermissionError
    allowed = (root / subdir).resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise HTTPException(status_code=403, detail="path outside storage subdir")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return resolved


def to_storage_relative(root: Path, path: Path) -> str:
    """写库前把路径转成「相对 storage 根 + POSIX 分隔符」。

    不在 storage 下的路径（理论上不会出现）退回绝对形式，宁可难读也不能丢信息。
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()
