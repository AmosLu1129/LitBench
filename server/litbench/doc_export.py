# SPDX-License-Identifier: AGPL-3.0-or-later
"""Markdown 草稿 → Word / PDF 导出（M8，2026-11-16）。

链路：写作台 claim 卡片 → Markdown（标题 + 章节 + 正文 + pandoc 引用）
→ ``pandoc --citeproc``（官方 CSL 样式）→ ``.docx`` / ``.pdf``。

为什么 PDF 用 typst 而不是 xelatex：

* xelatex 由 API 进程（Python subprocess）拉起时，MiKTeX 25 会写不了自己的
  ``%LOCALAPPDATA%\\MiKTeX\\miktex\\log``（权限/沙箱），直接判失败、不产 PDF；
  这是本项目已验证过的限制（`docs/09` §0.6），不要试图绕过；
* typst 是单文件 Rust 二进制（MIT），由 pandoc 以子进程调用，没有 TeX 安装/字体
  数据库依赖；中文由 typst 的字体回退渲染（Windows 用系统字体，镜像里装 Noto CJK）。
  本机实测 pandoc 3.14 + typst 0.15.1 生成中文 PDF，正文/参考文献/引用编号都正常。

边界：

* 正文只来自 claim 卡片（和 LaTeX/Markdown 导出同一批数据），不自动写正文；
* 引用键来自 claim 关联文献；claim 文本里手写的 ``[@key]`` 必须能在库内解析，
  否则 422（不静默丢掉引用）；
* 输出是**草稿**：默认 Word 样式/字体/页边距，不做学校模板；
* pandoc 缺失 → 503；PDF 还需要 typst（缺失 → 503）；渲染失败 → 502。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import csl
from .config import settings
from .models import AppUser, Claim, Paper
from .toolchain import executable_name, find_tool
from .writing import (
    _CITE_RE,
    DEFAULT_SECTION,
    MAX_EXPORT_CLAIMS,
    _load_export_claims,
    _paper_cite_key,
    _resolve_key,
    _safe_filename,
    visible_paper_index,
)

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PDF_MIME = "application/pdf"

_DOCX_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"
# pandoc 引用：[@key]、[@k1; @k2]、[@key, p. 3] 都认；正文里的其它 [x@y] 不动
_BRACKET_CITE_RE = re.compile(r"\[([^\[\]]*@[^\[\]]*)\]")


class DocExportError(RuntimeError):
    """可读的导出失败（不会抛到 HTTP 500）。"""


class DocExportUnavailable(DocExportError):
    """环境缺 pandoc / typst；功能不可用但模板导出不受影响。"""


class DocExportFailed(DocExportError):
    """pandoc/引擎已经启动，但渲染失败、超时或产物无效（HTTP 502）。"""


# ---------------------------------------------------------------------------
# 外部工具定位（pandoc 与 typst 共用 toolchain；版本门槛见 csl）
# ---------------------------------------------------------------------------
def find_typst() -> str | None:
    return find_tool("typst", settings.typst_path, "typst", executable_name("typst"))


def export_capabilities() -> dict[str, Any]:
    """给前端/健康检查用：Word/PDF 各自是否可用。"""
    pandoc = csl.find_pandoc()
    pandoc_version = csl.pandoc_version(pandoc) if pandoc is not None else None
    pandoc_ready = pandoc is not None and csl.pandoc_is_supported(pandoc)
    typst = find_typst()
    docx_ready = settings.docx_export_enabled and pandoc_ready
    pdf_ready = settings.pdf_export_enabled and pandoc_ready and typst is not None
    notes: list[str] = []
    if pandoc is None:
        notes.append("未找到 pandoc：Word/PDF 导出不可用（scripts/get_pandoc.py）")
    elif not pandoc_ready:
        shown = (
            f"{pandoc_version[0]}.{pandoc_version[1]}"
            if pandoc_version is not None
            else "未知版本"
        )
        notes.append(f"pandoc 版本过低或无法读取（{shown}，需要 ≥3.0）：Word/PDF 导出不可用")
    elif not typst:
        notes.append("未找到 typst：PDF 导出不可用（scripts/get_typst.py）")
    if not settings.docx_export_enabled:
        notes.append("Word 导出已被配置关闭")
    if not settings.pdf_export_enabled:
        notes.append("PDF 导出已被配置关闭")
    return {
        "docx": docx_ready,
        "pdf": pdf_ready,
        "pandoc": pandoc_ready,
        "typst": typst is not None,
        "note": "；".join(notes),
    }


# ---------------------------------------------------------------------------
# 草稿 Markdown
# ---------------------------------------------------------------------------
def _section_order(
    groups: dict[str, list[tuple[Claim, Paper]]], order: list[str] | None
) -> list[str]:
    result: list[str] = []
    for section in order or []:
        if section in groups and section not in result:
            result.append(section)
    for section in groups:
        if section not in result:
            result.append(section)
    return result


def build_draft_markdown(
    rows: list[tuple[Claim, Paper]],
    *,
    title: str,
    section_order: list[str] | None = None,
) -> tuple[str, dict[str, Paper]]:
    """claim 行 → (markdown, 引用到的 paper.id → Paper)。

    引用统一写成 ``[@<paper.id>]``（pandoc 的 citation key = CSL JSON 的 id），
    这样官方 CSL 样式能正确编号；claim 文本里手写的 ``[@cite_key]`` 会在
    :func:`_rewrite_citations` 里换成对应 id。
    """
    groups: dict[str, list[tuple[Claim, Paper]]] = {}
    papers: dict[str, Paper] = {}
    for claim, paper in rows:
        section = (claim.section or DEFAULT_SECTION).strip() or DEFAULT_SECTION
        groups.setdefault(section, []).append((claim, paper))
        papers.setdefault(paper.id, paper)
    safe_title = title.strip() or "LitBench 写作草稿"
    lines = [
        "---",
        f"title: {json.dumps(safe_title, ensure_ascii=False)}",
        # 被选进参考文献范围、但正文没显式引用的文献也进参考文献表
        'nocite: "@*"',
        "---",
        "",
    ]
    for section in _section_order(groups, section_order):
        lines.append(f"## {section}")
        lines.append("")
        for claim, paper in groups[section]:
            text = (claim.text or "").strip()
            if text and paper is not None:
                cite_key = _paper_cite_key(paper)
                if cite_key and "\\cite" not in text and "[@" not in text:
                    text = f"{text} [@{paper.id}]"
            lines.append(text)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n", papers


def _rewrite_citations(
    markdown: str,
    papers: dict[str, Paper],
    visible_index: list | None = None,
) -> tuple[str, set[str]]:
    """把 ``\\cite{k1,k2}`` / ``[@k1; @k2]`` / ``[@k]`` 换成 paper.id。

    返回 ``(markdown, cited_ids)``：cited_ids 用于把正文手写引用的可见文献补进
    参考文献表。未知键直接报错（不静默丢引用）。
    """
    all_papers = list(papers.values()) + list(visible_index or [])
    known_ids = set(papers) | {paper.id for paper in (visible_index or [])}
    unknown: list[str] = []
    cited_ids: set[str] = set()

    def resolve(key: str) -> str | None:
        if key in known_ids:
            cited_ids.add(key)
            return key
        paper = _resolve_key(all_papers, key)
        if paper is None:
            return None
        cited_ids.add(paper.id)
        return paper.id

    def latex_repl(match: re.Match[str]) -> str:
        keys = [raw.strip() for raw in match.group(1).split(",") if raw.strip()]
        ids: list[str] = []
        for key in keys:
            paper_id = resolve(key)
            if paper_id is None:
                unknown.append(key)
                return match.group(0)
            ids.append(paper_id)
        return "[" + "; ".join(f"@{paper_id}" for paper_id in ids) + "]"

    text = _CITE_RE.sub(latex_repl, markdown)

    def bracket_repl(match: re.Match[str]) -> str:
        parts = [part.strip() for part in match.group(1).split(";")]
        if not parts or any(not part.startswith("@") for part in parts):
            return match.group(0)
        rebuilt: list[str] = []
        for part in parts:
            body = part[1:]
            key, comma, locator = body.partition(",")
            paper_id = resolve(key.strip())
            if paper_id is None:
                unknown.append(key.strip())
                return match.group(0)
            rebuilt.append("@" + paper_id + ("," + locator if comma else ""))
        return "[" + "; ".join(rebuilt) + "]"

    text = _BRACKET_CITE_RE.sub(bracket_repl, text)
    if unknown:
        raise DocExportError(
            "稿子里有无法解析的引用键："
            + "、".join(sorted(set(unknown)))
            + "。请改成库内文献的 cite_key，或删掉手写引用。"
        )
    return text, cited_ids


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _pandoc_env(typst: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if typst:
        # pandoc 按名字找 --pdf-engine=typst，需要把 typst 所在目录放进 PATH
        directory = str(Path(typst).parent)
        env["PATH"] = directory + os.pathsep + env.get("PATH", "")
    return env


def _run_pandoc(
    *,
    pandoc: str,
    typst: str | None,
    workdir: Path,
    markdown_path: Path,
    refs_path: Path,
    style_path: Path,
    target: Path,
    fmt: str,
) -> None:
    command = [
        pandoc,
        str(markdown_path),
        "--citeproc",
        f"--bibliography={refs_path}",
        f"--csl={style_path}",
        # claim 文本是用户输入；sandbox 禁止 pandoc 读取/抓取正文里引用的任意本地或远程资源
        "--sandbox",
        "--wrap=none",
        "-o",
        str(target),
    ]
    if fmt == "pdf":
        command.extend(["--pdf-engine=typst"])
        if settings.pdf_main_font.strip():
            command.extend(["-V", f"mainfont={settings.pdf_main_font.strip()}"])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.document_export_timeout_s,
            stdin=subprocess.DEVNULL,
            cwd=workdir,
            env=_pandoc_env(typst),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocExportFailed(
            f"pandoc 渲染超时（>{settings.document_export_timeout_s:g}s）"
        ) from exc
    except OSError as exc:
        raise DocExportUnavailable(f"无法启动 pandoc：{exc}") from exc
    if result.returncode != 0:
        stderr_lines = [
            line.strip() for line in (result.stderr or "").splitlines() if line.strip()
        ]
        detail = stderr_lines[-1] if stderr_lines else "无错误输出"
        # stderr 可能带绝对路径/乱码；只取最后一行给用户，完整内容不落库
        raise DocExportFailed(f"pandoc 渲染失败（退出码 {result.returncode}）：{detail}")


def _safe_download_name(title: str, fmt: str) -> str:
    return f"{_safe_filename(title)}.{fmt}"


def _ensure_workdir() -> Path:
    """导出临时目录（放 storage_dir 下，Windows 沙箱/权限比系统 %TEMP% 稳）。

    测试/API 从不同目录启动时 storage_dir 可能还不存在，这里先建再交给
    ``TemporaryDirectory``。
    """
    workdir = settings.storage_dir
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def export_document(
    session: AsyncSession,
    *,
    fmt: str,
    paper_ids: list[str] | None = None,
    claim_ids: list[str] | None = None,
    title: str = "",
    section_order: list[str] | None = None,
    include_drafts: bool = True,
    csl_style: str = "gbt7714",
    owner_user_id: str | None = None,
    user: AppUser | None = None,
) -> dict[str, Any]:
    """导出 Word/PDF；返回 ``{content: bytes, filename, media_type, ...}``。"""
    fmt = (fmt or "").strip().lower()
    if fmt not in ("docx", "pdf"):
        raise DocExportError("format 只支持 docx / pdf")
    if fmt == "docx" and not settings.docx_export_enabled:
        raise DocExportUnavailable("Word 导出已被配置关闭（DOCX_EXPORT_ENABLED=0）")
    if fmt == "pdf" and not settings.pdf_export_enabled:
        raise DocExportUnavailable("PDF 导出已被配置关闭（PDF_EXPORT_ENABLED=0）")
    pandoc = csl.find_pandoc()
    if pandoc is None:
        raise DocExportUnavailable(
            "Word/PDF 导出需要 pandoc：没找到可执行的 pandoc"
            "（安装 pandoc 或设 PANDOC_PATH；模板导出不受影响）"
        )
    if not csl.pandoc_is_supported(pandoc):
        raise DocExportUnavailable(
            "Word/PDF 导出需要 pandoc ≥3.0（--sandbox / --citeproc）；"
            "当前 pandoc 版本过低或无法读取，请用 scripts/get_pandoc.py "
            "或设 PANDOC_PATH 指向 3.x"
        )
    typst = find_typst() if fmt == "pdf" else None
    if fmt == "pdf" and typst is None:
        raise DocExportUnavailable(
            "PDF 导出需要 typst：没找到可执行的 typst"
            "（scripts/get_typst.py 下载，或设 TYPST_PATH）"
        )
    style_path = csl.style_file(csl_style)
    if style_path is None:
        raise DocExportError(
            f"格式 {csl_style!r} 没有 CSL 样式；可用：{', '.join(csl.csl_style_keys())}"
        )

    rows = await _load_export_claims(
        session,
        claim_ids=claim_ids,
        paper_ids=paper_ids,
        include_drafts=include_drafts,
        owner_user_id=owner_user_id,
        user=user,
    )
    if len(rows) > MAX_EXPORT_CLAIMS:
        raise DocExportError(
            f"一次最多导出 {MAX_EXPORT_CLAIMS} 张卡片；请用 claim_ids / paper_ids 缩小范围"
        )
    if not rows:
        raise DocExportError("没有可导出的 claim；先写几张卡片，或放宽「包含草稿」")
    markdown, papers = build_draft_markdown(
        rows, title=title, section_order=section_order
    )
    # 额外文献（只勾了参考文献范围）也要先进 papers，正文手写引用才解析得到
    if paper_ids:
        extra = (
            await session.execute(select(Paper).where(Paper.id.in_(paper_ids)))
        ).scalars()
        for paper in extra:
            papers.setdefault(paper.id, paper)
    # 正文可能手写引用其余可见文献：先按轻量索引解析，再把引用到的完整 Paper 补进 bib
    index = await visible_paper_index(session, user)
    markdown, cited_ids = _rewrite_citations(markdown, papers, index)
    missing_ids = cited_ids - set(papers)
    if missing_ids:
        cited = (
            await session.execute(select(Paper).where(Paper.id.in_(missing_ids)))
        ).scalars()
        for paper in cited:
            papers.setdefault(paper.id, paper)

    try:
        temp_root = _ensure_workdir()
    except OSError as exc:
        raise DocExportFailed(f"导出临时目录不可写：{exc}") from exc
    with tempfile.TemporaryDirectory(prefix="litbench-doc-", dir=str(temp_root)) as tmp:
        workdir = Path(tmp)
        markdown_path = workdir / "draft.md"
        refs_path = workdir / "refs.json"
        target = workdir / f"draft.{fmt}"
        markdown_path.write_text(markdown, encoding="utf-8")
        refs_path.write_text(
            json.dumps(csl.export_csl_json(list(papers.values())), ensure_ascii=False),
            encoding="utf-8",
        )
        await asyncio.to_thread(
            _run_pandoc,
            pandoc=pandoc,
            typst=typst,
            workdir=workdir,
            markdown_path=markdown_path,
            refs_path=refs_path,
            style_path=style_path,
            target=target,
            fmt=fmt,
        )
        content = target.read_bytes() if target.is_file() else b""
    magic = _DOCX_MAGIC if fmt == "docx" else _PDF_MAGIC
    if not content.startswith(magic):
        raise DocExportFailed(
            f"pandoc 没有产出有效的 .{fmt}（{len(content)} 字节）；请检查 pandoc/PDF 引擎"
        )
    return {
        "content": content,
        "filename": _safe_download_name(title or "LitBench 写作草稿", fmt),
        "media_type": DOCX_MIME if fmt == "docx" else PDF_MIME,
        "claim_count": len(rows),
        "paper_count": len(papers),
    }


__all__ = [
    "DOCX_MIME",
    "PDF_MIME",
    "DocExportError",
    "DocExportFailed",
    "DocExportUnavailable",
    "build_draft_markdown",
    "export_capabilities",
    "export_document",
    "find_typst",
]
