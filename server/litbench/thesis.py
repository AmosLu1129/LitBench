# SPDX-License-Identifier: AGPL-3.0-or-later
"""学位论文 LaTeX 骨架（M8，2026-11-15，docs/17 §11.3）。

目标不是「一键生成整篇论文」，而是把库里已有的两样东西装进一份能编译的
``ctexbook`` 骨架：

* **claim 卡片** → 章节正文（按 ``section`` 归到绪论/方法/实验等章）；
* **文献元数据** → ``refs.bib``（BibTeX 可编译）与 ``references_gbt.tex``
  （GB/T 7714 文本版，学校要求时替换）。

三条边界（与 docs/17 §11.3.6 一致）：

1. 不替用户写正文：claim 只作为骨架里的论断，正文由用户补；
2. 不编造缺字段：学校/学位/年份缺失只在封面留空并给 warning；
3. 图表不自动排版：正文只留 ``\\\\ref{fig:...}`` / ``\\\\ref{tab:...}`` 占位，
   引用检查是纯词面确定性比对，不保证语义正确。

可见性：认证打开时只导出当前用户可见的文献 + 自己的 claim；MCP 的 system
令牌（内部服务令牌）按全库。导出是只读派生，不落库。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AppUser, Claim, Figure, Paper, PaperVersion, TableBlock
from .references import _entry_type, format_reference, format_references
from .team import private_claim_condition, visible_paper_condition
from .writing import (
    ClaimError,
    _CITE_RE,
    _latex_escape,
    _resolve_key,
    _safe_filename,
    _suggest_key,
    DEFAULT_SECTION,
)

# 默认章节顺序（docs/17 §11.3.3）；「参考文献」是 backmatter，不参与编号章节
DEFAULT_CHAPTERS = ("摘要", "绪论", "相关工作", "方法", "实验与结果", "结论")
REFERENCES_CHAPTER = "参考文献"
MAX_THESIS_CHAPTERS = 30
MAX_THESIS_REFS = 300

# claim.section → 默认章节的别名表：用户写「引言」「文献综述」「实验结果」也能归章
_SECTION_ALIASES = {
    "摘要": "摘要",
    "abstract": "摘要",
    "绪论": "绪论",
    "引言": "绪论",
    "前言": "绪论",
    "introduction": "绪论",
    "相关工作": "相关工作",
    "文献综述": "相关工作",
    "研究现状": "相关工作",
    "related work": "相关工作",
    "方法": "方法",
    "方法学": "方法",
    "研究方法": "方法",
    "模型": "方法",
    "method": "方法",
    "methods": "方法",
    "实验与结果": "实验与结果",
    "实验": "实验与结果",
    "结果": "实验与结果",
    "实验结果": "实验与结果",
    "experiments": "实验与结果",
    "results": "实验与结果",
    "结论": "结论",
    "总结": "结论",
    "结论与展望": "结论",
    "conclusion": "结论",
}

# LaTeX 里有特殊含义、出现在 cite_key 里会直接编译失败的字符
_UNSAFE_KEY_RE = re.compile(r"[\\{}%&#$^~]")


class ThesisError(RuntimeError):
    """学位论文导出 / 检查的可读错误（路由层转 404/422）。"""


# ---------------------------------------------------------------------------
# 章节归并
# ---------------------------------------------------------------------------
def _canonical_section(label: str) -> str:
    key = (label or "").strip()
    if not key:
        return ""
    return _SECTION_ALIASES.get(key) or _SECTION_ALIASES.get(key.lower()) or key


def _chapter_for_section(section: str, chapters: list[str]) -> str:
    """claim 的 section → 章节名：先按别名归一，再按章节表匹配。"""
    canon = _canonical_section(section) or DEFAULT_SECTION
    for chapter in chapters:
        if _canonical_section(chapter) == canon:
            return chapter
    # 别名对不上：section 原样作为额外章节（不丢内容）
    return (section or "").strip() or DEFAULT_SECTION


def _clean_chapter_order(raw: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in raw or []:
        chapter = (value or "").strip()
        if not chapter or chapter == REFERENCES_CHAPTER:
            continue
        if chapter not in out:
            out.append(chapter)
    if len(out) > MAX_THESIS_CHAPTERS:
        raise ThesisError(f"章节最多 {MAX_THESIS_CHAPTERS} 个")
    return out


# ---------------------------------------------------------------------------
# 可见内容加载
# ---------------------------------------------------------------------------
async def _load_claims(
    session: AsyncSession,
    *,
    claim_ids: list[str] | None,
    paper_ids: list[str] | None,
    include_drafts: bool,
    user: AppUser | None,
) -> list[tuple[Claim, Paper]]:
    stmt = (
        select(Claim, Paper)
        .join(Paper, Claim.paper_id == Paper.id)
        .where(visible_paper_condition(user), private_claim_condition(user))
    )
    if claim_ids:
        stmt = stmt.where(Claim.id.in_(claim_ids))
    if paper_ids:
        stmt = stmt.where(Claim.paper_id.in_(paper_ids))
    if not include_drafts:
        stmt = stmt.where(Claim.status == "approved")
    stmt = stmt.order_by(Claim.section.asc(), Claim.order_idx.asc(), Claim.created_at.asc())
    rows = (await session.execute(stmt)).all()
    return [(claim, paper) for claim, paper in rows]


async def _load_visible_papers(
    session: AsyncSession,
    *,
    paper_ids: list[str] | None,
    user: AppUser | None,
) -> dict[str, Paper]:
    stmt = select(Paper).where(visible_paper_condition(user))
    if paper_ids:
        stmt = stmt.where(Paper.id.in_(paper_ids)).order_by(Paper.created_at.asc())
    else:
        stmt = stmt.order_by(Paper.created_at.asc()).limit(MAX_THESIS_REFS)
    return {paper.id: paper for paper in (await session.execute(stmt)).scalars()}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _cite_key(paper: Paper) -> str:
    """与 writing/references 一致的引用键兜底。"""
    return (paper.cite_key or f"litbench-{paper.id[:8]}").strip()


def _claim_latex(claim: Claim, paper: Paper) -> str:
    text = _latex_escape((claim.text or "").strip())
    key = _cite_key(paper)
    raw = claim.text or ""
    if key and "\\cite" not in raw and f"[@{key}]" not in raw:
        text = f"{text}~\\cite{{{key}}}"
    return text


def _titlepage(*, title: str, author: str, school: str, degree: str, advisor: str, year: str) -> list[str]:
    def field(label: str, value: str) -> str:
        shown = _latex_escape(value) if value else r"\underline{\hspace{4cm}}"
        return rf"  {label} & {shown} \\"

    lines = [
        r"\begin{titlepage}",
        r"\centering",
        r"\vspace*{1.5cm}",
    ]
    if school:
        lines.append(rf"{{\Large {_latex_escape(school)}}}\par")
    else:
        lines.append(r"{\Large \underline{\hspace{6cm}}}\par")
    lines.append(r"\vspace{1.5cm}")
    lines.append(rf"{{\Huge\bfseries {_latex_escape(title) or '（题目待填写）'}}}\par")
    lines.append(r"\vspace{1.5cm}")
    lines.append(
        rf"{{\Large {_latex_escape(degree) + '学位论文' if degree else '学位论文'}}}\par"
    )
    lines.extend(
        [
            r"\vspace{2cm}",
            r"\begin{tabular}{rl}",
            field("作者", author),
            field("导师", advisor),
            field("学位", degree),
            field("年份", year),
        ]
    )
    lines.extend(
        [
            r"\end{tabular}",
            r"\vfill",
            r"\end{titlepage}",
        ]
    )
    return lines


def _gbt_tex(papers: list[Paper]) -> str:
    """GB/T 7714-2015 文本版：``\\input{references_gbt}`` 即可替代 BibTeX。"""
    lines = [
        "% references_gbt.tex — 由 LitBench 生成（GB/T 7714-2015 顺序编码制文本版）",
        "% 用法：在主文件里把 \\bibliographystyle/\\bibliography 两行换成 \\input{references_gbt}",
        r"\begin{thebibliography}{99}",
    ]
    for paper in papers:
        entry, _warnings = format_reference(paper, "gbt7714", index=None, numbered=False)
        # 条目文本里的 & % # _ 等要转义；\bibitem 的键必须与正文 \cite 完全一致，不转义
        lines.append(rf"\bibitem{{{_cite_key(paper)}}} {_latex_escape(entry)}")
    lines.extend([r"\end{thebibliography}", ""])
    return "\n".join(lines)


def _thesis_warnings(
    *,
    title: str,
    author: str,
    school: str,
    degree: str,
    advisor: str,
    year: int | None,
    papers: list[Paper],
    missing_claim_ids: list[str],
) -> list[str]:
    warnings: list[str] = []
    if not title.strip():
        warnings.append("缺论文题目，封面会留空")
    if not author.strip():
        warnings.append("缺作者，封面会留空")
    if not school.strip():
        warnings.append("缺学校，封面会留空")
    if not degree.strip():
        warnings.append("缺学位类型（博士/硕士），封面会留空")
    if not advisor.strip():
        warnings.append("缺导师，封面会留空")
    if year is None:
        warnings.append("缺年份，封面会留空")
    if missing_claim_ids:
        warnings.append(f"有 {len(missing_claim_ids)} 条卡片不在可见范围，已跳过")
    for paper in papers:
        key = _cite_key(paper)
        if not paper.cite_key:
            warnings.append(f"文献 {key} 没有 cite_key，已用内部 id 兜底")
        if _UNSAFE_KEY_RE.search(key):
            warnings.append(
                f"cite_key {key!r} 含 LaTeX 特殊字符，\\cite 前建议改成字母/数字/冒号"
            )
        if _entry_type(paper) == "thesis":
            if not (paper.thesis_school or "").strip():
                warnings.append(f"学位论文 {key} 缺学校，[D] 条目会省略学校")
            if not paper.year:
                warnings.append(f"学位论文 {key} 缺年份，[D] 条目会省略年份")
    return warnings


async def export_thesis(
    session: AsyncSession,
    *,
    title: str = "",
    author: str = "",
    school: str = "",
    degree: str = "",
    advisor: str = "",
    year: int | None = None,
    paper_ids: list[str] | None = None,
    claim_ids: list[str] | None = None,
    chapter_order: list[str] | None = None,
    include_drafts: bool = True,
    user: AppUser | None = None,
) -> dict:
    """生成 ctexbook 骨架 + refs.bib + references_gbt.tex（只读派生）。"""
    rows = await _load_claims(
        session,
        claim_ids=claim_ids,
        paper_ids=paper_ids,
        include_drafts=include_drafts,
        user=user,
    )
    found_claim_ids = {claim.id for claim, _paper in rows}
    missing_claim_ids = [cid for cid in (claim_ids or []) if cid not in found_claim_ids]
    if claim_ids and not rows:
        raise ThesisError("没有可导出的 claim；先写几张卡片，或放宽「包含草稿」")

    # 参考文献范围：显式 paper_ids 优先，否则取 claim 关联的文献
    paper_map: dict[str, Paper] = {}
    if paper_ids:
        visible = await _load_visible_papers(session, paper_ids=paper_ids, user=user)
        missing_paper_ids = [pid for pid in paper_ids if pid not in visible]
        if missing_paper_ids:
            raise ThesisError(f"文献不存在或无权限：{', '.join(missing_paper_ids[:5])}")
        for pid in paper_ids:
            paper_map[pid] = visible[pid]
    for _claim, paper in rows:
        paper_map.setdefault(paper.id, paper)
    if not paper_map and not rows:
        raise ThesisError("没有可导出的内容；先选文献或写几张 claim 卡片")
    if len(paper_map) > MAX_THESIS_REFS:
        raise ThesisError(f"参考文献最多 {MAX_THESIS_REFS} 篇")

    chapters = _clean_chapter_order(chapter_order) or list(DEFAULT_CHAPTERS)
    groups: dict[str, list[tuple[Claim, Paper]]] = {}
    extra_chapters: list[str] = []
    for claim, paper in rows:
        chapter = _chapter_for_section(claim.section or DEFAULT_SECTION, chapters)
        if chapter not in chapters and chapter not in extra_chapters:
            extra_chapters.append(chapter)
        groups.setdefault(chapter, []).append((claim, paper))
    chapters.extend(extra_chapters)

    papers = list(paper_map.values())
    warnings = _thesis_warnings(
        title=title,
        author=author,
        school=school,
        degree=degree,
        advisor=advisor,
        year=year,
        papers=papers,
        missing_claim_ids=missing_claim_ids,
    )

    safe_title = title.strip() or "LitBench 学位论文骨架"
    year_text = str(year) if year else ""
    body: list[str] = [
        f"% {safe_title} — 由 LitBench 生成的学位论文骨架（ctexbook）",
        f"% 生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}",
        "% 正文来自写作台 claim 卡片；封面/页边距/页眉页脚请按学校模板修改。",
        "% 本文件只提供可编译的骨架，不代替正文写作。",
        "",
        r"\documentclass[UTF8,12pt,openany]{ctexbook}",
        r"\usepackage{geometry}",
        r"\geometry{margin=2.5cm}",
        r"\usepackage[numbers,sort&compress]{natbib}",
        r"\usepackage{hyperref}",
        r"\hypersetup{colorlinks=true,linkcolor=black,citecolor=black,urlcolor=blue}",
        rf"\title{{{_latex_escape(title.strip())}}}",
        rf"\author{{{_latex_escape(author.strip())}}}",
        rf"\date{{{_latex_escape(year_text)}}}",
        "",
        r"\begin{document}",
        "",
        *_titlepage(
            title=title.strip(),
            author=author.strip(),
            school=school.strip(),
            degree=degree.strip(),
            advisor=advisor.strip(),
            year=year_text,
        ),
        "",
        r"\frontmatter",
    ]

    # 摘要：默认章节里的「摘要」放到 frontmatter；用户自定义章节顺序里没有就不生成
    if "摘要" in chapters:
        body.extend(
            [
                r"\chapter*{摘要}",
                r"\addcontentsline{toc}{chapter}{摘要}",
            ]
        )
        abstract_claims = groups.get("摘要") or []
        if abstract_claims:
            for claim, paper in abstract_claims:
                body.extend([_claim_latex(claim, paper), ""])
        else:
            body.append("% 摘要待写（这里放中文摘要）")
            body.append("")

    body.extend([r"\tableofcontents", "", r"\mainmatter"])

    for chapter in chapters:
        if chapter == "摘要":
            continue
        body.append(rf"\chapter{{{_latex_escape(chapter)}}}")
        claims = groups.get(chapter) or []
        if not claims:
            body.append("% 本章暂无卡片，可手工补写正文。")
            body.append("")
            continue
        for claim, paper in claims:
            body.extend([f"% [claim {claim.id}]", _claim_latex(claim, paper), ""])

    body.extend(
        [
            r"\backmatter",
            r"\bibliographystyle{plainnat}",
            r"\bibliography{refs}",
            "% 学校若要求 GB/T 7714 文本版（不走 BibTeX），把上面两行换成：",
            "%   \\input{references_gbt}",
            r"\end{document}",
            "",
        ]
    )

    bib = format_references(papers, "bibtex")["content"].strip()
    refs_bib = (
        "% refs.bib — 由 LitBench 从文献库导出（cite_key 与库内一致）\n\n"
        + bib
        + "\n"
    )
    return {
        "format": "latex",
        "filename": f"{_safe_filename(safe_title)}.tex",
        "content": "\n".join(body),
        "extra_files": {
            "refs.bib": refs_bib,
            "references_gbt.tex": _gbt_tex(papers),
        },
        "claim_count": len(rows),
        "paper_count": len(papers),
        "chapter_count": len([chapter for chapter in chapters if chapter != "摘要"]),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 引用交叉检查（确定性；不调模型）
# ---------------------------------------------------------------------------
_REF_RE = re.compile(r"\\(?:ref|autoref|Cref|cref)\s*\{([^{}]*)\}")
_FIG_PREFIXES = ("fig:",)
_TAB_PREFIXES = ("tab:",)


def _norm_label(raw: str) -> str:
    """图/表标签归一：去中英文前缀、去空白、大小写不敏感。

    要同时吃两类写法：解析器归一化的 ``fig:7-1`` / ``tab:7-1``，
    以及人手写的 ``图 7-1`` / ``表 7-1`` / ``Figure 7.1``。
    """
    value = (raw or "").strip().lower()
    value = re.sub(r"^(?:fig(?:ure)?|tab(?:le)?)\s*[:：.]?\s*", "", value)
    value = re.sub(r"^(?:图|表)\s*", "", value)
    value = re.sub(r"\s+", "", value)
    return value.rstrip(".")


def _extract_cite_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in _CITE_RE.finditer(text or ""):
        for raw in match.group(1).split(","):
            key = raw.strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def _extract_refs(text: str) -> tuple[list[str], list[str]]:
    figures: list[str] = []
    tables: list[str] = []
    for match in _REF_RE.finditer(text or ""):
        label = match.group(1).strip()
        for prefix in _FIG_PREFIXES:
            if label.lower().startswith(prefix):
                value = label[len(prefix):].strip()
                if value and value not in figures:
                    figures.append(value)
        for prefix in _TAB_PREFIXES:
            if label.lower().startswith(prefix):
                value = label[len(prefix):].strip()
                if value and value not in tables:
                    tables.append(value)
    return figures, tables


async def _load_figure_index(
    session: AsyncSession, user: AppUser | None
) -> tuple[dict[str, Figure], dict[str, str]]:
    """返回 (标签查找表, 标签→展示名)。"""
    stmt = (
        select(Figure)
        .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        .where(Figure.deleted_at.is_(None), visible_paper_condition(user))
    )
    figure_map: dict[str, Figure] = {}
    labels: dict[str, str] = {}
    for figure in (await session.execute(stmt)).scalars():
        candidates = [figure.cite_key, figure.label_norm, figure.label_raw]
        for candidate in candidates:
            label = (candidate or "").strip()
            if not label:
                continue
            figure_map.setdefault(label.lower(), figure)
            figure_map.setdefault(_norm_label(label), figure)
            labels.setdefault(label.lower(), figure.label_raw or figure.label_norm or label)
    return figure_map, labels


async def _load_table_index(
    session: AsyncSession, user: AppUser | None
) -> tuple[dict[str, TableBlock], dict[str, str]]:
    stmt = (
        select(TableBlock)
        .join(PaperVersion, TableBlock.paper_version_id == PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        .where(visible_paper_condition(user))
    )
    table_map: dict[str, TableBlock] = {}
    labels: dict[str, str] = {}
    for table in (await session.execute(stmt)).scalars():
        # 表没有 cite_key（图有），所以额外接受 table_id：
        # 前端「复制引用」在没识别出表号时退化为 \ref{tab:<table_id>}。
        candidates = [table.id, table.label_norm, table.label_raw]
        for candidate in candidates:
            label = (candidate or "").strip()
            if not label:
                continue
            table_map.setdefault(label.lower(), table)
            table_map.setdefault(_norm_label(label), table)
            labels.setdefault(label.lower(), table.label_raw or table.label_norm or label)
    return table_map, labels


def _resolve_label(index: dict, labels: dict, raw: str) -> tuple[object | None, str | None]:
    key = (raw or "").strip()
    if not key:
        return None, None
    obj = index.get(key.lower()) or index.get(_norm_label(key))
    return obj, labels.get(key.lower())


async def check_thesis(
    session: AsyncSession,
    *,
    text: str,
    paper_ids: list[str] | None = None,
    user: AppUser | None = None,
) -> dict:
    """扫描 ``\\cite{}`` / ``\\ref{fig:*}`` / ``\\ref{tab:*}``，比对库内 cite_key 与图/表标签。"""
    body = text or ""
    cite_keys = _extract_cite_keys(body)
    figure_refs, table_refs = _extract_refs(body)

    if paper_ids:
        visible = await _load_visible_papers(session, paper_ids=paper_ids, user=user)
        all_papers = list(visible.values())
    else:
        visible = await _load_visible_papers(session, paper_ids=None, user=user)
        all_papers = list(visible.values())

    citations: list[dict] = []
    for key in cite_keys:
        paper = _resolve_key(all_papers, key)
        if paper is None:
            citations.append(
                {
                    "key": key,
                    "status": "unknown",
                    "paper_id": None,
                    "title": None,
                    "cite_key": None,
                    "suggestion": _suggest_key(all_papers, key),
                }
            )
        else:
            citations.append(
                {
                    "key": key,
                    "status": "resolved",
                    "paper_id": paper.id,
                    "title": paper.title,
                    "cite_key": paper.cite_key,
                    "suggestion": None,
                }
            )
    unknown_cites = [item["key"] for item in citations if item["status"] == "unknown"]

    figure_index, figure_labels = await _load_figure_index(session, user)
    table_index, table_labels = await _load_table_index(session, user)

    figures: list[dict] = []
    for label in figure_refs:
        figure, shown = _resolve_label(figure_index, figure_labels, label)
        figures.append(
            {
                "key": label,
                "status": "resolved" if figure is not None else "unknown",
                "kind": "figure",
                "object_id": getattr(figure, "id", None),
                "label": shown or getattr(figure, "label_raw", None),
                "suggestion": None,
            }
        )
    tables: list[dict] = []
    for label in table_refs:
        table, shown = _resolve_label(table_index, table_labels, label)
        tables.append(
            {
                "key": label,
                "status": "resolved" if table is not None else "unknown",
                "kind": "table",
                "object_id": getattr(table, "id", None),
                "label": shown or getattr(table, "label_raw", None),
                "suggestion": None,
            }
        )

    missing: list[str] = [f"cite:{key}" for key in unknown_cites]
    missing.extend(f"fig:{item['key']}" for item in figures if item["status"] == "unknown")
    missing.extend(f"tab:{item['key']}" for item in tables if item["status"] == "unknown")

    cited = {key.lower() for key in cite_keys}
    unused = [
        paper.cite_key
        for paper in all_papers
        if paper.cite_key and paper.cite_key.lower() not in cited
    ]

    warnings: list[str] = []
    if not cite_keys:
        warnings.append("没有检测到 \\cite{} 引用")
    for figure in figures:
        if figure["status"] == "unknown":
            warnings.append(f"正文引用了图 {figure['key']}，但库里找不到对应图号")
    for table in tables:
        if table["status"] == "unknown":
            warnings.append(f"正文引用了表 {table['key']}，但库里找不到对应表号")
    unknown_total = len(unknown_cites) + sum(
        1 for item in (*figures, *tables) if item["status"] == "unknown"
    )
    resolved = len(cite_keys) - len(unknown_cites)
    message = (
        f"引用 {len(cite_keys)} 条（可解析 {resolved}）；图 {len(figures)} 个、表 {len(tables)} 个；"
        f"{len(unused)} 篇文献未被正文引用"
        + (f"；{unknown_total} 项找不到" if unknown_total else "")
    )
    return {
        "total": len(cite_keys),
        "resolved": resolved,
        "unknown": len(unknown_cites),
        "citations": citations,
        "figures": figures,
        "tables": tables,
        "missing": missing,
        "unused": unused,
        "warnings": warnings,
        "message": message,
    }


__all__ = [
    "DEFAULT_CHAPTERS",
    "MAX_THESIS_REFS",
    "REFERENCES_CHAPTER",
    "ThesisError",
    "check_thesis",
    "export_thesis",
]
