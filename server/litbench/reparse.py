# SPDX-License-Identifier: AGPL-3.0-or-later
"""重解析后的 AI 产物重锚。

``workers/run.py`` 的 ``persist`` 是「先删后插」：该版本的全部 Block / Figure 被物理
删除，再插入一批**全新 UUID**。而 AI 产物用旧 id 记锚点：

* ``summary.evidence_block_ids_json`` / ``summary_revision.evidence_block_ids_json``；
* ``figure_proposal.figure_id``（外键指向 ``figure.id``）；
* ``claim.evidence_json`` 里 block / figure / table / summary 的 id。

不迁移的话，重复上传同一份 PDF、按需升级重解析之后，这些产物会全部悬空——摘要点不开
证据、图纠错提案指向不存在的图。这里在删除前快照、插入后按稳定身份重锚：

* 摘要 / 历史版本：证据块按 ``(page, 归一化文本)`` 重新映射到新 Block id；章节先按
  ``(归一化标题, level)`` 匹配新 Section，匹配不到再退化为「证据重叠最多的 Section」；
* 图纠错提案：按 ``cite_key``（由 paper / version / 图号决定，重解析稳定）找新 Figure，
  重新插一条指向新 ``figure_id`` 的提案；
* 写作卡证据：block 按 ``(page, 归一化文本)``、figure 按 ``cite_key``（退化为
  ``(page, 归一化 caption)``）、table 按 ``(page, 归一化 caption/html)`` 重锚；
  summary 的 id 重解析稳定，但摘要本身可能因证据失配被删。映射不回来的证据剔除并
  置 ``claim.evidence_stale``——**卡片永不删除**（人工内容不丢），历史版本不动。

刻意接受的失效（宁可丢、不造假）：

* 旧证据块在新解析结果里找不到同样文本 → 从证据里剔除；若整条摘要的证据被剔空，且它
  是 agent 生成、没被人工锁定，则整条摘要连同历史一起删除（人工内容永不删）；
* 提案对应的图号在新解析结果里不存在 → 跳过该提案（cite_key 已无法对应）。

快照是纯 Python 对象（dict / list / datetime），不含 ORM 实例，可以安全 pickle。
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Block,
    Claim,
    Figure,
    FigureProposal,
    PaperVersion,
    Summary,
    SummaryRevision,
    TableBlock,
)
from .sections import Section, build_sections
from .writing import _clip

log = logging.getLogger("litbench.reparse")

# 整篇速览用保留键 ``__paper__``，不对应章节树里的任何一节：重解析时只迁证据，不动键。
RESERVED_SECTION_KEYS = frozenset({"__paper__"})

_SUMMARY_FIELDS = (
    "id",
    "section_key",
    "section_title",
    "level",
    "content",
    "key_points_json",
    "evidence_block_ids_json",
    "source",
    "provider",
    "model",
    "prompt_version",
    "status",
    "tokens_in",
    "tokens_out",
    "cost",
    "confidence",
    "auto_accepted",
    "human_locked",
    "created_at",
    "updated_at",
    "edited_by",
    "edited_at",
)

_REVISION_FIELDS = (
    "id",
    "summary_id",
    "content",
    "key_points_json",
    "evidence_block_ids_json",
    "confidence",
    "source",
    "provider",
    "model",
    "prompt_version",
    "status",
    "reason",
    "edited_by",
    "created_at",
)

_PROPOSAL_FIELDS = (
    "id",
    "paper_version_id",
    "kind",
    "proposed_json",
    "evidence_json",
    "confidence",
    "status",
    "provider",
    "model",
    "prompt_version",
    "mode",
    "tokens_in",
    "tokens_out",
    "cost",
    "latency_ms",
    "created_at",
    "decided_at",
    "decided_by",
    "decision_reason",
    # 回滚保险（F15）：before_json 丢了，重解析后「一键回滚」就永远报「没有 before 快照」
    "before_json",
    "auto_accepted",
    "rolled_back_at",
    "rolled_back_by",
)


def _norm(text: str | None) -> str:
    """证据匹配用的文本归一化：与需求约定一致，折叠所有空白。"""
    return " ".join(str(text or "").split())


def _plain(value):
    """把 ORM 上的 JSON 值拷贝成普通 list/dict，快照不与会话对象共享引用。"""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _dump(row, fields: tuple[str, ...]) -> dict:
    return {name: _plain(getattr(row, name)) for name in fields}


async def _block_texts(
    db: AsyncSession, version_id: str, block_ids: set[str]
) -> dict[str, list]:
    """旧 Block id -> [page, 归一化文本]。查不到（已悬空）的证据不出现在结果里。"""
    ids = sorted({str(i) for i in block_ids if i})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Block.id, Block.page, Block.text).where(
                Block.id.in_(ids), Block.paper_version_id == version_id
            )
        )
    ).all()
    return {str(bid): [int(page or 0), _norm(text)] for bid, page, text in rows}


async def snapshot_ai_products(db: AsyncSession, version_id: str) -> dict:
    """在 ``_clear_version_rows`` **之前**调：把该版本的 AI 产物快照成普通 dict。

    必须在旧 Block / Figure 行还在时调用——证据块的 (page, text) 和提案指向的图的
    ``cite_key`` 都只能从当前行读出来。
    """
    summaries = (
        await db.execute(
            select(Summary)
            .where(Summary.paper_version_id == version_id)
            .order_by(Summary.created_at)
        )
    ).scalars().all()
    revisions = (
        await db.execute(
            select(SummaryRevision)
            .join(Summary, SummaryRevision.summary_id == Summary.id)
            .where(Summary.paper_version_id == version_id)
            .order_by(SummaryRevision.created_at)
        )
    ).scalars().all()
    proposal_rows = (
        await db.execute(
            select(FigureProposal, Figure.cite_key)
            .outerjoin(Figure, FigureProposal.figure_id == Figure.id)
            .where(FigureProposal.paper_version_id == version_id)
            .order_by(FigureProposal.created_at)
        )
    ).all()

    evidence_ids: set[str] = set()
    for row in summaries:
        evidence_ids.update(str(i) for i in (row.evidence_block_ids_json or []))
    for row in revisions:
        evidence_ids.update(str(i) for i in (row.evidence_block_ids_json or []))

    # --- 写作卡：整篇 paper 的 claim 都查（含 paper_version_id 为 NULL 的旧卡），
    # 但只给「证据确实锚在本版本行上」的条目记身份，别的版本 / 已悬空的不动。
    version_row = await db.get(PaperVersion, version_id)
    claim_rows = (
        (
            await db.execute(select(Claim).where(Claim.paper_id == version_row.paper_id))
        ).scalars().all()
        if version_row is not None
        else []
    )
    claim_ids_by_type: dict[str, set[str]] = {
        "block": set(), "figure": set(), "table": set(), "summary": set()
    }
    for row in claim_rows:
        for item in row.evidence_json or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            eid = str(item.get("id") or "")
            if eid and kind in claim_ids_by_type:
                claim_ids_by_type[kind].add(eid)
    evidence_ids.update(claim_ids_by_type["block"])
    texts = await _block_texts(db, version_id, evidence_ids)

    figure_identity: dict[str, dict] = {}
    if claim_ids_by_type["figure"]:
        rows = (
            await db.execute(
                select(Figure.id, Figure.cite_key, Figure.page, Figure.caption).where(
                    Figure.id.in_(sorted(claim_ids_by_type["figure"])),
                    Figure.paper_version_id == version_id,
                )
            )
        ).all()
        for fid, cite_key, page, caption in rows:
            figure_identity[str(fid)] = {
                "type": "figure",
                "cite_key": str(cite_key) if cite_key else None,
                "page": int(page or 0),
                "caption": _norm(caption),
            }
    table_identity: dict[str, dict] = {}
    if claim_ids_by_type["table"]:
        rows = (
            await db.execute(
                select(
                    TableBlock.id, TableBlock.page, TableBlock.caption, TableBlock.html
                ).where(
                    TableBlock.id.in_(sorted(claim_ids_by_type["table"])),
                    TableBlock.paper_version_id == version_id,
                )
            )
        ).all()
        for tid, page, caption, html in rows:
            table_identity[str(tid)] = {
                "type": "table",
                "page": int(page or 0),
                "caption": _norm(caption),
                "html": _norm(html),
            }
    summary_identity: set[str] = set()
    if claim_ids_by_type["summary"]:
        rows = (
            await db.execute(
                select(Summary.id).where(
                    Summary.id.in_(sorted(claim_ids_by_type["summary"])),
                    Summary.paper_version_id == version_id,
                )
            )
        ).all()
        summary_identity = {str(sid) for (sid,) in rows}

    def _with_evidence(row) -> dict:
        data = _dump(row, _SUMMARY_FIELDS if isinstance(row, Summary) else _REVISION_FIELDS)
        data["evidence"] = {
            str(bid): texts[str(bid)]
            for bid in (row.evidence_block_ids_json or [])
            if str(bid) in texts
        }
        return data

    claims_snap: list[dict] = []
    for row in claim_rows:
        identity: dict[str, dict] = {}
        for item in row.evidence_json or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            eid = str(item.get("id") or "")
            if kind == "block" and eid in texts:
                identity[eid] = {"type": "block", "text": texts[eid]}
            elif kind == "figure" and eid in figure_identity:
                identity[eid] = figure_identity[eid]
            elif kind == "table" and eid in table_identity:
                identity[eid] = table_identity[eid]
            elif kind == "summary" and eid in summary_identity:
                identity[eid] = {"type": "summary"}
        if identity:
            claims_snap.append(
                {
                    "id": str(row.id),
                    "evidence": [
                        dict(item) if isinstance(item, dict) else item
                        for item in (row.evidence_json or [])
                    ],
                    "identity": identity,
                }
            )

    return {
        "version_id": version_id,
        "summaries": [_with_evidence(row) for row in summaries],
        "revisions": [_with_evidence(row) for row in revisions],
        "proposals": [
            {**_dump(proposal, _PROPOSAL_FIELDS), "figure_cite_key": cite_key}
            for proposal, cite_key in proposal_rows
        ],
        "claims": claims_snap,
    }


def _block_text_index(block_rows: list[Block]) -> dict[tuple[int, str], list[str]]:
    """``(page, 归一化文本) -> [block_id, ...]``；同文重复的块按出现顺序排队。"""
    index: dict[tuple[int, str], list[str]] = {}
    for row in block_rows:
        text = _norm(row.text)
        if not text:
            continue
        index.setdefault((int(row.page or 0), text), []).append(str(row.id))
    return index


def _remap_evidence(
    evidence: dict,
    old_ids: list | None,
    by_text: dict[tuple[int, str], list[str]],
) -> list[str]:
    """把旧证据 id 按 (page, 归一化文本) 映射到新 id：去重、保序、同文取第一个未占用的。"""
    used: set[str] = set()
    mapped: list[str] = []
    for raw in old_ids or []:
        old_id = str(raw)
        snapshot = evidence.get(old_id)
        if not snapshot:
            continue
        page, text = int(snapshot[0]), str(snapshot[1])
        if not text:
            continue
        candidates = by_text.get((page, text))
        if not candidates:
            continue
        chosen = next((cid for cid in candidates if cid not in used), candidates[0])
        if chosen in used:
            continue
        used.add(chosen)
        mapped.append(chosen)
    return mapped


def _section_index(
    sections: list[Section],
) -> tuple[dict[tuple[str, int], Section], dict[tuple[str, int], Section]]:
    """两种标题归一化：折叠空白（常规）与去掉所有空白（OCR 常给标题塞空格）。"""
    by_title: dict[tuple[str, int], Section] = {}
    by_compact: dict[tuple[str, int], Section] = {}
    for section in sections:
        title = _norm(section.title)
        level = int(section.level or 1)
        by_title.setdefault((title, level), section)
        by_compact.setdefault((title.replace(" ", ""), level), section)
    return by_title, by_compact


def _overlap(section: Section, evidence_ids: list[str]) -> int:
    return len(set(evidence_ids).intersection(section.block_ids))


def _match_section(
    snapshot: dict,
    sections: list[Section],
    by_title: dict[tuple[str, int], Section],
    by_compact: dict[tuple[str, int], Section],
    evidence_ids: list[str],
) -> tuple[Section | None, int]:
    """给旧摘要找新章节：先按 (标题, level)，再按证据重叠最多兜底。"""
    if str(snapshot.get("section_key") or "") in RESERVED_SECTION_KEYS:
        return None, 0
    title = _norm(snapshot.get("section_title"))
    level = int(snapshot.get("level") or 1)
    for index in (by_title, by_compact):
        section = index.get((title, level))
        if section is not None:
            return section, _overlap(section, evidence_ids)

    best: Section | None = None
    best_count = 0
    for section in sections:
        count = _overlap(section, evidence_ids)
        if count > best_count:
            best, best_count = section, count
    return best, best_count


def _unique_section_key(base: str | None, claimed: set[str]) -> str:
    """极端情况下两个旧摘要抢同一个新章节：造一个稳定唯一的键，保住内容不丢。"""
    prefix = (base or "orphan")[:56]
    suffix = 1
    while f"{prefix}~{suffix}" in claimed:
        suffix += 1
    return f"{prefix}~{suffix}"


async def reconcile_ai_products(
    db: AsyncSession,
    version: PaperVersion,
    snapshot: dict,
    block_rows: list[Block],
    figure_rows: list[Figure],
    table_rows: list[TableBlock] | None = None,
) -> dict:
    """在新 Block / Figure / TableBlock 行插入并 ``flush()`` 之后调：把 AI 产物重锚到新 id。

    调用约定：``block_rows`` / ``figure_rows`` / ``table_rows`` 必须是已经 flush 过、
    ``id`` 已生成的行；``version.parser`` / ``version.page_count`` 应已更新为新解析结果
    （章节切分才一致）。返回统计 dict，供 ``persist`` 打日志。
    """
    stats = {
        "summaries_remapped": 0,
        "summaries_dropped": 0,
        "revisions_remapped": 0,
        "proposals_remapped": 0,
        "proposals_unmatched": 0,
        "claims_remapped": 0,
        "claims_stale": 0,
    }
    if not snapshot:
        return stats
    # 首次解析 / 没有 AI 产物时不用算章节树（大书上 build_sections 不是零成本）。
    if not (
        snapshot.get("summaries")
        or snapshot.get("revisions")
        or snapshot.get("proposals")
        or snapshot.get("claims")
    ):
        return stats

    by_text = _block_text_index(block_rows)
    figure_pages = [
        int(row.page) for row in figure_rows if getattr(row, "deleted_at", None) is None
    ]
    sections = build_sections(
        block_rows,
        page_count=version.page_count,
        figure_pages=figure_pages,
        parser=version.parser,
    )
    by_title, by_compact = _section_index(sections)

    summaries = {
        str(row.id): row
        for row in (
            await db.execute(select(Summary).where(Summary.paper_version_id == version.id))
        ).scalars()
    }
    revisions = {
        str(row.id): row
        for row in (
            await db.execute(
                select(SummaryRevision)
                .join(Summary, SummaryRevision.summary_id == Summary.id)
                .where(Summary.paper_version_id == version.id)
            )
        ).scalars()
    }

    # --- 摘要：重映射证据 + 重新匹配章节 -------------------------------------
    planned: list[dict] = []
    dropped: set[str] = set()
    for snap in snapshot.get("summaries") or []:
        row = summaries.get(str(snap.get("id")))
        if row is None:  # 快照之后被别处删了：不复活
            continue
        new_ids = _remap_evidence(
            snap.get("evidence") or {}, snap.get("evidence_block_ids_json"), by_text
        )
        if not new_ids and snap.get("source") == "agent" and not snap.get("human_locked"):
            # agent 摘要的证据在新解析结果里完全找不到：留着只会是一页无凭据的文字。
            dropped.add(row.id)
            log.info("重解析：摘要 %s 的证据已全部失配，删除该 agent 摘要", row.id)
            continue
        target, overlap = _match_section(snap, sections, by_title, by_compact, new_ids)
        if (
            target is None
            and new_ids
            and str(snap.get("section_key") or "") not in RESERVED_SECTION_KEYS
        ):
            log.warning(
                "重解析：摘要 %s 找不到对应章节，保留原 section_key=%s",
                row.id,
                row.section_key,
            )
        planned.append({"row": row, "target": target, "overlap": overlap, "new_ids": new_ids})

    # 唯一约束 (paper_version_id, section_key)：先让「不改键」的占住原位，
    # 再把匹配到的按证据重叠降序分配，撞键就退回原键、再撞就造唯一键。
    claimed = {item["row"].section_key for item in planned if item["target"] is None}
    for item in sorted(
        (it for it in planned if it["target"] is not None),
        key=lambda it: (-it["overlap"], it["row"].id),
    ):
        key = item["target"].key
        if key not in claimed:
            item["final_key"] = key
            claimed.add(key)
        elif item["row"].section_key not in claimed:
            item["final_key"] = item["row"].section_key
            claimed.add(item["final_key"])
        else:
            item["final_key"] = _unique_section_key(item["row"].section_key, claimed)
            claimed.add(item["final_key"])
            log.warning(
                "重解析：摘要 %s 匹配到章节 %s 但键冲突，改用 %s",
                item["row"].id,
                key,
                item["final_key"],
            )

    for item in planned:
        row = item["row"]
        row.evidence_block_ids_json = item["new_ids"]
        if item["target"] is not None:
            row.section_key = item["final_key"]
            row.section_title = item["target"].title
            row.level = item["target"].level
        stats["summaries_remapped"] += 1

    # 被丢弃摘要的历史一起删（外键/唯一约束都别留尾巴），其余 revision 更新证据。
    dropped_revisions = [rid for rid, rev in revisions.items() if rev.summary_id in dropped]
    if dropped_revisions:
        await db.execute(
            delete(SummaryRevision).where(SummaryRevision.id.in_(dropped_revisions))
        )
    for snap in snapshot.get("revisions") or []:
        rev = revisions.get(str(snap.get("id")))
        if rev is None or rev.summary_id in dropped:
            continue
        rev.evidence_block_ids_json = _remap_evidence(
            snap.get("evidence") or {}, snap.get("evidence_block_ids_json"), by_text
        )
        stats["revisions_remapped"] += 1
    if dropped:
        await db.execute(delete(Summary).where(Summary.id.in_(sorted(dropped))))
        stats["summaries_dropped"] = len(dropped)

    by_cite: dict[str, Figure] = {}
    for row in figure_rows:
        if row.cite_key:
            by_cite.setdefault(str(row.cite_key), row)

    # --- 写作卡：证据按稳定身份重锚；映射不回来就剔除并标 stale，卡片永不删 -----
    claim_snaps = snapshot.get("claims") or []
    if claim_snaps:
        blocks_by_id = {str(b.id): b for b in block_rows}
        fig_by_caption: dict[tuple[int, str], Figure] = {}
        for row in figure_rows:
            if getattr(row, "deleted_at", None) is None and row.caption:
                fig_by_caption.setdefault((int(row.page or 0), _norm(row.caption)), row)
        tab_by_caption: dict[tuple[int, str], TableBlock] = {}
        tab_by_html: dict[tuple[int, str], TableBlock] = {}
        for row in table_rows or []:
            page = int(row.page or 0)
            if row.caption:
                tab_by_caption.setdefault((page, _norm(row.caption)), row)
            if row.html:
                tab_by_html.setdefault((page, _norm(row.html)), row)
        surviving_summaries = {
            sid: row for sid, row in summaries.items() if sid not in dropped
        }
        claims = {
            str(row.id): row
            for row in (
                await db.execute(select(Claim).where(Claim.paper_id == version.paper_id))
            ).scalars()
        }

        def _reanchor(ident: dict, old_id: str) -> dict | None:
            kind = ident.get("type")
            if kind == "block":
                mapped = _remap_evidence({old_id: ident["text"]}, [old_id], by_text)
                block = blocks_by_id.get(mapped[0]) if mapped else None
                if block is None:
                    return None
                return {
                    "type": "block",
                    "id": block.id,
                    "page": block.page,
                    "label": f"p{block.page} {block.type}",
                    "snippet": _clip(block.text, 120),
                }
            if kind == "figure":
                figure = by_cite.get(str(ident["cite_key"])) if ident.get("cite_key") else None
                if figure is None or getattr(figure, "deleted_at", None) is not None:
                    figure = (
                        fig_by_caption.get((ident["page"], ident["caption"]))
                        if ident.get("caption")
                        else None
                    )
                if figure is None:
                    return None
                return {
                    "type": "figure",
                    "id": figure.id,
                    "page": figure.page,
                    "label": figure.label_raw or figure.label_norm or "图（未编号）",
                    "snippet": _clip(figure.caption, 120),
                }
            if kind == "table":
                page = ident["page"]
                table = (
                    tab_by_caption.get((page, ident["caption"]))
                    if ident.get("caption")
                    else None
                )
                if table is None and ident.get("html"):
                    table = tab_by_html.get((page, ident["html"]))
                if table is None:
                    return None
                return {
                    "type": "table",
                    "id": table.id,
                    "page": table.page,
                    "label": table.label_raw or table.label_norm or "表（未编号）",
                    "snippet": _clip(table.caption, 120),
                }
            # summary：id 重解析稳定，但摘要本身可能因证据失配被删
            summary = surviving_summaries.get(old_id)
            if summary is None:
                return None
            return {
                "type": "summary",
                "id": summary.id,
                "page": None,
                "label": summary.section_title or summary.section_key,
                "snippet": _clip(summary.content, 120),
            }

        for snap in claim_snaps:
            claim = claims.get(str(snap.get("id")))
            if claim is None:  # 快照之后被别处删了：不复活
                continue
            identity = snap.get("identity") or {}
            new_evidence: list[dict] = []
            seen: set[tuple[str, str]] = set()
            lost = False
            for item in snap.get("evidence") or []:
                if not isinstance(item, dict):
                    continue
                old_id = str(item.get("id") or "")
                ident = identity.get(old_id)
                if ident is None:
                    # 证据属于别的版本 / 重解析前就已悬空：原样保留，不越权处理
                    new_evidence.append(dict(item))
                    continue
                entry = _reanchor(ident, old_id)
                if entry is None:
                    lost = True
                    continue
                key = (entry["type"], entry["id"])
                if key in seen:
                    continue
                seen.add(key)
                new_evidence.append(entry)
            claim.evidence_json = new_evidence
            claim.evidence_stale = lost
            stats["claims_remapped"] += 1
            if lost:
                stats["claims_stale"] += 1
                log.info("重解析：claim %s 有证据映射不回来，标记 evidence_stale", claim.id)

    # --- 图纠错提案：按 cite_key 找新 Figure，重新 insert ---------------------
    for snap in snapshot.get("proposals") or []:
        cite_key = snap.get("figure_cite_key")
        figure = by_cite.get(str(cite_key)) if cite_key else None
        if figure is None or getattr(figure, "id", None) is None:
            stats["proposals_unmatched"] += 1
            log.warning(
                "重解析：找不到 cite_key=%s 的图，跳过图纠错提案 %s",
                cite_key,
                snap.get("id"),
            )
            continue
        kwargs = {
            "figure_id": figure.id,
            "paper_version_id": snap.get("paper_version_id") or version.id,
            "kind": snap.get("kind") or "figure_review",
            "proposed_json": _plain(snap.get("proposed_json") or {}),
            "evidence_json": _plain(snap.get("evidence_json") or []),
            "confidence": float(snap.get("confidence") or 0.0),
            "status": snap.get("status") or "pending",
            "provider": snap.get("provider") or "",
            "model": snap.get("model") or "",
            "prompt_version": snap.get("prompt_version") or "",
            "mode": snap.get("mode") or "text",
            "tokens_in": int(snap.get("tokens_in") or 0),
            "tokens_out": int(snap.get("tokens_out") or 0),
            "cost": float(snap.get("cost") or 0.0),
            "latency_ms": int(snap.get("latency_ms") or 0),
            "decided_at": snap.get("decided_at"),
            "decided_by": snap.get("decided_by"),
            "decision_reason": snap.get("decision_reason"),
            "before_json": _plain(snap.get("before_json")),
            "auto_accepted": bool(snap.get("auto_accepted")),
            "rolled_back_at": snap.get("rolled_back_at"),
            "rolled_back_by": snap.get("rolled_back_by"),
        }
        if snap.get("created_at") is not None:
            kwargs["created_at"] = snap["created_at"]  # 保留原创建时间，审计别错位
        db.add(FigureProposal(**kwargs))
        stats["proposals_remapped"] += 1

    return stats


__all__ = ["RESERVED_SECTION_KEYS", "reconcile_ai_products", "snapshot_ai_products"]
