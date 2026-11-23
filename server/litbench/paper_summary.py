# SPDX-License-Identifier: AGPL-3.0-or-later
"""整篇文献速览（M2b/M3）：「AI 一键总结文献」。

和章节摘要的区别（docs/15）：

* 章节摘要按标题切章，一节制一次模型调用；本模块**整篇只调一次**，成本可控；
* 材料 = 作者摘要 + 章节大纲 + 引言/结论附近的正文证据块（不是全文，避免一次烧掉整本书）；
* 结果仍落在同一张 ``summary`` 表里，``section_key="__paper__"`` 作为「整篇」的保留键——
  证据校验、自动接受、编辑锁定、revision 回滚全部复用 M2a 的状态机，不另造一套。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .evidence import EvidenceError, validate_summary_payload
from .llm import LLMClient, LLMError, complete_with_budget_retry, effective_config, get_client
from .models import Block, Figure, Paper, PaperVersion, Summary, SummaryRevision
from .prompts import load_prompt, render
from .sections import build_sections
from .summaries import SummaryError, latest_version

PROMPT_NAME = "paper_summary"
PAPER_SECTION_KEY = "__paper__"
PAPER_SECTION_TITLE = "整篇速览"

# 整篇速览的正文预算：引言/结论各取一段，够模型判断「做了什么、结果如何」。
INTRO_BUDGET = 1800
CONCLUSION_BUDGET = 1400
MAX_EVIDENCE_BLOCKS = 14


class PaperSummaryError(RuntimeError):
    """整篇速览的可读错误（没有 PDF / 没有正文 / 模型没配好 / 人工锁定）。"""


def _meta_line(paper: Paper) -> str:
    parts: list[str] = []
    if paper.authors_json:
        names = []
        for author in paper.authors_json[:6]:
            if not isinstance(author, dict):
                continue
            name = author.get("name") or " ".join(
                str(author.get(k) or "").strip()
                for k in ("firstName", "lastName")
                if author.get(k)
            )
            if name:
                names.append(str(name))
        if names:
            more = " 等" if len(paper.authors_json) > len(names) else ""
            parts.append("、".join(names) + more)
    if paper.year:
        parts.append(str(paper.year))
    if paper.venue:
        parts.append(str(paper.venue))
    if paper.doi:
        parts.append(f"DOI {paper.doi}")
    elif paper.arxiv_id:
        parts.append(f"arXiv:{paper.arxiv_id}")
    return " · ".join(parts) or "（未著录）"


def _outline(blocks: list[Block], version: PaperVersion, figure_pages: list[int]) -> str:
    sections = build_sections(
        blocks,
        page_count=version.page_count,
        figure_pages=figure_pages,
        parser=version.parser,
    )
    if not sections:
        return "（解析器没切出章节；按页阅读）"
    lines: list[str] = []
    for section in sections[:40]:
        indent = "  " * max(0, section.level - 1)
        lines.append(
            f"{indent}- {section.title or '（无标题）'}"
            f"（p{section.page_start}–{section.page_end}，{section.figure_count} 图）"
        )
    return "\n".join(lines)


def _pick_evidence_blocks(blocks: list[Block]) -> list[Block]:
    """取引言（开头）+ 结论（结尾）附近的正文块，跳过参考文献/纯数字行。"""
    readable = [
        b
        for b in blocks
        if (b.text or "").strip() and b.type in ("text", "title", "section", "caption")
    ]
    if not readable:
        return []
    picked: list[Block] = []
    seen: set[str] = set()

    def take(block_list: list[Block], budget: int) -> None:
        used = 0
        for block in block_list:
            if len(picked) >= MAX_EVIDENCE_BLOCKS:
                return
            text = (block.text or "").strip()
            if block.id in seen or not text:
                continue
            if text.startswith("参考文献") or text.startswith("References"):
                break
            picked.append(block)
            seen.add(block.id)
            used += len(text)
            if used >= budget:
                return

    take(readable[:8], INTRO_BUDGET)
    # 结论：从尾部往前找，但排除参考文献区（粗略：最后 25% 里含 References 之后的不取）
    tail = readable[-12:]
    take(tail, CONCLUSION_BUDGET)
    return picked


def _evidence_lines(blocks: list[Block]) -> tuple[str, list[str], list[str]]:
    lines: list[str] = []
    ids: list[str] = []
    texts: list[str] = []
    total = 0
    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        if total + len(text) > settings.summary_max_chars:
            remaining = settings.summary_max_chars - total
            if remaining <= 20:
                break
            text = text[:remaining]
        total += len(text)
        ids.append(block.id)
        texts.append(text)
        lines.append(f"{block.id} | p{block.page} | {text[:400]}")
    return "\n".join(lines), ids, texts


async def _load_context(
    session: AsyncSession, paper_id: str
) -> tuple[Paper, PaperVersion, list[Block]]:
    paper = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one_or_none()
    if paper is None:
        raise PaperSummaryError("paper not found")
    try:
        version = await latest_version(session, paper_id)
    except SummaryError as exc:
        raise PaperSummaryError(str(exc)) from exc
    blocks = list(
        (
            await session.execute(
                select(Block)
                .where(Block.paper_version_id == version.id)
                .order_by(Block.page, Block.order_idx)
            )
        ).scalars()
    )
    return paper, version, blocks


async def get_paper_summary(session: AsyncSession, paper_id: str) -> Summary | None:
    """取当前最新版本的整篇速览（没有就返回 None）。"""
    try:
        version = await latest_version(session, paper_id)
    except SummaryError as exc:
        raise PaperSummaryError(str(exc)) from exc
    return (
        await session.execute(
            select(Summary).where(
                Summary.paper_version_id == version.id,
                Summary.section_key == PAPER_SECTION_KEY,
            )
        )
    ).scalar_one_or_none()


def _snapshot(session: AsyncSession, summary: Summary, reason: str) -> None:
    session.add(
        SummaryRevision(
            summary_id=summary.id,
            content=summary.content,
            key_points_json=summary.key_points_json or [],
            evidence_block_ids_json=summary.evidence_block_ids_json or [],
            confidence=summary.confidence,
            source=summary.source,
            provider=summary.provider,
            model=summary.model,
            prompt_version=summary.prompt_version,
            status=summary.status,
            reason=reason,
            edited_by=summary.edited_by,
        )
    )


async def generate_paper_summary(
    session: AsyncSession,
    paper_id: str,
    *,
    force: bool = False,
    provider: str | None = None,
    client: LLMClient | None = None,
) -> dict:
    """一键总结整篇文献（一次模型调用 + 证据校验 + 落库）。"""
    paper, version, blocks = await _load_context(session, paper_id)
    figure_pages = [
        int(page)
        for page in (
            await session.execute(
                select(Figure.page).where(
                    Figure.paper_version_id == version.id, Figure.deleted_at.is_(None)
                )
            )
        ).scalars()
    ]
    evidence_blocks = _pick_evidence_blocks(blocks)
    if not evidence_blocks:
        raise PaperSummaryError("这篇文献没有可读正文（可能还没解析完成）")

    existing = await get_paper_summary(session, paper_id)
    if existing is not None and not force:
        # 与章节摘要对齐：人工锁定的、已发布的一律不静默覆盖（force 才允许，覆盖前留 revision）。
        if existing.human_locked or existing.source == "human_edited":
            raise PaperSummaryError(
                "整篇速览已被人工编辑锁定；要覆盖请用「重新生成」并确认覆盖"
            )
        if existing.status == "published":
            raise PaperSummaryError(
                "整篇速览已发布（内容已确认）；要覆盖请用「重新生成」并确认覆盖"
            )
    if existing is not None:
        # 覆盖前先留历史（与章节摘要一致：任何重新生成都可回滚）
        _snapshot(session, existing, reason="regenerated")

    prompt = load_prompt(PROMPT_NAME)
    if client is None:
        source, _cfg = effective_config()
        if source == "mock":
            raise PaperSummaryError(
                "还没有配置模型：请先在右上角「设置」里填 base_url / model / API key"
            )
    try:
        llm = client or get_client(provider)
    except LLMError as exc:
        raise PaperSummaryError(str(exc)) from exc

    evidence_lines, evidence_ids, texts = _evidence_lines(evidence_blocks)
    outline = _outline(blocks, version, figure_pages)
    messages = [
        {
            "role": "user",
            "content": render(
                prompt,
                title=paper.title,
                meta_line=_meta_line(paper),
                abstract=(paper.abstract or "（无摘要）").strip()[:2000],
                outline=outline,
                evidence_lines=evidence_lines,
            ),
        }
    ]
    allowed = {b.id for b in blocks}
    try:
        result = await complete_with_budget_retry(
            llm,
            messages,
            json_schema={"type": "object"},
            meta={
                "task": "paper_summary",
                "title": paper.title,
                "evidence_ids": evidence_ids,
                "texts": texts,
            },
            max_tokens=settings.paper_summary_max_tokens,
        )
        validated = validate_summary_payload(
            result.parsed, allowed=allowed, min_ids=1
        )
    except (LLMError, EvidenceError) as exc:
        raise PaperSummaryError(str(exc)) from exc

    auto_accept = (
        settings.summary_auto_accept
        and validated.confidence >= settings.summary_auto_accept_confidence
    )
    now = datetime.now(timezone.utc)
    if existing is None:
        summary = Summary(
            paper_version_id=version.id,
            section_key=PAPER_SECTION_KEY,
            section_title=PAPER_SECTION_TITLE,
            level=0,
            content=validated.content,
            key_points_json=validated.key_points,
            evidence_block_ids_json=validated.evidence_ids,
            source="agent",
            provider=result.provider,
            model=result.model,
            prompt_version=prompt.version,
            status="published" if auto_accept else "draft",
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost=result.cost,
            confidence=validated.confidence,
            auto_accepted=auto_accept,
            human_locked=False,
            created_at=now,
            updated_at=now,
        )
        session.add(summary)
        await session.flush()
        action = "created"
    else:
        summary = existing
        summary.section_title = PAPER_SECTION_TITLE
        summary.level = 0
        summary.content = validated.content
        summary.key_points_json = validated.key_points
        summary.evidence_block_ids_json = validated.evidence_ids
        summary.source = "agent"
        summary.provider = result.provider
        summary.model = result.model
        summary.prompt_version = prompt.version
        summary.status = "published" if auto_accept else "draft"
        summary.tokens_in = result.tokens_in
        summary.tokens_out = result.tokens_out
        summary.cost = result.cost
        summary.confidence = validated.confidence
        summary.auto_accepted = auto_accept
        summary.human_locked = False
        summary.edited_by = None
        summary.edited_at = None
        summary.updated_at = now
        action = "updated"
    await session.commit()
    return {
        "paper_id": paper_id,
        "version_id": version.id,
        "status": action,
        "summary_id": summary.id,
        "provider": result.provider,
        "model": result.model,
        "prompt_version": prompt.version,
        "confidence": validated.confidence,
        "auto_accepted": auto_accept,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost": result.cost,
        "latency_ms": result.latency_ms,
    }


__all__ = [
    "PAPER_SECTION_KEY",
    "PAPER_SECTION_TITLE",
    "PaperSummaryError",
    "PROMPT_NAME",
    "generate_paper_summary",
    "get_paper_summary",
]
