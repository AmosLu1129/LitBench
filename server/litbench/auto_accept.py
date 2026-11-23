# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 正式化：提案落地 + 自动接受器 + 回滚（docs/15 §4 M3 / §5 D3）。

三条硬规则（宁严勿滥）：

1. **白名单**：只有「图号修正 / 图注去噪」可自动写；裁框问题（crop_issue）永远留人工。
2. **三道闸**：置信 ≥ ``auto_accept_threshold``（默认 0.95）+ 证据非空 +
   建议字段已过 ``figure_review._validated`` 的候选集合/长度校验（落库时已校验）。
3. **人工改过的不动**：``label_source == "review"`` 或 ``caption_source != "parser"``
   的目标，自动接受器一律跳过；人工采纳时也会写快照，可回滚。

审计：所有正式写（人工采纳 / 自动采纳 / 拒绝 / 回滚）都写 ``audit_log``，
before/after 只含被修改字段（不记正文）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import write_audit
from .config import settings
from .labels import normalize_label, split_label
from .models import Figure, FigureProposal

# 裁框类判定：M3c 默认只提议不自动写。带这些值的 proposal 一律留人工。
CROP_ISSUES = ("cut_off", "too_large", "too_small", "not_figure")
# 自动接受器认可的 proposal 类型（图提取纠错 + M3c 框复核；bbox 另有更严的开关/闸门）
AUTO_ACCEPT_KINDS = ("figure_review", "figure_bbox")
AUTO_ACTOR = "auto-accepter"


class ProposalError(RuntimeError):
    """提案操作的可读错误（找不到 / 已处理 / 不可回滚）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def snapshot_figure(fig: Figure) -> dict[str, Any]:
    """采纳前快照：只含会被 proposal 修改的字段，可直接存 JSON。"""
    return {
        "label_raw": fig.label_raw,
        "label_norm": fig.label_norm,
        "label_num": fig.label_num,
        "sublabel": fig.sublabel,
        "label_source": fig.label_source,
        "label_confidence": fig.label_confidence,
        "caption": fig.caption,
        "caption_source": fig.caption_source,
        "review_status": fig.review_status,
        "review_note": fig.review_note,
        "reviewed_by": fig.reviewed_by,
        "reviewed_at": _iso(fig.reviewed_at),
        "bbox_override_json": fig.bbox_override_json,
        "image_path": fig.image_path,
    }


def _restore_figure(fig: Figure, before: dict[str, Any]) -> None:
    """按快照恢复（回滚用）；缺字段就跳过，向后兼容老快照。"""
    for key in (
        "label_raw",
        "label_norm",
        "label_num",
        "sublabel",
        "label_source",
        "label_confidence",
        "caption",
        "caption_source",
        "review_status",
        "review_note",
        "reviewed_by",
        "bbox_override_json",
        "image_path",
    ):
        if key in before:
            setattr(fig, key, before[key])
    if "reviewed_at" in before:
        value = before["reviewed_at"]
        fig.reviewed_at = datetime.fromisoformat(value) if value else None


def _proposed_label(proposed: dict[str, Any]) -> tuple[str | None, bool]:
    """返回 (建议图号原文, 是否明确要求「无编号」)。"""
    raw = proposed.get("suggested_label")
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), False
    if raw == "" or proposed.get("label_ok") is False:
        return "", True
    return None, False


def _auto_accept_reasons(fig: Figure, proposal: FigureProposal) -> list[str]:
    """返回阻止自动接受的原因列表（空列表 = 可以自动接受）。"""
    reasons: list[str] = []
    if not settings.auto_accept_enabled:
        reasons.append("自动接受已关闭")
    if proposal.kind not in AUTO_ACCEPT_KINDS:
        reasons.append(f"kind={proposal.kind} 不在白名单")
    proposed = proposal.proposed_json or {}
    if proposal.kind == "figure_bbox":
        # M3c 最保守：默认只提议；即便打开开关，也要「IoU 极低/极高 + caption 锚点 +
        # 置信 ≥ 0.95」三条同时满足（置信/证据在下面的通用规则里）。
        if not settings.bbox_review_auto_accept:
            reasons.append("bbox 默认只提议不自动写（BBOX_REVIEW_AUTO_ACCEPT=0）")
        else:
            if proposed.get("verdict") != "wrong":
                reasons.append("模型判定框没有问题")
            iou = float(proposed.get("iou") or 0.0)
            if not (iou <= 0.2 or iou >= 0.9):
                reasons.append(f"与现有框 IoU={iou:.2f}，不在极低/极高区间")
            anchor = proposed.get("caption_anchor") or {}
            if not anchor.get("ok"):
                reasons.append("caption 锚点不一致")
            if not proposed.get("bbox_points"):
                reasons.append("缺少可写入的 bbox")
    if proposal.status != "pending":
        reasons.append(f"proposal 状态是 {proposal.status}")
    confidence = float(proposal.confidence or 0.0)
    if confidence < settings.auto_accept_threshold:
        reasons.append(
            f"置信 {confidence:.2f} < 阈值 {settings.auto_accept_threshold:.2f}"
        )
    evidence = proposal.evidence_json or []
    if len(evidence) < settings.auto_accept_min_evidence:
        reasons.append(f"证据 {len(evidence)} 条，少于 {settings.auto_accept_min_evidence}")
    if not proposed.get("needs_change"):
        reasons.append("模型判定无需修改")
    if proposed.get("crop_issue") in CROP_ISSUES:
        reasons.append(f"裁框问题 {proposed.get('crop_issue')} 默认只提议不自动写")

    raw_label, no_label = _proposed_label(proposed)
    if raw_label is not None and raw_label != (fig.label_raw or ""):
        if fig.label_source == "review":
            reasons.append("图号已被人工改过（label_source=review）")
        if raw_label:
            if normalize_label(raw_label) is None:
                reasons.append(f"建议图号 {raw_label!r} 不能归一")
        elif not no_label:
            reasons.append("建议图号为空但没有明确「无编号」")
    elif raw_label is None and no_label:
        reasons.append("建议无编号但缺少可写入字段")

    cleaned = proposed.get("cleaned_caption")
    if isinstance(cleaned, str) and cleaned.strip():
        if fig.caption_source != "parser":
            reasons.append(f"图注已被改过（caption_source={fig.caption_source}）")
        if cleaned.strip() == (fig.caption or "").strip():
            reasons.append("清洗后图注与原图注相同")
    return reasons


def evaluate_auto_accept(fig: Figure, proposal: FigureProposal) -> tuple[bool, str, list[str]]:
    """自动接受判定：(是否接受, 可读说明, 阻止原因列表)。"""
    reasons = _auto_accept_reasons(fig, proposal)
    if reasons:
        return False, "；".join(reasons), reasons
    return True, "满足白名单 + 置信阈值 + 证据 + 未人工修改", []


def _apply_label(
    fig: Figure,
    proposed: dict[str, Any],
    *,
    actor: str,
    auto: bool,
    confidence: float | None = None,
) -> bool:
    raw, no_label = _proposed_label(proposed)
    if raw is None and not no_label:
        return False
    now = _now()
    before_raw = fig.label_raw
    # 自动接受时置信度如实落模型自评（≥阈值），人工采纳按惯例记 1.0。
    if auto and confidence is not None:
        label_confidence = round(float(confidence), 3)
    else:
        label_confidence = 1.0
    if no_label or raw == "":
        fig.label_raw = None
        fig.label_norm = None
        fig.label_num = None
        fig.sublabel = None
        fig.label_source = "agent" if auto else "review"
        fig.label_confidence = label_confidence
    else:
        assert raw is not None
        norm = normalize_label(raw)
        if norm is None:
            return False
        fig.label_raw = raw
        fig.label_norm = norm
        num, _ = split_label(norm)
        fig.label_num = num
        fig.sublabel = None
        fig.label_source = "agent" if auto else "review"
        fig.label_confidence = label_confidence
    fig.reviewed_by = actor
    fig.reviewed_at = now
    return fig.label_raw != before_raw


def _apply_caption(fig: Figure, proposed: dict[str, Any], *, auto: bool) -> bool:
    cleaned = proposed.get("cleaned_caption")
    if not isinstance(cleaned, str) or not cleaned.strip():
        return False
    text = cleaned.strip()
    if text == (fig.caption or "").strip():
        return False
    fig.caption = text
    # 无论是人工采纳还是自动采纳，文本都是「AI 清洗后的」；标记为 agent，
    # 只有真正的人工手改才写 human（自动接受器只看 caption_source == parser）。
    fig.caption_source = "agent"
    return True


def _apply_crop_issue(fig: Figure, proposed: dict[str, Any], *, actor: str) -> bool:
    issue = proposed.get("crop_issue")
    if issue not in CROP_ISSUES:
        return False
    fig.review_status = "wrong"
    reason = str(proposed.get("reason") or "")
    fig.review_note = f"AI 判断裁框问题：{issue}（{reason}）"[:2000]
    fig.reviewed_by = actor
    fig.reviewed_at = _now()
    return True


def _apply_bbox(fig: Figure, proposed: dict[str, Any], *, actor: str) -> bool:
    """M3c：采纳框建议 → 写 bbox_override_json（overlay 用）；缩略图由 router 重裁。"""
    points = proposed.get("bbox_points")
    if not isinstance(points, (list, tuple)) or len(points) != 4:
        return False
    try:
        box = [round(float(v), 1) for v in points]
    except (TypeError, ValueError):
        return False
    if fig.bbox_override_json == box:
        return False
    fig.bbox_override_json = box
    fig.review_status = "corrected"
    reason = str(proposed.get("reason") or "")
    fig.review_note = f"AI 框复核修正：{reason}"[:2000]
    fig.reviewed_by = actor
    fig.reviewed_at = _now()
    return True


def apply_proposal(
    session: AsyncSession,
    proposal: FigureProposal,
    fig: Figure,
    *,
    actor: str,
    auto: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """把 proposal 写进 figure 正式字段（不 commit）。

    返回 ``applied``：实际改了哪些字段（图号 / 图注 / 裁框标记）。
    采纳前快照写进 ``proposal.before_json``，供一键回滚。
    """
    before = snapshot_figure(fig)
    proposed = proposal.proposed_json or {}
    applied: dict[str, Any] = {}
    if _apply_label(
        fig,
        proposed,
        actor=actor,
        auto=auto,
        confidence=float(proposal.confidence or 0.0),
    ):
        applied["label_raw"] = fig.label_raw
        applied["label_norm"] = fig.label_norm
    if _apply_caption(fig, proposed, auto=auto):
        applied["caption"] = fig.caption
    if _apply_crop_issue(fig, proposed, actor=actor):
        applied["crop_issue"] = proposed.get("crop_issue")
    if _apply_bbox(fig, proposed, actor=actor):
        applied["bbox_override"] = list(fig.bbox_override_json or [])

    now = _now()
    if proposal.before_json is None:
        proposal.before_json = before
    proposal.status = "accepted"
    proposal.auto_accepted = auto
    proposal.decided_at = now
    proposal.decided_by = AUTO_ACTOR if auto else (actor or "human")
    if auto:
        proposal.decision_reason = "自动接受：白名单 + 置信阈值 + 证据 + 未人工修改"
    else:
        proposal.decision_reason = reason or "人工采纳"
    write_audit(
        session,
        actor=proposal.decided_by or actor,
        action="proposal.auto_accept" if auto else "proposal.accept",
        target_type="figure_proposal",
        target_id=proposal.id,
        before=before,
        after=snapshot_figure(fig),
        evidence=proposal.evidence_json or [],
        note=proposal.decision_reason,
    )
    return applied


def reject_proposal(
    session: AsyncSession,
    proposal: FigureProposal,
    *,
    actor: str,
    reason: str | None = None,
) -> None:
    now = _now()
    proposal.status = "rejected"
    proposal.decided_at = now
    proposal.decided_by = actor or "human"
    proposal.decision_reason = reason
    write_audit(
        session,
        actor=actor or "human",
        action="proposal.reject",
        target_type="figure_proposal",
        target_id=proposal.id,
        before=None,
        after={"status": "rejected", "reason": reason},
        evidence=proposal.evidence_json or [],
        note=reason,
    )


async def auto_accept_proposal(
    session: AsyncSession, proposal: FigureProposal, fig: Figure | None = None
) -> dict[str, Any] | None:
    """尝试自动接受一条 pending proposal；接受则 commit 并返回 applied，否则 None。"""
    if proposal.status != "pending":
        return None
    if fig is None:
        fig = await session.get(Figure, proposal.figure_id)
    if fig is None:
        return None
    eligible, _reason, _blocked = evaluate_auto_accept(fig, proposal)
    if not eligible:
        return None
    applied = apply_proposal(session, proposal, fig, actor=AUTO_ACTOR, auto=True)
    if not applied:
        # 理论不可能（evaluate 已保证至少有改动）；保险起见不写状态
        session.rollback()
        return None
    await session.commit()
    await session.refresh(proposal)
    await session.refresh(fig)
    return applied


async def sweep_auto_accept(
    session: AsyncSession,
    *,
    limit: int = 50,
    paper_id: str | None = None,
) -> dict[str, Any]:
    """扫一遍 pending proposal，把满足条件的自动接受；返回统计与命中列表。"""
    from .models import Paper, PaperVersion  # noqa: PLC0415 - 避免循环/只在查询时用

    stmt = (
        select(FigureProposal, Figure)
        .join(Figure, FigureProposal.figure_id == Figure.id)
        .join(PaperVersion, FigureProposal.paper_version_id == PaperVersion.id)
        .where(FigureProposal.status == "pending", Figure.deleted_at.is_(None))
        .order_by(FigureProposal.created_at.asc())
        .limit(limit)
    )
    if paper_id:
        stmt = stmt.join(Paper, PaperVersion.paper_id == Paper.id).where(Paper.id == paper_id)
    rows = (await session.execute(stmt)).all()
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for proposal, fig in rows:
        eligible, reason, blocked = evaluate_auto_accept(fig, proposal)
        if not eligible:
            skipped.append({"proposal_id": proposal.id, "figure_id": fig.id, "reason": reason})
            continue
        applied = apply_proposal(session, proposal, fig, actor=AUTO_ACTOR, auto=True)
        if not applied:
            session.rollback()
            skipped.append({"proposal_id": proposal.id, "reason": "无实际改动"})
            continue
        await session.commit()
        accepted.append(
            {
                "proposal_id": proposal.id,
                "figure_id": fig.id,
                "applied": applied,
                "confidence": proposal.confidence,
            }
        )
    return {
        "scanned": len(rows),
        "accepted": len(accepted),
        "skipped": len(skipped),
        "items": accepted,
        "skipped_items": skipped,
    }


async def rollback_proposal(
    session: AsyncSession,
    proposal_id: str,
    *,
    actor: str = "human",
) -> tuple[FigureProposal, Figure, dict[str, Any]]:
    """回滚一条已采纳的 proposal：按 before_json 恢复 figure 正式字段。"""
    row = (
        await session.execute(
            select(FigureProposal, Figure)
            .join(Figure, FigureProposal.figure_id == Figure.id)
            .where(FigureProposal.id == proposal_id)
        )
    ).first()
    if row is None:
        raise ProposalError("proposal not found")
    proposal, fig = row
    if proposal.status != "accepted":
        raise ProposalError(f"只有已采纳的建议能回滚（当前 {proposal.status}）")
    before = proposal.before_json
    if not isinstance(before, dict) or not before:
        raise ProposalError("这条建议没有 before 快照，无法回滚")
    current = snapshot_figure(fig)
    _restore_figure(fig, before)
    now = _now()
    proposal.status = "rolled_back"
    proposal.rolled_back_at = now
    proposal.rolled_back_by = actor or "human"
    write_audit(
        session,
        actor=actor or "human",
        action="proposal.rollback",
        target_type="figure_proposal",
        target_id=proposal.id,
        before=current,
        after=snapshot_figure(fig),
        evidence=proposal.evidence_json or [],
        note=f"回滚 {proposal.id}",
    )
    await session.commit()
    await session.refresh(proposal)
    await session.refresh(fig)
    return proposal, fig, snapshot_figure(fig)


__all__ = [
    "AUTO_ACCEPT_KINDS",
    "AUTO_ACTOR",
    "CROP_ISSUES",
    "ProposalError",
    "apply_proposal",
    "auto_accept_proposal",
    "evaluate_auto_accept",
    "reject_proposal",
    "rollback_proposal",
    "snapshot_figure",
    "sweep_auto_accept",
]
