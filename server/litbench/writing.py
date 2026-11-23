# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作侧（M5）：claim 卡片 / 对比矩阵 / LaTeX · BibTeX 导出 / 引用校验。

为什么先做这四件：

* **claim 卡片**是「阅读 → 写作」的最小闭环。用户在摘要/图表上看到一句能用的结论，
  把它钉成 claim，证据跟着走；导出时自然带 ``\\cite{}``，不需要再手工回查。
* **对比矩阵**不落库：它完全由现有 paper / summary / figure / claim 派生，
  存下来只会随源数据过期。先给「不用配置也能看」的版本。
* **导出**只吐「能编译的骨架」：LaTeX 正文 + refs.bib 两份，不做模板市场。
* **引用校验**只做确定性解析（cite_key / DOI / arXiv / 文献题名兜底），
  不调模型——写错了要能解释为什么错。

三条硬约束（延续 docs/15 的原则）：

1. claim 的 ``evidence_json`` 至少一条，且每条都能在本库解析到（block/figure/table/summary）；
2. 证据必须属于**同一 paper_version**（引用要指向你实际读到的那版）；
3. 导出/校验都是只读派生，不写库；只有 claim CRUD 落表。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from types import SimpleNamespace

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AppUser,
    Block,
    Claim,
    ClaimRevision,
    Figure,
    Paper,
    PaperVersion,
    Summary,
    TableBlock,
)
from .team import private_claim_condition, scoped_user, visible_paper_condition

# 一句话论断的长度上限：再长就不是「卡片」而是段落，写进 LaTeX 也没法维护
MAX_CLAIM_CHARS = 2000
MIN_CLAIM_CHARS = 2
MAX_EVIDENCE = 20
MAX_TAGS = 10
MAX_TAG_CHARS = 32
# 矩阵里摘要/发现的截断长度：够扫一眼做对比，全文去摘要页签看
MATRIX_TEXT_MAX = 260
# 导出时最多带多少条 claim / 矩阵最多几篇（防止误点把整库导出成一个 .tex）
MAX_EXPORT_CLAIMS = 500
MAX_MATRIX_PAPERS = 8

# 对比矩阵的可选维度（2026-11-15 小修）：默认仍是原来的 7 行，
# cite/type/tags 是可选维度；顺序即渲染顺序，不落库。
MATRIX_DIMENSIONS: tuple[dict, ...] = (
    {"key": "title", "label": "题目", "description": "文献标题", "default": True},
    {"key": "authors", "label": "作者", "description": "作者列表", "default": True},
    {"key": "meta", "label": "年份 / 来源", "description": "年份与期刊/会议", "default": True},
    {"key": "abstract", "label": "摘要", "description": "论文摘要或整篇速览", "default": True},
    {"key": "findings", "label": "关键发现", "description": "整篇速览的要点", "default": True},
    {"key": "assets", "label": "图 / 表", "description": "图表数量", "default": True},
    {"key": "claims", "label": "我的论断", "description": "写作卡片", "default": True},
    {
        "key": "cite",
        "label": "引用键",
        "description": "cite_key（写 \\cite 时用）",
        "default": False,
    },
    {
        "key": "type",
        "label": "条目类型",
        "description": "期刊 / 会议 / 学位论文等",
        "default": False,
    },
    {"key": "tags", "label": "标签", "description": "文献标签", "default": False},
)
MATRIX_DIMENSION_KEYS = tuple(item["key"] for item in MATRIX_DIMENSIONS)
DEFAULT_MATRIX_DIMENSIONS = tuple(
    item["key"] for item in MATRIX_DIMENSIONS if item["default"]
)

_ENTRY_TYPE_LABELS = {
    "journal": "期刊",
    "conference": "会议",
    "preprint": "预印本",
    "thesis": "学位论文",
    "book": "图书",
    "report": "报告",
    "other": "其它",
}

EVIDENCE_TYPES = ("block", "figure", "table", "summary")
CLAIM_STATUSES = ("draft", "approved")
CLAIM_SOURCES = ("human", "agent")

DEFAULT_SECTION = "正文"


class ClaimError(RuntimeError):
    """claim / 导出 / 校验的可读错误。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def _clip(text: str | None, limit: int) -> str:
    value = (text or "").strip().replace("\n", " ")
    return value[:limit]


def _author_names(paper: Paper) -> list[str]:
    names: list[str] = []
    for author in paper.authors_json or []:
        if isinstance(author, str):
            name = author.strip()
        elif isinstance(author, dict):
            name = str(
                author.get("name")
                or " ".join(
                    str(author.get(key) or "").strip()
                    for key in ("firstName", "lastName")
                    if author.get(key)
                )
            ).strip()
        else:
            name = ""
        if name:
            names.append(name)
    return names


def _paper_cite_key(paper: Paper) -> str:
    """导出用引用键：优先 paper.cite_key，没有就用稳定兜底（并让校验能识别）。"""
    return (paper.cite_key or f"litbench-{paper.id[:8]}").strip()


def _sort_key_claim(claim: Claim) -> tuple:
    return (claim.section or DEFAULT_SECTION, claim.order_idx, claim.created_at or _now())


def claim_to_dict(claim: Claim, paper: Paper | None = None) -> dict:
    return {
        "claim_id": claim.id,
        "paper_id": claim.paper_id,
        "paper_title": paper.title if paper is not None else None,
        "cite_key": paper.cite_key if paper is not None else None,
        "paper_version_id": claim.paper_version_id,
        "text": claim.text or "",
        "section": claim.section or DEFAULT_SECTION,
        "status": claim.status or "draft",
        "source": claim.source or "human",
        "evidence": list(claim.evidence_json or []),
        "evidence_stale": bool(claim.evidence_stale),
        "tags": list(claim.tags_json or []),
        "order_idx": claim.order_idx or 0,
        "created_by": claim.created_by,
        "created_at": claim.created_at,
        "updated_at": claim.updated_at,
    }


def _claim_state(claim: Claim) -> dict:
    """claim 的可编辑字段快照（JSON 列表做浅拷贝，避免和 claim 共享引用）。"""
    return {
        "text": claim.text or "",
        "section": claim.section or "",
        "status": claim.status or "draft",
        "source": claim.source or "human",
        "evidence_json": list(claim.evidence_json or []),
        "tags_json": list(claim.tags_json or []),
        "order_idx": int(claim.order_idx or 0),
        "paper_version_id": claim.paper_version_id,
    }


def _claim_snapshot(
    session: AsyncSession,
    claim: Claim,
    state: dict,
    *,
    reason: str,
    edited_by: str | None,
) -> None:
    session.add(
        ClaimRevision(
            claim_id=claim.id,
            text=state["text"],
            section=state["section"],
            status=state["status"],
            source=state["source"],
            evidence_json=state["evidence_json"],
            tags_json=state["tags_json"],
            order_idx=state["order_idx"],
            paper_version_id=state["paper_version_id"],
            reason=reason,
            edited_by=edited_by,
            created_at=_now(),
        )
    )


def revision_to_dict(revision: ClaimRevision) -> dict:
    return {
        "revision_id": revision.id,
        "claim_id": revision.claim_id,
        "text": revision.text or "",
        "section": revision.section or DEFAULT_SECTION,
        "status": revision.status or "draft",
        "source": revision.source or "human",
        "evidence": list(revision.evidence_json or []),
        "tags": list(revision.tags_json or []),
        "order_idx": int(revision.order_idx or 0),
        "paper_version_id": revision.paper_version_id,
        "reason": revision.reason or "edited",
        "edited_by": revision.edited_by,
        "created_at": revision.created_at,
    }


def _clean_tags(tags: list | None) -> list[str]:
    out: list[str] = []
    for raw in tags or []:
        value = str(raw).strip()
        if not value:
            continue
        if len(value) > MAX_TAG_CHARS:
            raise ClaimError(f"标签太长（最多 {MAX_TAG_CHARS} 字）：{value[:40]}")
        if value not in out:
            out.append(value)
    if len(out) > MAX_TAGS:
        raise ClaimError(f"标签最多 {MAX_TAGS} 个")
    return out


def _clean_section(section: str | None) -> str:
    value = (section or "").strip()
    if len(value) > 255:
        raise ClaimError("章节名太长（最多 255 字）")
    return value


# ---------------------------------------------------------------------------
# 证据解析：每条证据都必须能落到本库的具体对象上
# ---------------------------------------------------------------------------
async def latest_version(session: AsyncSession, paper_id: str) -> PaperVersion | None:
    return (
        await session.execute(
            select(PaperVersion)
            .where(PaperVersion.paper_id == paper_id)
            .order_by(PaperVersion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _resolve_evidence(
    session: AsyncSession, version: PaperVersion, item: dict
) -> dict:
    kind = str(item.get("type") or "").strip().lower()
    target_id = str(item.get("id") or "").strip()
    if kind not in EVIDENCE_TYPES:
        raise ClaimError(f"证据类型只支持 {' / '.join(EVIDENCE_TYPES)}，收到 {kind!r}")
    if not target_id:
        raise ClaimError(f"证据 {kind} 缺少 id")

    if kind == "block":
        block = await session.get(Block, target_id)
        if block is None or block.paper_version_id != version.id:
            raise ClaimError(f"证据 block 不存在或不属于当前版本：{target_id}")
        return {
            "type": "block",
            "id": block.id,
            "page": block.page,
            "label": f"p{block.page} {block.type}",
            "snippet": _clip(block.text, 120),
        }
    if kind == "figure":
        figure = await session.get(Figure, target_id)
        if figure is None or figure.paper_version_id != version.id or figure.deleted_at is not None:
            raise ClaimError(f"证据 figure 不存在或不属于当前版本：{target_id}")
        return {
            "type": "figure",
            "id": figure.id,
            "page": figure.page,
            "label": figure.label_raw or figure.label_norm or "图（未编号）",
            "snippet": _clip(figure.caption, 120),
        }
    if kind == "table":
        table = await session.get(TableBlock, target_id)
        if table is None or table.paper_version_id != version.id:
            raise ClaimError(f"证据 table 不存在或不属于当前版本：{target_id}")
        return {
            "type": "table",
            "id": table.id,
            "page": table.page,
            "label": table.label_raw or table.label_norm or "表（未编号）",
            "snippet": _clip(table.caption, 120),
        }
    summary = await session.get(Summary, target_id)
    if summary is None or summary.paper_version_id != version.id:
        raise ClaimError(f"证据 summary 不存在或不属于当前版本：{target_id}")
    return {
        "type": "summary",
        "id": summary.id,
        "page": None,
        "label": summary.section_title or summary.section_key,
        "snippet": _clip(summary.content, 120),
    }


async def normalize_evidence(
    session: AsyncSession, paper: Paper, raw: list | None
) -> tuple[list[dict], str | None]:
    """校验并标准化证据列表，返回 ``(evidence, paper_version_id)``。"""
    if not raw:
        raise ClaimError("claim 必须带至少一条证据（block / figure / table / summary）")
    if len(raw) > MAX_EVIDENCE:
        raise ClaimError(f"证据最多 {MAX_EVIDENCE} 条")
    version = await latest_version(session, paper.id)
    if version is None:
        raise ClaimError(
            "这篇文献还没有解析版本（没有 PDF 或还没解析完）；写作证据必须落在已解析的版面上"
        )

    evidence: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ClaimError("证据格式不对：每条都要是 {type, id} 对象")
        resolved = await _resolve_evidence(session, version, item)
        key = (resolved["type"], resolved["id"])
        if key in seen:
            continue
        seen.add(key)
        evidence.append(resolved)
    if not evidence:
        raise ClaimError("claim 必须带至少一条证据")
    return evidence, version.id


# ---------------------------------------------------------------------------
# claim CRUD
# ---------------------------------------------------------------------------
async def get_claim(session: AsyncSession, claim_id: str) -> Claim:
    claim = await session.get(Claim, claim_id)
    if claim is None:
        raise ClaimError("claim not found")
    return claim


async def list_claims(
    session: AsyncSession,
    *,
    paper_id: str | None = None,
    section: str | None = None,
    status: str | None = None,
    q: str | None = None,
    user: AppUser | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    stmt = select(Claim, Paper).join(Paper, Claim.paper_id == Paper.id)
    current = scoped_user(user)
    if current is not None:
        # 写作卡片归个人：只看自己的；NULL-owner 旧卡跟随文献可见性（F2），
        # 不能让别人私有文献上的旧卡跨库泄露。
        stmt = stmt.where(
            or_(
                Claim.owner_user_id == current.id,
                and_(Claim.owner_user_id.is_(None), visible_paper_condition(user)),
            )
        )
    if paper_id:
        stmt = stmt.where(Claim.paper_id == paper_id)
    if section:
        stmt = stmt.where(Claim.section == section)
    if status:
        if status not in CLAIM_STATUSES:
            raise ClaimError(f"status 只支持 {' / '.join(CLAIM_STATUSES)}")
        stmt = stmt.where(Claim.status == status)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Claim.text.ilike(like), Claim.section.ilike(like)))
    stmt = (
        stmt.order_by(Claim.section.asc(), Claim.order_idx.asc(), Claim.created_at.asc())
        .limit(max(1, min(limit, 500)))
        .offset(max(0, offset))
    )
    rows = (await session.execute(stmt)).all()
    return [claim_to_dict(claim, paper) for claim, paper in rows]


async def create_claim(
    session: AsyncSession,
    *,
    paper_id: str,
    text: str,
    section: str | None = None,
    evidence: list | None = None,
    tags: list | None = None,
    status: str = "draft",
    source: str = "human",
    created_by: str | None = None,
    owner_user_id: str | None = None,
    order_idx: int | None = None,
) -> dict:
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise ClaimError("paper not found")
    body = (text or "").strip()
    if len(body) < MIN_CLAIM_CHARS:
        raise ClaimError("claim 内容太短")
    if len(body) > MAX_CLAIM_CHARS:
        raise ClaimError(f"claim 太长（最多 {MAX_CLAIM_CHARS} 字）")
    if status not in CLAIM_STATUSES:
        raise ClaimError(f"status 只支持 {' / '.join(CLAIM_STATUSES)}")
    if source not in CLAIM_SOURCES:
        raise ClaimError(f"source 只支持 {' / '.join(CLAIM_SOURCES)}")
    normalized, version_id = await normalize_evidence(session, paper, evidence)
    if order_idx is None:
        max_idx = (
            await session.execute(
                select(func.max(Claim.order_idx)).where(Claim.paper_id == paper_id)
            )
        ).scalar_one_or_none()
        order_idx = int(max_idx or 0) + 1

    claim = Claim(
        paper_id=paper_id,
        paper_version_id=version_id,
        text=body,
        section=_clean_section(section),
        status=status,
        source=source,
        evidence_json=normalized,
        tags_json=_clean_tags(tags),
        order_idx=order_idx,
        created_by=created_by,
        owner_user_id=owner_user_id,
    )
    session.add(claim)
    await session.commit()
    await session.refresh(claim)
    return claim_to_dict(claim, paper)


async def update_claim(
    session: AsyncSession,
    claim_id: str,
    *,
    text: str | None = None,
    section: str | None = None,
    evidence: list | None = None,
    tags: list | None = None,
    status: str | None = None,
    order_idx: int | None = None,
    edited_by: str | None = None,
) -> dict:
    claim = await get_claim(session, claim_id)
    paper = await session.get(Paper, claim.paper_id)
    if paper is None:  # pragma: no cover - 外键保证
        raise ClaimError("paper not found")

    # 先记旧状态：只有内容真正变化才留 revision（反复保存同一份不算一次编辑）
    before = _claim_state(claim)
    if text is not None:
        body = text.strip()
        if len(body) < MIN_CLAIM_CHARS:
            raise ClaimError("claim 内容太短")
        if len(body) > MAX_CLAIM_CHARS:
            raise ClaimError(f"claim 太长（最多 {MAX_CLAIM_CHARS} 字）")
        claim.text = body
    if section is not None:
        claim.section = _clean_section(section)
    if tags is not None:
        claim.tags_json = _clean_tags(tags)
    if evidence is not None:
        normalized, version_id = await normalize_evidence(session, paper, evidence)
        claim.evidence_json = normalized
        claim.paper_version_id = version_id
        claim.evidence_stale = False
    if status is not None:
        if status not in CLAIM_STATUSES:
            raise ClaimError(f"status 只支持 {' / '.join(CLAIM_STATUSES)}")
        claim.status = status
    if order_idx is not None:
        claim.order_idx = order_idx
    if _claim_state(claim) != before:
        _claim_snapshot(
            session, claim, before, reason="edited", edited_by=edited_by
        )
        claim.updated_at = _now()
    await session.commit()
    await session.refresh(claim)
    return claim_to_dict(claim, paper)


async def list_claim_revisions(
    session: AsyncSession, claim_id: str
) -> list[ClaimRevision]:
    """该 claim 的历史版本（新→旧）；当前内容不在列表里。"""
    await get_claim(session, claim_id)
    rows = (
        await session.execute(
            select(ClaimRevision)
            .where(ClaimRevision.claim_id == claim_id)
            .order_by(ClaimRevision.created_at.desc(), ClaimRevision.id.desc())
        )
    ).scalars().all()
    return list(rows)


async def restore_claim_revision(
    session: AsyncSession,
    claim_id: str,
    revision_id: str,
    *,
    edited_by: str | None = None,
) -> dict:
    """把 claim 恢复到某个历史版本；恢复前先压一份当前状态，历史链不断。"""
    claim = await get_claim(session, claim_id)
    paper = await session.get(Paper, claim.paper_id)
    if paper is None:  # pragma: no cover - 外键保证
        raise ClaimError("paper not found")
    revision = (
        await session.execute(
            select(ClaimRevision).where(
                ClaimRevision.id == revision_id,
                ClaimRevision.claim_id == claim_id,
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise ClaimError("revision not found")
    _claim_snapshot(
        session,
        claim,
        _claim_state(claim),
        reason="restored",
        edited_by=edited_by,
    )
    claim.text = revision.text or ""
    claim.section = revision.section or ""
    claim.status = revision.status if revision.status in CLAIM_STATUSES else "draft"
    claim.evidence_json = list(revision.evidence_json or [])
    claim.tags_json = list(revision.tags_json or [])
    claim.order_idx = int(revision.order_idx or 0)
    claim.paper_version_id = revision.paper_version_id
    claim.updated_at = _now()
    await session.commit()
    await session.refresh(claim)
    return claim_to_dict(claim, paper)


async def delete_claim(session: AsyncSession, claim_id: str) -> None:
    claim = await get_claim(session, claim_id)
    await session.delete(claim)
    await session.commit()


# ---------------------------------------------------------------------------
# 对比矩阵（派生，不落库）
# ---------------------------------------------------------------------------
def list_matrix_dimensions() -> list[dict]:
    """矩阵可选维度清单（默认维度带 ``default=true``）。"""
    return [dict(item) for item in MATRIX_DIMENSIONS]


def _clean_matrix_dimensions(raw: list[str] | None) -> list[str]:
    """校验/去重维度列表；不传 = 默认 7 行，顺序保留调用方顺序。"""
    if not raw:
        return list(DEFAULT_MATRIX_DIMENSIONS)
    out: list[str] = []
    for value in raw:
        for part in str(value).split(","):
            key = part.strip()
            if not key:
                continue
            if key not in MATRIX_DIMENSION_KEYS:
                raise ClaimError(
                    f"未知矩阵维度：{key}（可选：{', '.join(MATRIX_DIMENSION_KEYS)}）"
                )
            if key not in out:
                out.append(key)
    return out or list(DEFAULT_MATRIX_DIMENSIONS)


async def build_matrix(
    session: AsyncSession,
    paper_ids: list[str],
    *,
    dimensions: list[str] | None = None,
    user: AppUser | None = None,
) -> dict:
    dims = _clean_matrix_dimensions(dimensions)
    ids: list[str] = []
    for raw in paper_ids:
        value = str(raw).strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        raise ClaimError("至少选一篇文献")
    if len(ids) > MAX_MATRIX_PAPERS:
        raise ClaimError(f"对比矩阵最多 {MAX_MATRIX_PAPERS} 篇")

    papers = {
        paper.id: paper
        for paper in (
            await session.execute(select(Paper).where(Paper.id.in_(ids)))
        ).scalars()
    }
    missing = [paper_id for paper_id in ids if paper_id not in papers]
    if missing:
        raise ClaimError(f"找不到文献：{', '.join(missing)}")

    columns: list[dict] = []
    findings: list[str] = []
    abstracts: list[str] = []
    claim_texts: list[str] = []
    figure_cells: list[str] = []
    meta_cells: list[str] = []
    author_cells: list[str] = []
    title_cells: list[str] = []
    cite_cells: list[str] = []
    type_cells: list[str] = []
    tag_cells: list[str] = []

    for paper_id in ids:
        paper = papers[paper_id]
        version = await latest_version(session, paper_id)
        version_id = version.id if version else None

        summary = None
        if version_id:
            summary = (
                await session.execute(
                    select(Summary)
                    .where(Summary.paper_version_id == version_id)
                    .order_by(Summary.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        claims = (
            await session.execute(
                select(Claim)
                # 「我的论断」行只放调用者自己的卡（+ 可见文献上的旧卡）：
                # 共享文献能进矩阵，但别人挂在上面的私有卡片不能跟着泄露（F1）。
                .where(Claim.paper_id == paper_id, private_claim_condition(user))
                .order_by(Claim.section.asc(), Claim.order_idx.asc())
                .limit(8)
            )
        ).scalars().all()
        figure_count = 0
        table_count = 0
        if version_id:
            figure_count = (
                await session.execute(
                    select(func.count(Figure.id)).where(
                        Figure.paper_version_id == version_id, Figure.deleted_at.is_(None)
                    )
                )
            ).scalar_one()
            table_count = (
                await session.execute(
                    select(func.count(TableBlock.id)).where(
                        TableBlock.paper_version_id == version_id
                    )
                )
            ).scalar_one()

        authors = _author_names(paper)
        columns.append(
            {
                "paper_id": paper_id,
                "title": paper.title or "（无题）",
                "cite_key": paper.cite_key,
                "year": paper.year,
                "venue": paper.venue,
                "authors": authors,
                "has_version": version is not None,
                "summary_id": summary.id if summary else None,
                "figure_count": int(figure_count or 0),
                "table_count": int(table_count or 0),
                "claim_count": len(claims),
            }
        )
        title_cells.append(paper.title or "（无题）")
        author_cells.append(
            "、".join(authors[:5]) + (" 等" if len(authors) > 5 else "")
            if authors
            else "（未著录）"
        )
        meta_bits = [str(paper.year) if paper.year else "", paper.venue or ""]
        meta_cells.append(" · ".join(bit for bit in meta_bits if bit))
        if paper.abstract:
            abstracts.append(_clip(paper.abstract, MATRIX_TEXT_MAX))
        elif summary is not None and summary.content:
            abstracts.append(_clip(summary.content, MATRIX_TEXT_MAX))
        else:
            abstracts.append("（还没有摘要）")
        if summary is not None and summary.key_points_json:
            points = [str(point).strip() for point in summary.key_points_json if str(point).strip()]
            findings.append("；".join(points[:6])[:MATRIX_TEXT_MAX * 2])
        else:
            findings.append("（还没有整篇速览/摘要要点）")
        figure_cells.append(f"{int(figure_count or 0)} 图 · {int(table_count or 0)} 表")
        cite_cells.append(paper.cite_key or "（无 cite_key）")
        entry_type = paper.entry_type or "journal"
        type_cells.append(_ENTRY_TYPE_LABELS.get(entry_type, entry_type))
        tags = [str(tag).strip() for tag in (paper.tags_json or []) if str(tag).strip()]
        tag_cells.append("、".join(tags) if tags else "（无标签）")
        if claims:
            claim_texts.append(
                "；".join(_clip(claim.text, 80) for claim in claims[:5])
            )
        else:
            claim_texts.append("（还没有写作卡片）")

    def row(key: str, label: str, values: list[str]) -> dict:
        return {
            "key": key,
            "label": label,
            "values": [
                {"paper_id": paper_id, "text": value}
                for paper_id, value in zip(ids, values, strict=True)
            ],
        }

    row_specs = {
        "title": ("题目", title_cells),
        "authors": ("作者", author_cells),
        "meta": ("年份 / 来源", meta_cells),
        "abstract": ("摘要", abstracts),
        "findings": ("关键发现", findings),
        "assets": ("图 / 表", figure_cells),
        "claims": ("我的论断", claim_texts),
        "cite": ("引用键", cite_cells),
        "type": ("条目类型", type_cells),
        "tags": ("标签", tag_cells),
    }
    return {
        "paper_ids": ids,
        "papers": columns,
        "rows": [
            row(key, row_specs[key][0], row_specs[key][1])
            for key in dims
        ],
        "note": "矩阵由 bibliography、summary、figure/table、claim 实时派生，不落库；维度可选。",
    }


# ---------------------------------------------------------------------------
# 导出：LaTeX / BibTeX / Markdown
# ---------------------------------------------------------------------------
_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _latex_escape(text: str) -> str:
    return "".join(_LATEX_ESCAPES.get(char, char) for char in text)


_CITE_RE = re.compile(
    r"\\(?:cite|citep|citet|citealp|citealt|parencite|textcite|autocite)\*?"
    r"(?:\[[^\]]*\])*\{([^}]*)\}"
)


def _bibtex_escape(text: str) -> str:
    return (
        text.replace("\\", r"\textbackslash{}")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("_", r"\_")
        .replace("#", r"\#")
    )


def _bibtex_entry(paper: Paper) -> str:
    key = _paper_cite_key(paper)
    authors = _author_names(paper)
    fields: list[tuple[str, str]] = [("title", paper.title or "Untitled")]
    if authors:
        fields.append(("author", " and ".join(authors)))
    if paper.year:
        fields.append(("year", str(paper.year)))
    if paper.venue:
        fields.append(("journal", paper.venue))
    if paper.doi:
        fields.append(("doi", paper.doi))
    if paper.arxiv_id:
        fields.append(("eprint", paper.arxiv_id))
        fields.append(("archivePrefix", "arXiv"))
    body = ",\n".join(f"  {name} = {{{_bibtex_escape(value)}}}" for name, value in fields)
    return f"@article{{{key},\n{body}\n}}"


def _claim_export_text(claim: Claim, paper: Paper | None, *, latex: bool) -> str:
    text = (claim.text or "").strip()
    if latex:
        text = _latex_escape(text)
    cite_key = _paper_cite_key(paper) if paper is not None else ""
    if cite_key and "\\cite" not in text and f"[@{cite_key}]" not in text:
        text = f"{text}~\\cite{{{cite_key}}}" if latex else f"{text}[@{cite_key}]"
    return text


async def _load_export_claims(
    session: AsyncSession,
    *,
    claim_ids: list[str] | None,
    paper_ids: list[str] | None,
    include_drafts: bool,
    owner_user_id: str | None = None,
    user: AppUser | None = None,
) -> list[tuple[Claim, Paper]]:
    stmt = select(Claim, Paper).join(Paper, Claim.paper_id == Paper.id)
    # 文献可见性：撤共享/移出工作组后，旧 claim 不能靠导出接口把元数据带出去
    stmt = stmt.where(visible_paper_condition(user))
    if owner_user_id:
        stmt = stmt.where(
            or_(Claim.owner_user_id == owner_user_id, Claim.owner_user_id.is_(None))
        )
    if claim_ids:
        stmt = stmt.where(Claim.id.in_(claim_ids))
    if paper_ids:
        stmt = stmt.where(Claim.paper_id.in_(paper_ids))
    if not include_drafts:
        stmt = stmt.where(Claim.status == "approved")
    stmt = stmt.order_by(Claim.section.asc(), Claim.order_idx.asc(), Claim.created_at.asc())
    # 多取一条用来判断是否超过上限，避免静默截断
    stmt = stmt.limit(MAX_EXPORT_CLAIMS + 1)
    rows = (await session.execute(stmt)).all()
    return [(claim, paper) for claim, paper in rows]


async def export_writing(
    session: AsyncSession,
    *,
    fmt: str,
    paper_ids: list[str] | None = None,
    claim_ids: list[str] | None = None,
    title: str = "",
    section_order: list[str] | None = None,
    include_drafts: bool = True,
    with_evidence_comments: bool = False,
    owner_user_id: str | None = None,
    user: AppUser | None = None,
) -> dict:
    fmt = (fmt or "latex").strip().lower()
    if fmt not in ("latex", "bibtex", "markdown"):
        raise ClaimError("format 只支持 latex / bibtex / markdown")
    rows = await _load_export_claims(
        session,
        claim_ids=claim_ids,
        paper_ids=paper_ids,
        include_drafts=include_drafts,
        owner_user_id=owner_user_id,
        user=user,
    )
    if len(rows) > MAX_EXPORT_CLAIMS:
        raise ClaimError(
            f"一次最多导出 {MAX_EXPORT_CLAIMS} 张卡片；请用 claim_ids / paper_ids 缩小范围"
        )
    if not rows and fmt != "bibtex":
        raise ClaimError("没有可导出的 claim；先写几张卡片，或放宽「包含草稿」")

    # 论文去重（保持 claim 出现顺序）
    papers: dict[str, Paper] = {}
    for _claim, paper in rows:
        papers.setdefault(paper.id, paper)
    # BibTeX 是「按文献出清单」，没有 claim 也要能导出；LaTeX 只给正文实际引用到的文献。
    if fmt == "bibtex" and paper_ids:
        extra = (
            await session.execute(select(Paper).where(Paper.id.in_(paper_ids)))
        ).scalars()
        for paper in extra:
            papers.setdefault(paper.id, paper)

    if fmt == "bibtex":
        header = "% refs.bib — 由 LitBench 从文献库导出（cite_key 与库内一致）\n\n"
        content = header + "\n\n".join(_bibtex_entry(paper) for paper in papers.values())
        return {
            "format": "bibtex",
            "filename": "refs.bib",
            "content": content + "\n",
            "extra_files": {},
            "claim_count": 0,
            "paper_count": len(papers),
        }

    # 分组：显式 section_order 优先，其余按首次出现顺序
    groups: dict[str, list[tuple[Claim, Paper]]] = {}
    for claim, paper in rows:
        section = (claim.section or DEFAULT_SECTION).strip() or DEFAULT_SECTION
        groups.setdefault(section, []).append((claim, paper))
    order: list[str] = []
    for section in section_order or []:
        if section in groups and section not in order:
            order.append(section)
    for section in groups:
        if section not in order:
            order.append(section)

    safe_title = title.strip() or "LitBench 写作草稿"
    notes = [
        "% 由 LitBench 生成：正文来自写作台 claim 卡片，引用键来自文献库 cite_key。",
        f"% 生成时间：{_now().isoformat(timespec='seconds')}",
    ]
    if fmt == "markdown":
        lines = [f"# {safe_title}", ""]
        for section in order:
            lines.append(f"## {section}")
            lines.append("")
            for claim, paper in groups[section]:
                text = _claim_export_text(claim, paper, latex=False)
                evidence = ""
                if with_evidence_comments:
                    evidence = " ^[" + ", ".join(
                        f"{item.get('type')}:{item.get('id')}" for item in claim.evidence_json or []
                    ) + "]"
                lines.append(f"{text}{evidence}")
                lines.append("")
        return {
            "format": "markdown",
            "filename": f"{_safe_filename(safe_title)}.md",
            "content": "\n".join(lines).rstrip() + "\n",
            "extra_files": {},
            "claim_count": len(rows),
            "paper_count": len(papers),
        }

    lines = [
        *notes,
        r"\documentclass[UTF8,11pt]{article}",
        r"\usepackage{ctex}",
        r"\usepackage{geometry}",
        r"\geometry{margin=2.5cm}",
        r"\usepackage{hyperref}",
        r"\usepackage{natbib}",
        rf"\title{{{_latex_escape(safe_title)}}}",
        r"\author{}",
        r"\date{\today}",
        r"\begin{document}",
        r"\maketitle",
    ]
    for section in order:
        lines.append(rf"\section{{{_latex_escape(section)}}}")
        for claim, paper in groups[section]:
            if with_evidence_comments:
                evidence = ", ".join(
                    f"{item.get('type')}:{item.get('id')}" for item in claim.evidence_json or []
                )
                lines.append(rf"% [claim {claim.id} | 证据 {evidence}]")
            lines.append(_claim_export_text(claim, paper, latex=True) + "\n")
    lines.extend(
        [
            r"\bibliographystyle{plainnat}",
            r"\bibliography{refs}",
            r"\end{document}",
            "",
        ]
    )
    bib = "\n\n".join(_bibtex_entry(paper) for paper in papers.values()) + "\n"
    return {
        "format": "latex",
        "filename": f"{_safe_filename(safe_title)}.tex",
        "content": "\n".join(lines),
        "extra_files": {"refs.bib": bib},
        "claim_count": len(rows),
        "paper_count": len(papers),
    }


def _safe_filename(title: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "-", title.strip())[:60].strip("-")
    return cleaned or "litbench-draft"


# ---------------------------------------------------------------------------
# 引用校验（确定性；不调模型）
# ---------------------------------------------------------------------------
def _resolve_key(all_papers: list[Paper], key: str) -> Paper | None:
    needle = key.strip()
    if not needle:
        return None
    lowered = needle.lower()
    for paper in all_papers:
        if (paper.cite_key or "").lower() == lowered:
            return paper
    # 兼容 arXiv:xxxx / doi:xxxx / 裸 id 三种写法
    bare = lowered
    for prefix in ("arxiv:", "doi:"):
        bare = bare.removeprefix(prefix)
    for paper in all_papers:
        if paper.arxiv_id and paper.arxiv_id.lower() == bare:
            return paper
        if paper.doi and paper.doi.lower() == bare:
            return paper
    for paper in all_papers:
        if (paper.cite_key or "").lower().endswith(":" + bare) and bare:
            return paper
    return None


def _suggest_key(all_papers: list[Paper], key: str) -> str | None:
    best: tuple[float, str] | None = None
    for paper in all_papers:
        candidate = paper.cite_key or ""
        if not candidate:
            continue
        ratio = SequenceMatcher(None, key.lower(), candidate.lower()).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, candidate)
    if best is not None and best[0] >= 0.6:
        return best[1]
    # 相似度不够时按「前缀家族」兜底：arXiv:9999.99999 至少提示库里唯一的 arXiv 条目
    prefix = key.split(":", 1)[0].lower() if ":" in key else ""
    if prefix:
        for paper in all_papers:
            candidate = paper.cite_key or ""
            if candidate.lower().startswith(prefix + ":"):
                return candidate
    return None


async def visible_paper_index(
    session: AsyncSession, user: AppUser | None = None
) -> list[SimpleNamespace]:
    """当前用户可见文献的轻量索引（id/cite_key/doi/arxiv/title），用于引用键解析。

    不 fetch 完整 Paper、不做 300 篇截断：解析 \\cite 只需要这几列；比
    ``thesis._load_visible_papers`` 更适合「先解析键、再按需取文献」的场景。
    """
    rows = (
        await session.execute(
            select(
                Paper.id, Paper.cite_key, Paper.doi, Paper.arxiv_id, Paper.title
            ).where(visible_paper_condition(user))
        )
    ).all()
    return [
        SimpleNamespace(
            id=row.id,
            cite_key=row.cite_key,
            doi=row.doi,
            arxiv_id=row.arxiv_id,
            title=row.title,
        )
        for row in rows
    ]


async def check_citations(
    session: AsyncSession, text: str, *, user: AppUser | None = None
) -> dict:
    """解析正文里的 ``\\cite`` 键；只对照当前用户能看见的文献（不泄露别人的私有库）。"""
    body = text or ""
    keys: list[str] = []
    for match in _CITE_RE.finditer(body):
        for key in match.group(1).split(","):
            value = key.strip()
            if value and value not in keys:
                keys.append(value)
    all_papers = list(
        (
            await session.execute(
                select(Paper).where(visible_paper_condition(user))
            )
        ).scalars()
    )
    results: list[dict] = []
    for key in keys:
        paper = _resolve_key(all_papers, key)
        if paper is None:
            results.append(
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
            results.append(
                {
                    "key": key,
                    "status": "resolved",
                    "paper_id": paper.id,
                    "title": paper.title,
                    "cite_key": paper.cite_key,
                    "suggestion": None,
                }
            )
    unknown = [item for item in results if item["status"] == "unknown"]
    return {
        "total": len(results),
        "resolved": len(results) - len(unknown),
        "unknown": len(unknown),
        "citations": results,
        "message": (
            "全部引用都能在文献库里找到"
            if results and not unknown
            else (
                "没有检测到 \\cite{} 引用"
                if not results
                else f"{len(unknown)} 条引用在库里找不到"
            )
        ),
    }


__all__ = [
    "CLAIM_SOURCES",
    "CLAIM_STATUSES",
    "DEFAULT_MATRIX_DIMENSIONS",
    "DEFAULT_SECTION",
    "EVIDENCE_TYPES",
    "MATRIX_DIMENSIONS",
    "MATRIX_DIMENSION_KEYS",
    "MAX_CLAIM_CHARS",
    "ClaimError",
    "build_matrix",
    "check_citations",
    "claim_to_dict",
    "create_claim",
    "delete_claim",
    "export_writing",
    "get_claim",
    "latest_version",
    "list_claim_revisions",
    "list_claims",
    "list_matrix_dimensions",
    "normalize_evidence",
    "restore_claim_revision",
    "revision_to_dict",
    "update_claim",
    "visible_paper_index",
]
