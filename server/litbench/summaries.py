# SPDX-License-Identifier: AGPL-3.0-or-later
"""章节摘要服务（M2a）：章节树 → prompt → mock provider → 证据校验 → 落库 → 状态机。

这是 M2 的「脊椎」：UI 只发一个「生成」动作，服务端把整条链路跑完，所有 AI 产物都带证据。

状态机（docs/15 §5 D3）：

* 生成成功 → ``draft``；证据合格且置信度 ≥ 阈值 → 服务端**自动接受**为 ``published``
  （``auto_accepted=True``）；
* 用户编辑 → ``source=human_edited`` + ``human_locked=True`` + ``published``；
  此后自动生成不再覆盖（除非显式 force）；
* 每次生成/编辑/回滚前把旧内容压进 ``summary_revision``，可回滚；
* 证据校验不过 → **不落库**，只在本次结果里报错（不产生脏行）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .evidence import EvidenceError, validate_summary_payload
from .llm import LLMClient, LLMError, complete_with_budget_retry, get_client
from .models import Block, Figure, Paper, PaperVersion, Summary, SummaryRevision
from .prompts import load_prompt, render
from .sections import Section, build_sections

PROMPT_NAME = "section_summary"


class SummaryError(RuntimeError):
    """摘要相关的可读错误（文献不存在 / 没有文本块 / 章节不存在……）。"""


def _preview(text: str, limit: int = 200) -> str:
    """把模型原始输出压成一行短预览，用来诊断「为什么不是 JSON」。"""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


# ---------------------------------------------------------------------------
# 章节树
# ---------------------------------------------------------------------------
async def latest_version(session: AsyncSession, paper_id: str) -> PaperVersion:
    paper = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one_or_none()
    if paper is None:
        raise SummaryError("paper not found")
    version = (
        await session.execute(
            select(PaperVersion)
            .where(PaperVersion.paper_id == paper_id)
            .order_by(PaperVersion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if version is None:
        raise SummaryError("这篇文献还没有解析版本，先上传并解析 PDF")
    return version


async def _blocks_and_figures(
    session: AsyncSession, version_id: str
) -> tuple[list[Block], list[int]]:
    blocks = (
        await session.execute(
            select(Block)
            .where(Block.paper_version_id == version_id)
            .order_by(Block.page, Block.order_idx)
        )
    ).scalars().all()
    figure_pages = [
        int(page)
        for page in (
            await session.execute(
                select(Figure.page).where(
                    Figure.paper_version_id == version_id, Figure.deleted_at.is_(None)
                )
            )
        ).scalars()
    ]
    return list(blocks), figure_pages


async def paper_sections(session: AsyncSession, paper_id: str) -> tuple[PaperVersion, list[Section]]:
    version = await latest_version(session, paper_id)
    blocks, figure_pages = await _blocks_and_figures(session, version.id)
    sections = build_sections(
        blocks,
        page_count=version.page_count,
        figure_pages=figure_pages,
        parser=version.parser,
    )
    return version, sections


async def summaries_by_section(
    session: AsyncSession, version_id: str
) -> dict[str, Summary]:
    rows = (
        await session.execute(select(Summary).where(Summary.paper_version_id == version_id))
    ).scalars().all()
    return {s.section_key: s for s in rows}


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def _evidence_lines(blocks: list[Block]) -> tuple[str, list[str], list[str]]:
    lines: list[str] = []
    ids: list[str] = []
    texts: list[str] = []
    total = 0
    for b in blocks[: settings.summary_max_blocks]:
        text = (b.text or "").strip()
        if not text:
            continue
        if total + len(text) > settings.summary_max_chars:
            remaining = settings.summary_max_chars - total
            if remaining <= 20:
                break
            text = text[:remaining]
        total += len(text)
        ids.append(b.id)
        texts.append(text)
        lines.append(f"{b.id} | p{b.page} | {text[:300]}")
    return "\n".join(lines), ids, texts


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


async def generate_summaries(
    session: AsyncSession,
    paper_id: str,
    *,
    section_keys: list[str] | None = None,
    force: bool = False,
    provider: str | None = None,
    client: LLMClient | None = None,
) -> dict:
    version, sections = await paper_sections(session, paper_id)
    if not sections:
        raise SummaryError("这篇文献没有可切分的文本块（可能还没解析完成）")

    wanted = set(section_keys or [])
    targets = [s for s in sections if not wanted or s.key in wanted]
    if not targets:
        raise SummaryError("指定的章节不存在（文献可能已重新解析，请刷新章节树）")

    blocks, _ = await _blocks_and_figures(session, version.id)
    block_by_id = {b.id: b for b in blocks}
    existing_map = await summaries_by_section(session, version.id)

    prompt = load_prompt(PROMPT_NAME)
    try:
        llm = client or get_client(provider)
    except LLMError as exc:
        raise SummaryError(str(exc)) from exc
    results: list[dict] = []
    stats = {
        "paper_id": paper_id,
        "version_id": version.id,
        "provider": llm.provider,
        "model": llm.model,
        "prompt_version": prompt.version,
        "sections_total": len(sections),
        "requested": len(targets),
        "created": 0,
        "updated": 0,
        "skipped_existing": 0,
        "skipped_locked": 0,
        "skipped_empty": 0,
        "failed": 0,
        "tokens_in_total": 0,
        "tokens_out_total": 0,
        "cost_total": 0.0,
        "results": results,
    }

    for section in targets:
        entry = {
            "section_key": section.key,
            "section_title": section.title,
            "status": "",
            "summary_id": None,
            "error": None,
            "confidence": None,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": 0.0,
        }
        results.append(entry)
        existing = existing_map.get(section.key)

        if not section.block_ids:
            # 空标题（纯父节点）没有正文可作证据，跳过但保留在章节树里
            entry["status"] = "skipped_empty"
            stats["skipped_empty"] += 1
            continue

        if existing is not None and not force:
            if existing.human_locked or existing.source == "human_edited":
                entry["status"] = "skipped_locked"
                stats["skipped_locked"] += 1
                continue
            if existing.status == "published":
                entry["status"] = "skipped_existing"
                stats["skipped_existing"] += 1
                continue

        allowed = set(section.block_ids)
        body = [block_by_id[bid] for bid in section.block_ids if bid in block_by_id]
        evidence_lines, evidence_ids, texts = _evidence_lines(body)
        if not evidence_ids:
            entry["status"] = "failed"
            entry["error"] = "本章节没有可读正文（无可作证据的 block）"
            stats["failed"] += 1
            continue

        messages = [
            {
                "role": "user",
                "content": render(
                    prompt,
                    section_title=section.title,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    evidence_lines=evidence_lines,
                ),
            }
        ]
        result = None
        try:
            # 推理模型会先花 token 在 reasoning 上；预算不够时整段正文会丢。
            # 这里显式给章节摘要留足预算，并在「推理耗尽」时自动翻倍重试一次。
            result = await complete_with_budget_retry(
                llm,
                messages,
                json_schema={"type": "object"},
                meta={
                    "section_title": section.title,
                    "page_start": section.page_start,
                    "page_end": section.page_end,
                    "evidence_ids": evidence_ids,
                    "block_ids": evidence_ids,
                    "texts": texts,
                },
                max_tokens=settings.section_summary_max_tokens,
            )
            if not isinstance(result.parsed, dict):
                raise EvidenceError(
                    "模型输出不是 JSON 对象（原始输出前 200 字："
                    f"{_preview(result.text)}）"
                )
            validated = validate_summary_payload(
                result.parsed, allowed=allowed, min_ids=settings.summary_min_evidence
            )
        except (LLMError, EvidenceError) as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            # 失败也要记账：模型返回了内容但没过校验的调用同样花了钱
            if result is not None:
                entry["tokens_in"] = result.tokens_in
                entry["tokens_out"] = result.tokens_out
                entry["cost"] = result.cost
                stats["tokens_in_total"] += result.tokens_in
                stats["tokens_out_total"] += result.tokens_out
                stats["cost_total"] += result.cost
            stats["failed"] += 1
            continue

        assert result is not None
        entry["confidence"] = validated.confidence
        entry["tokens_in"] = result.tokens_in
        entry["tokens_out"] = result.tokens_out
        entry["cost"] = result.cost
        stats["tokens_in_total"] += result.tokens_in
        stats["tokens_out_total"] += result.tokens_out
        stats["cost_total"] += result.cost

        auto_accept = (
            settings.summary_auto_accept
            and validated.confidence >= settings.summary_auto_accept_confidence
        )
        now = datetime.now(timezone.utc)
        if existing is None:
            summary = Summary(
                paper_version_id=version.id,
                section_key=section.key,
                section_title=section.title,
                level=section.level,
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
            entry["status"] = "created"
            stats["created"] += 1
        else:
            _snapshot(session, existing, reason="regenerated")
            existing.section_title = section.title
            existing.level = section.level
            existing.content = validated.content
            existing.key_points_json = validated.key_points
            existing.evidence_block_ids_json = validated.evidence_ids
            existing.source = "agent"
            existing.provider = result.provider
            existing.model = result.model
            existing.prompt_version = prompt.version
            existing.status = "published" if auto_accept else "draft"
            existing.tokens_in = result.tokens_in
            existing.tokens_out = result.tokens_out
            existing.cost = result.cost
            existing.confidence = validated.confidence
            existing.auto_accepted = auto_accept
            existing.human_locked = False
            existing.edited_by = None
            existing.edited_at = None
            existing.updated_at = now
            entry["status"] = "updated"
            stats["updated"] += 1
        entry["summary_id"] = summary.id if existing is None else existing.id
        existing_map[section.key] = summary if existing is None else existing
        # 每节提交：一节失败不拖累其它节（失败可见，不阻塞）
        await session.commit()

    stats["cost_total"] = round(float(stats["cost_total"]), 8)
    return stats


# ---------------------------------------------------------------------------
# 读 / 编辑 / 发布 / 回滚
# ---------------------------------------------------------------------------
async def get_summary(session: AsyncSession, summary_id: str) -> Summary:
    summary = (
        await session.execute(select(Summary).where(Summary.id == summary_id))
    ).scalar_one_or_none()
    if summary is None:
        raise SummaryError("summary not found")
    return summary


async def evidence_blocks(session: AsyncSession, summary: Summary) -> list[dict]:
    ids = [str(i) for i in (summary.evidence_block_ids_json or [])]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Block)
            .where(Block.id.in_(ids), Block.paper_version_id == summary.paper_version_id)
            .order_by(Block.page, Block.order_idx)
        )
    ).scalars().all()
    order = {block_id: idx for idx, block_id in enumerate(ids)}
    rows = sorted(rows, key=lambda b: order.get(b.id, 999))
    return [
        {
            "block_id": b.id,
            "page": b.page,
            "type": b.type,
            "text": b.text or "",
            "bbox": b.bbox_json or [],
        }
        for b in rows
    ]


async def list_summaries(session: AsyncSession, paper_id: str) -> list[Summary]:
    version = await latest_version(session, paper_id)
    return list(
        (
            await session.execute(
                select(Summary)
                .where(Summary.paper_version_id == version.id)
                .order_by(Summary.created_at)
            )
        ).scalars().all()
    )


async def edit_summary(
    session: AsyncSession,
    summary_id: str,
    *,
    content: str,
    key_points: list[str] | None = None,
    actor: str = "human",
) -> Summary:
    summary = await get_summary(session, summary_id)
    text = (content or "").strip()
    if not text:
        raise SummaryError("摘要正文不能为空")
    _snapshot(session, summary, reason="edited")
    summary.content = text
    if key_points is not None:
        summary.key_points_json = [str(p).strip()[:200] for p in key_points if str(p).strip()]
    summary.source = "human_edited"
    summary.human_locked = True
    summary.status = "published"
    summary.auto_accepted = False
    summary.edited_by = actor
    summary.edited_at = datetime.now(timezone.utc)
    summary.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return summary


async def publish_summary(session: AsyncSession, summary_id: str, *, actor: str = "human") -> Summary:
    """把摘要标记为已发布（人工确认）。

    发布是「这篇内容可以用了」的动作，因此顺带**锁住**：自动生成不能再覆盖它，
    除非显式 force。不锁的话「一键总结整篇文献」会把用户刚发布的内容静默冲掉。
    内容本身没变，不需要留 revision。
    """
    summary = await get_summary(session, summary_id)
    now = datetime.now(timezone.utc)
    summary.status = "published"
    # 发布 = 人工确认：锁住自动覆盖；auto_accepted 只描述「生成时自动接受」，发布后不再成立
    summary.human_locked = True
    summary.auto_accepted = False
    if summary.source == "agent":
        # 内容确实还是模型写的，source 保持 agent；落一条人工动作的审计字段
        summary.edited_by = actor
        summary.edited_at = now
    summary.updated_at = now
    await session.commit()
    return summary


async def list_revisions(session: AsyncSession, summary_id: str) -> list[SummaryRevision]:
    await get_summary(session, summary_id)  # 404 语义
    return list(
        (
            await session.execute(
                select(SummaryRevision)
                .where(SummaryRevision.summary_id == summary_id)
                .order_by(SummaryRevision.created_at.desc())
            )
        ).scalars().all()
    )


async def restore_revision(
    session: AsyncSession, summary_id: str, revision_id: str, *, actor: str = "human"
) -> Summary:
    summary = await get_summary(session, summary_id)
    revision = (
        await session.execute(
            select(SummaryRevision).where(
                SummaryRevision.id == revision_id, SummaryRevision.summary_id == summary_id
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise SummaryError("revision not found")
    _snapshot(session, summary, reason="restored")
    summary.content = revision.content
    summary.key_points_json = revision.key_points_json or []
    summary.evidence_block_ids_json = revision.evidence_block_ids_json or []
    summary.source = "human_edited"
    summary.human_locked = True
    summary.status = "published"
    summary.auto_accepted = False
    summary.provider = revision.provider
    summary.model = revision.model
    summary.prompt_version = revision.prompt_version
    summary.confidence = revision.confidence
    summary.edited_by = actor
    summary.edited_at = datetime.now(timezone.utc)
    summary.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return summary


def summary_to_dict(summary: Summary) -> dict:
    return {
        "summary_id": summary.id,
        "paper_version_id": summary.paper_version_id,
        "section_key": summary.section_key,
        "section_title": summary.section_title,
        "level": summary.level,
        "content": summary.content,
        "key_points": summary.key_points_json or [],
        "evidence_block_ids": summary.evidence_block_ids_json or [],
        "source": summary.source,
        "provider": summary.provider,
        "model": summary.model,
        "prompt_version": summary.prompt_version,
        "status": summary.status,
        "auto_accepted": summary.auto_accepted,
        "human_locked": summary.human_locked,
        "tokens_in": summary.tokens_in,
        "tokens_out": summary.tokens_out,
        "cost": summary.cost,
        "confidence": summary.confidence,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "edited_by": summary.edited_by,
        "edited_at": summary.edited_at,
    }


def revision_to_dict(revision: SummaryRevision) -> dict:
    return {
        "revision_id": revision.id,
        "summary_id": revision.summary_id,
        "content": revision.content,
        "key_points": revision.key_points_json or [],
        "evidence_block_ids": revision.evidence_block_ids_json or [],
        "source": revision.source,
        "provider": revision.provider,
        "model": revision.model,
        "prompt_version": revision.prompt_version,
        "confidence": revision.confidence,
        "status": revision.status,
        "reason": revision.reason,
        "edited_by": revision.edited_by,
        "created_at": revision.created_at,
    }


__all__ = [
    "PROMPT_NAME",
    "SummaryError",
    "edit_summary",
    "evidence_blocks",
    "generate_summaries",
    "get_summary",
    "latest_version",
    "list_revisions",
    "list_summaries",
    "paper_sections",
    "publish_summary",
    "restore_revision",
    "revision_to_dict",
    "summaries_by_section",
    "summary_to_dict",
]
