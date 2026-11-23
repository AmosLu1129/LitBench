# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作大纲与「引用—图表」交叉检查（M8，2026-11-16）。

把 claim 卡片按 `section` 归到默认章节（绪论/相关工作/方法/实验与结果/结论），
给出**可读大纲**（章节 → 按 order_idx 排序的 claim），并做两件确定性检查：

1. **图表**: claim 正文里写的「图 3 / Fig. 3 / 表 2」是否和这张卡片的证据标签对得上；
   对不上（正文说图 3、证据是图 4）在 issues 里报出来，不静默；
2. **引用**: claim 正文里手写的 ``\\cite{key}`` 能否在可见文献库里解析，解析不了报
   unknown（与 `/thesis/check` 同口径）。

边界（与 M5/M8 其他块一致）：

* 只对**已有卡片**做整理和词面检查，不写正文、不改 claim；
* 只做**词面确定性比对**，不做语义判断（「图 3 支持这个结论」的对错不归它管）；
* 检查结果只读派生，不落库；用户要根据 issues 自己改正文/证据。
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AppUser
from .thesis import (
    DEFAULT_CHAPTERS,
    _canonical_section,
    _chapter_for_section,
    _clean_chapter_order,
    _load_claims,
    _norm_label,
)
from .writing import _CITE_RE, ClaimError, _resolve_key, _suggest_key, visible_paper_index

MAX_OUTLINE_CLAIMS = 500

# 「图 3」「图3」「图 3-1」「Fig. 3(a)」「Table 2」「表2」都认；
# 英文前缀加「前面不是字母」守卫（configure 3 / stable 3），中文排除「图表/发表/代表」
# 这类复合词里的图/表字，避免把普通句子误判成图表引用。
_MENTION_RE = re.compile(
    r"(?P<kind>(?<![图表代发])图|(?<![图表代发])表|"
    r"(?<![A-Za-z])(?:fig(?:ure)?\.?|tab(?:le)?\.?))"
    r"\s*(?P<num>[A-Za-z]?\d+(?:[.\-–]\d+)?(?:\([a-z]\))?)",
    re.IGNORECASE,
)
_REF_RE = re.compile(r"\\(?:ref|autoref|Cref|cref)\s*\{([^{}]*)\}")


def _extract_mentions(text: str) -> tuple[set[str], set[str]]:
    """正文里的图/表编号（归一化：去前缀/空白，大小写不敏感）。"""
    figures: set[str] = set()
    tables: set[str] = set()
    for match in _MENTION_RE.finditer(text or ""):
        kind = match.group("kind").lower()
        number = _norm_label(match.group("num"))
        if not number:
            continue
        if kind.startswith(("图", "fig")):
            figures.add(number)
        else:
            tables.add(number)
    # LaTeX 里的 \ref{fig:3} / \ref{tab:2} 也算
    for match in _REF_RE.finditer(text or ""):
        label = match.group(1).strip()
        lower = label.lower()
        if lower.startswith("fig:"):
            number = _norm_label(label[4:])
            if number:
                figures.add(number)
        elif lower.startswith("tab:"):
            number = _norm_label(label[4:])
            if number:
                tables.add(number)
    return figures, tables


def _evidence_number(label: str) -> str:
    """证据标签「图 3」/「Table 2」→ 数字部分；没有编号返回空串。"""
    match = _MENTION_RE.search(label or "")
    if match:
        return _norm_label(match.group("num"))
    return ""


def _claim_checks(
    claim_text: str,
    evidence: list[dict],
    *,
    all_papers: list,
    claim_cite_key: str | None,
) -> tuple[list[dict], list[str]]:
    issues: list[dict] = []
    notes: list[str] = []
    figures, tables = _extract_mentions(claim_text)
    evidence_figures: set[str] = set()
    evidence_tables: set[str] = set()
    for item in evidence:
        kind = str(item.get("type") or "").lower()
        label = str(item.get("label") or "")
        if kind == "figure":
            number = _evidence_number(label)
            if number:
                evidence_figures.add(number)
        elif kind == "table":
            number = _evidence_number(label)
            if number:
                evidence_tables.add(number)

    for number in sorted(figures - evidence_figures):
        expected = "、".join(f"图 {value}" for value in sorted(evidence_figures)) or "（无图证据）"
        issues.append(
            {
                "kind": "figure",
                "severity": "warning",
                "message": f"正文提到图 {number}，但本卡证据对不上（证据：{expected}）",
            }
        )
    for number in sorted(evidence_figures - figures):
        notes.append(f"卡片有图 {number} 证据，但正文没提到")
    for number in sorted(tables - evidence_tables):
        expected = "、".join(f"表 {value}" for value in sorted(evidence_tables)) or "（无表证据）"
        issues.append(
            {
                "kind": "table",
                "severity": "warning",
                "message": f"正文提到表 {number}，但本卡证据对不上（证据：{expected}）",
            }
        )
    for number in sorted(evidence_tables - tables):
        notes.append(f"卡片有表 {number} 证据，但正文没提到")

    for match in _CITE_RE.finditer(claim_text or ""):
        for raw in match.group(1).split(","):
            key = raw.strip()
            if not key:
                continue
            paper = _resolve_key(all_papers, key)
            if paper is None:
                suggestion = _suggest_key(all_papers, key)
                suffix = f"；可能是 {suggestion}" if suggestion else ""
                issues.append(
                    {
                        "kind": "citation",
                        "severity": "warning",
                        "message": f"引用键 {key} 在可见文献库里找不到{suffix}",
                    }
                )
            elif claim_cite_key and key != claim_cite_key:
                notes.append(f"正文引用了 {key}（与所属文献的 cite_key 不同）")
    return issues, notes


def _render_markdown(sections: list[dict], title: str) -> str:
    lines = [f"# {title.strip() or '写作大纲'}", ""]
    for section in sections:
        lines.append(f"## {section['section']}")
        lines.append("")
        for claim in section["claims"]:
            text = (claim["text"] or "").strip()
            labels = [
                str(item.get("label") or "")
                for item in claim["evidence"]
                if str(item.get("type")) in ("figure", "table")
            ]
            labels = [label for label in labels if label]
            suffix = f"（证据：{'、'.join(labels)}）" if labels else ""
            lines.append(f"- {text}{suffix}")
            for issue in claim["issues"]:
                lines.append(f"  - ⚠ {issue['message']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def build_outline(
    session: AsyncSession,
    *,
    claim_ids: list[str] | None = None,
    paper_ids: list[str] | None = None,
    include_drafts: bool = True,
    section_order: list[str] | None = None,
    user: AppUser | None = None,
) -> dict:
    """claim → 章节大纲 + 图表/引用检查（只读派生，不落库）。"""
    rows = await _load_claims(
        session,
        claim_ids=claim_ids,
        paper_ids=paper_ids,
        include_drafts=include_drafts,
        user=user,
    )
    if not rows:
        raise ClaimError("没有可整理的 claim；先写几张卡片，或放宽「包含草稿」")
    if len(rows) > MAX_OUTLINE_CLAIMS:
        raise ClaimError(f"一次最多整理 {MAX_OUTLINE_CLAIMS} 张卡片")

    chapters = _clean_chapter_order(section_order) or list(DEFAULT_CHAPTERS)
    groups: dict[str, list[tuple]] = {}
    extra: list[str] = []
    for claim, paper in rows:
        chapter = _chapter_for_section(claim.section or "", chapters)
        if chapter not in chapters and chapter not in extra:
            extra.append(chapter)
        groups.setdefault(chapter, []).append((claim, paper))
    chapters.extend(extra)

    visible = await visible_paper_index(session, user)
    all_papers = list(visible)

    sections: list[dict] = []
    total_claims = 0
    issue_count = 0
    for chapter in chapters:
        group = groups.get(chapter)
        if not group:
            continue
        claims_out: list[dict] = []
        for claim, paper in group:
            evidence = [item for item in (claim.evidence_json or []) if isinstance(item, dict)]
            claim_cite_key = paper.cite_key if paper is not None else None
            issues, notes = _claim_checks(
                claim.text or "",
                evidence,
                all_papers=all_papers,
                claim_cite_key=claim_cite_key,
            )
            issue_count += len(issues)
            figure_labels = [
                str(item.get("label") or "")
                for item in evidence
                if str(item.get("type")) == "figure" and item.get("label")
            ]
            table_labels = [
                str(item.get("label") or "")
                for item in evidence
                if str(item.get("type")) == "table" and item.get("label")
            ]
            claims_out.append(
                {
                    "claim_id": claim.id,
                    "text": claim.text or "",
                    "status": claim.status or "draft",
                    "paper_id": claim.paper_id,
                    "paper_title": paper.title if paper is not None else None,
                    "cite_key": claim_cite_key,
                    "order_idx": int(claim.order_idx or 0),
                    "evidence": evidence,
                    "figure_labels": figure_labels,
                    "table_labels": table_labels,
                    "issues": issues,
                    "notes": notes,
                }
            )
            total_claims += 1
        sections.append(
            {
                "section": chapter,
                "canonical": _canonical_section(chapter),
                "claim_count": len(claims_out),
                "claims": claims_out,
            }
        )

    markdown = _render_markdown(sections, "写作大纲")
    return {
        "sections": sections,
        "total_claims": total_claims,
        "issue_count": issue_count,
        "section_order": chapters,
        "markdown": markdown,
        "filename": "outline.md",
        "warnings": [],
    }


__all__ = ["MAX_OUTLINE_CLAIMS", "build_outline"]
