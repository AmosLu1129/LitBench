# SPDX-License-Identifier: AGPL-3.0-or-later
"""图提取 AI 纠错（M3）：给一张已解析的图找「图号 / 图注 / 裁框」的问题，产出建议。

三条设计原则（docs/15 §4 M3）：

1. **只提议、不直接改**：建议落到 ``figure_proposal``，用户点「采纳」才写正式字段；
   拒绝也留记录（模型哪类判断不可靠是重要数据）。
2. **图号必须来自候选集合**：不让模型自由编编号——候选 = 同页 / 邻页 / 阅读顺序前后
   的图号，超出集合的建议直接丢弃（在 proposal 里注明原因）。
3. **图注只去噪不改写**：长度、数字/单位/符号都要保留；图片只是辅助（模型不支持视觉时
   退化成纯文本判断，``mode=text`` 如实记录）。
"""

from __future__ import annotations

import io
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auto_accept import (
    ProposalError,
    apply_proposal,
    auto_accept_proposal,
    reject_proposal,
)
from .config import settings
from .labels import LABEL_RE, normalize_label
from .llm import (
    LLMClient,
    LLMError,
    complete_with_budget_retry,
    effective_config,
    get_client,
    vision_message,
)
from .models import Figure, FigureProposal, Paper, PaperVersion
from .paths import resolve_data_path
from .prompts import load_prompt, render

PROMPT_NAME = "figure_review"
MAX_IMAGE_DIM = 900  # 长边上限：控制 token/费用（整页缩略图会更大）
CAPTION_MIN_RATIO = 0.55
CAPTION_MAX_RATIO = 1.4
MAX_NEIGHBOR_CAPTIONS = 6

_CROP_ISSUES = {"none", "cut_off", "too_large", "too_small", "not_figure", "unknown"}
_CAPTION_ISSUES = {"none", "ocr_noise", "truncated", "wrong_content"}


class FigureReviewError(RuntimeError):
    """图纠错的可读错误（图不存在 / 模型没配好 / 输出不可用）。"""


# ---------------------------------------------------------------------------
# 上下文：候选图号、邻近图注、图片
# ---------------------------------------------------------------------------
def _ordered_figures(rows: list[Figure]) -> list[Figure]:
    def sort_key(fig: Figure) -> tuple:
        bbox = fig.bbox_json or [0, 0, 0, 0]
        return (fig.page, bbox[1] if len(bbox) > 1 else 0, bbox[0] if bbox else 0)

    return sorted(rows, key=sort_key)


def _candidate_labels(current: Figure, ordered: list[Figure]) -> list[str]:
    """候选图号：同页 + 阅读顺序前后 + 邻图编号 ±1（全部去重，最多 12 个）。

    邻图 ±1 是给「当前图根本没有编号」准备的：真机实测里 OCR 把「图10-14」读成
    「图 HM4」，正确编号不在同页候选里，但邻图 10-13/10-15 的 ±1 正好能覆盖它。
    """
    try:
        index = ordered.index(current)
    except ValueError:  # pragma: no cover — 调用方保证 current 在 ordered 里
        index = 0
    near = ordered[max(0, index - 2) : index + 3]
    same_page = [f for f in ordered if f.page == current.page]
    raw_candidates = [
        f.label_raw
        for f in [*same_page, *near]
        if f.id != current.id and f.label_raw
    ]
    candidates: list[str] = []
    for raw in raw_candidates:
        if raw not in candidates:
            candidates.append(raw)

    def add_sequence(fig: Figure) -> None:
        if fig.label_num is None or not fig.label_raw:
            return
        prefix = re.sub(r"\d+$", "", fig.label_raw)
        for offset in (1, -1):
            value = fig.label_num + offset
            if value > 0:
                candidate = f"{prefix}{value}"
                if candidate not in candidates:
                    candidates.append(candidate)

    # 当前编号 ±1（已有编号时）+ 每张邻图编号 ±1（无编号时也能命中）
    add_sequence(current)
    for neighbor in near:
        if neighbor.id != current.id:
            add_sequence(neighbor)
    return candidates[:12]


def _neighbor_captions(current: Figure, ordered: list[Figure]) -> str:
    try:
        index = ordered.index(current)
    except ValueError:  # pragma: no cover
        index = 0
    neighbors = ordered[max(0, index - 2) : index + 3]
    lines: list[str] = []
    for fig in neighbors:
        if fig.id == current.id:
            continue
        label = fig.label_raw or "（无编号）"
        caption = re.sub(r"\s+", " ", fig.caption or "").strip()[:100]
        lines.append(f"- {label}（p{fig.page}）：{caption or '（无 caption）'}")
        if len(lines) >= MAX_NEIGHBOR_CAPTIONS:
            break
    return "\n".join(lines) or "（无邻近图注）"


def _image_payload(fig: Figure) -> tuple[bytes | None, str]:
    """读出裁剪图并压到可控尺寸。返回 (png 字节, 给 prompt 的说明)。"""
    if not fig.image_path:
        return None, "（这张图没有裁剪图：只做图号 / 图注判断，crop_issue 填 unknown）"
    try:
        path = resolve_data_path(settings.storage_dir, fig.image_path)
    except (PermissionError, ValueError):
        return None, "（裁剪图路径非法：只做文字判断，crop_issue 填 unknown）"
    if not path.is_file():
        return None, "（裁剪图不在磁盘：只做文字判断，crop_issue 填 unknown）"
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            if max(image.size) > MAX_IMAGE_DIM:
                image.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
            buf = io.BytesIO()
            image.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "（已附上该图的裁剪图，请结合图片判断裁框是否完整、内容是否为图）"
    except Exception:  # noqa: BLE001 — 图片读不出来不该让纠错失败
        return None, "（裁剪图读取失败：只做文字判断，crop_issue 填 unknown）"


# ---------------------------------------------------------------------------
# 输出校验
# ---------------------------------------------------------------------------
def _validated(
    payload: dict | None,
    *,
    current: Figure,
    candidates: list[str],
) -> tuple[dict, list[str]]:
    """把模型输出收敛成可落库的 proposal；越界建议丢弃并记原因。"""
    notes: list[str] = []
    if not isinstance(payload, dict):
        raise FigureReviewError("模型输出不是 JSON 对象")

    label_ok = bool(payload.get("label_ok", True))
    raw_label = payload.get("suggested_label")
    suggested_label: str | None = None
    suggested_norm: str | None = None
    if isinstance(raw_label, str):
        raw_label = raw_label.strip()
    else:
        raw_label = ""
    if raw_label:
        norm = normalize_label(raw_label)
        candidate_norms = {
            normalize_label(c) or "" for c in candidates
        }
        current_norm = current.label_norm or normalize_label(current.label_raw or "") or ""
        if norm is None and not LABEL_RE.search(raw_label):
            notes.append(f"建议图号「{raw_label}」不像图号，已忽略")
        elif norm and norm not in candidate_norms and norm != current_norm:
            notes.append(f"建议图号「{raw_label}」不在候选集合内，已忽略")
        else:
            suggested_label = raw_label
            suggested_norm = norm
            label_ok = False
    elif label_ok is False:
        # 模型明确说「当前图号不对」但没给替代：按「无编号」处理
        suggested_label = ""
        suggested_norm = None

    caption_issue = str(payload.get("caption_issue") or "none").strip().lower()
    if caption_issue not in _CAPTION_ISSUES:
        caption_issue = "none"
    cleaned_caption = payload.get("cleaned_caption")
    cleaned_caption = cleaned_caption.strip() if isinstance(cleaned_caption, str) else ""
    if caption_issue == "none":
        cleaned_caption = ""
    if cleaned_caption:
        original_len = len((current.caption or "").strip())
        if original_len < 6:
            notes.append("原图注太短，清洗结果按不可靠处理，已忽略")
            cleaned_caption = ""
        else:
            ratio = len(cleaned_caption) / original_len
            if ratio < CAPTION_MIN_RATIO or ratio > CAPTION_MAX_RATIO:
                notes.append(
                    f"清洗后图注长度变化过大（{original_len}→{len(cleaned_caption)} 字），已忽略"
                )
                cleaned_caption = ""
    if cleaned_caption and cleaned_caption == (current.caption or "").strip():
        cleaned_caption = ""

    crop_issue = str(payload.get("crop_issue") or "unknown").strip().lower()
    if crop_issue not in _CROP_ISSUES:
        crop_issue = "unknown"

    confidence = payload.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    def text(key: str, limit: int = 300) -> str:
        value = payload.get(key)
        return re.sub(r"\s+", " ", str(value)).strip()[:limit] if value else ""

    proposed = {
        "label_ok": label_ok,
        "suggested_label": suggested_label,
        "suggested_label_norm": suggested_norm,
        "caption_issue": caption_issue,
        "cleaned_caption": cleaned_caption,
        "crop_issue": crop_issue,
        "needs_change": bool(
            suggested_label is not None or cleaned_caption or crop_issue not in ("none", "unknown")
        ),
        "reason": text("reason"),
        "summary": text("summary"),
        "confidence": confidence,
        "current_label": current.label_raw,
        "current_caption": (current.caption or "")[:500],
        "validation_notes": notes,
    }
    return proposed, notes


# ---------------------------------------------------------------------------
# 生成 / 读取 / 采纳 / 拒绝
# ---------------------------------------------------------------------------
async def _load_figure(
    session: AsyncSession, figure_id: str
) -> tuple[Figure, PaperVersion, Paper, list[Figure]]:
    row = (
        await session.execute(
            select(Figure, PaperVersion, Paper)
            .join(PaperVersion, Figure.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(Figure.id == figure_id, Figure.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise FigureReviewError("figure not found")
    fig, version, paper = row
    siblings = list(
        (
            await session.execute(
                select(Figure).where(
                    Figure.paper_version_id == version.id, Figure.deleted_at.is_(None)
                )
            )
        ).scalars()
    )
    return fig, version, paper, _ordered_figures(siblings)


async def ai_review_figure(
    session: AsyncSession,
    figure_id: str,
    *,
    provider: str | None = None,
    client: LLMClient | None = None,
) -> dict:
    """对一张图跑一次 AI 纠错，产出一条 pending proposal（不改图）。"""
    fig, version, paper, siblings = await _load_figure(session, figure_id)
    candidates = _candidate_labels(fig, siblings)
    neighbors = _neighbor_captions(fig, siblings)
    image_bytes, image_note = _image_payload(fig)
    mode = "vision" if image_bytes else "text"

    prompt = load_prompt(PROMPT_NAME)
    prompt_text = render(
        prompt,
        paper_title=paper.title,
        page=fig.page,
        current_label=fig.label_raw or "（无编号）",
        current_caption=(fig.caption or "（无 caption）")[:600],
        candidate_labels="\n".join(f"- {c}" for c in candidates) or "（无候选：只能判定无编号）",
        neighbor_captions=neighbors,
        image_note=image_note,
    )
    messages = vision_message(prompt_text, image_bytes) if image_bytes else [
        {"role": "user", "content": prompt_text}
    ]
    if client is None:
        source, _cfg = effective_config()
        if source == "mock":
            raise FigureReviewError(
                "还没有配置模型：请先在右上角「设置」里填 base_url / model / API key"
            )
    try:
        llm = client or get_client(provider)
    except LLMError as exc:
        raise FigureReviewError(str(exc)) from exc
    try:
        result = await complete_with_budget_retry(
            llm,
            messages,
            json_schema={"type": "object"},
            meta={
                "task": "figure_review",
                "figure_id": fig.id,
                "has_image": bool(image_bytes),
                "candidates": candidates,
            },
            max_tokens=settings.figure_review_max_tokens,
        )
    except LLMError as exc:
        # 视觉调用偶发失败（推理预算/读超时/网关不支持图片）时降级为纯文字判断：
        # 图号和图注仍能纠错，裁框判断如实标 unknown，不让一条建议都产不出来。
        recoverable = any(
            token in str(exc).lower()
            for token in ("推理", "timeout", "超时", "不支持图片", "image", "http 5", "接关闭")
        )
        if not (image_bytes and recoverable):
            raise FigureReviewError(str(exc)) from exc
        fallback_text = (
            f"{prompt_text}\n\n（图片调用失败，已降级为纯文字判断：crop_issue 一律填 unknown，"
            "只判断图号和图注）"
        )
        try:
            result = await complete_with_budget_retry(
                llm,
                [{"role": "user", "content": fallback_text}],
                json_schema={"type": "object"},
                meta={
                    "task": "figure_review",
                    "figure_id": fig.id,
                    "has_image": False,
                    "vision_fallback": True,
                    "candidates": candidates,
                },
                max_tokens=settings.figure_review_max_tokens,
            )
        except LLMError as exc2:
            raise FigureReviewError(str(exc2)) from exc2
        mode = "text"
    proposed, _notes = _validated(result.parsed, current=fig, candidates=candidates)

    # 同一张图的旧 pending 建议作废：UI 只把最新一条当「当前建议」
    old = (
        await session.execute(
            select(FigureProposal).where(
                FigureProposal.figure_id == fig.id, FigureProposal.status == "pending"
            )
        )
    ).scalars().all()
    for row in old:
        row.status = "superseded"

    proposal = FigureProposal(
        figure_id=fig.id,
        paper_version_id=version.id,
        kind="figure_review",
        proposed_json=proposed,
        evidence_json=[
            {"type": "candidate_label", "value": c} for c in candidates
        ]
        + [{"type": "neighbor_caption", "value": neighbors}]
        + [{"type": "image", "value": mode}],
        confidence=float(proposed.get("confidence") or 0.0),
        # 检查后认为没问题 → no_change（不进待审队列，UI 只显示「AI 检查通过」）
        status="pending" if proposed.get("needs_change") else "no_change",
        provider=result.provider,
        model=result.model,
        prompt_version=prompt.version,
        mode=mode,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost=result.cost,
        latency_ms=result.latency_ms,
    )
    # confidence 单独算，避免把 0/1 混淆
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)

    # M3 正式化：proposal 先落 pending，随后立刻过一遍自动接受器（白名单/阈值/证据/
    # 未人工修改）。通过 → 当场写正式字段 + 审计；不通过 → 留在待审队列。
    if proposal.status == "pending":
        await auto_accept_proposal(session, proposal, fig)
        await session.refresh(proposal)
        await session.refresh(fig)
    return proposal_to_dict(proposal, fig=fig, paper=paper)


def proposal_to_dict(proposal: FigureProposal, *, fig: Figure | None = None, paper: Paper | None = None) -> dict:
    data = {
        "proposal_id": proposal.id,
        "figure_id": proposal.figure_id,
        "paper_version_id": proposal.paper_version_id,
        "kind": proposal.kind,
        "proposed": proposal.proposed_json or {},
        "evidence": proposal.evidence_json or [],
        "confidence": proposal.confidence,
        "status": proposal.status,
        "provider": proposal.provider,
        "model": proposal.model,
        "prompt_version": proposal.prompt_version,
        "mode": proposal.mode,
        "tokens_in": proposal.tokens_in,
        "tokens_out": proposal.tokens_out,
        "cost": proposal.cost,
        "latency_ms": proposal.latency_ms,
        "created_at": proposal.created_at,
        "decided_at": proposal.decided_at,
        "decided_by": proposal.decided_by,
        "decision_reason": proposal.decision_reason,
        "before": proposal.before_json,
        "auto_accepted": bool(proposal.auto_accepted),
        "rolled_back_at": proposal.rolled_back_at,
        "rolled_back_by": proposal.rolled_back_by,
    }
    if fig is not None:
        data["figure"] = {
            "figure_id": fig.id,
            "label_raw": fig.label_raw,
            "label_norm": fig.label_norm,
            "caption": fig.caption,
            "page": fig.page,
            "image_url": f"/api/figures/{fig.id}/image" if fig.image_path else None,
        }
    if paper is not None:
        data["paper"] = {"paper_id": paper.id, "title": paper.title}
    return data


async def list_proposals(
    session: AsyncSession,
    *,
    status: str = "pending",
    paper_id: str | None = None,
    figure_id: str | None = None,
    paper_scope=None,
    limit: int = 50,
) -> list[dict]:
    stmt = (
        select(FigureProposal, Figure, Paper)
        .join(Figure, FigureProposal.figure_id == Figure.id)
        .join(PaperVersion, FigureProposal.paper_version_id == PaperVersion.id)
        .join(Paper, PaperVersion.paper_id == Paper.id)
        .where(Figure.deleted_at.is_(None))
        .order_by(FigureProposal.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(FigureProposal.status == status)
    if paper_id:
        stmt = stmt.where(Paper.id == paper_id)
    if figure_id:
        stmt = stmt.where(FigureProposal.figure_id == figure_id)
    if paper_scope is not None:
        # 团队共享（M7）：提案列表只出当前用户看得见的文献
        stmt = stmt.where(paper_scope)
    rows = (await session.execute(stmt)).all()
    return [proposal_to_dict(prop, fig=fig, paper=paper) for prop, fig, paper in rows]


async def decide_proposal(
    session: AsyncSession,
    proposal_id: str,
    *,
    action: str,
    actor: str = "human",
    reason: str | None = None,
) -> tuple[dict, dict]:
    """采纳/拒绝一条建议。采纳时把建议写进 figure 正式字段 + 留快照 + 写审计。

    返回 ``(proposal_dict, applied_changes)``；applied_changes 说明实际改了什么
    （图号 / 图注 / 裁框标记），前端据此提示。
    """
    if action not in ("accept", "reject"):
        raise FigureReviewError("action 只支持 accept / reject")
    row = (
        await session.execute(
            select(FigureProposal, Figure, PaperVersion, Paper)
            .join(Figure, FigureProposal.figure_id == Figure.id)
            .join(PaperVersion, FigureProposal.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(FigureProposal.id == proposal_id)
        )
    ).first()
    if row is None:
        raise FigureReviewError("proposal not found")
    proposal, fig, _version, paper = row
    if proposal.status != "pending":
        raise FigureReviewError(f"这条建议已经处理过（{proposal.status}）")

    if action == "accept":
        try:
            applied = apply_proposal(
                session, proposal, fig, actor=actor, auto=False, reason=reason
            )
        except ProposalError as exc:  # pragma: no cover - 仅是防御性包装
            raise FigureReviewError(str(exc)) from exc
        await session.commit()
    else:
        reject_proposal(session, proposal, actor=actor, reason=reason)
        await session.commit()
        applied = {}
    await session.refresh(proposal)
    await session.refresh(fig)
    return proposal_to_dict(proposal, fig=fig, paper=paper), applied


async def get_proposal(session: AsyncSession, proposal_id: str) -> dict:
    row = (
        await session.execute(
            select(FigureProposal, Figure, Paper)
            .join(Figure, FigureProposal.figure_id == Figure.id)
            .join(PaperVersion, FigureProposal.paper_version_id == PaperVersion.id)
            .join(Paper, PaperVersion.paper_id == Paper.id)
            .where(FigureProposal.id == proposal_id)
        )
    ).first()
    if row is None:
        raise FigureReviewError("proposal not found")
    proposal, fig, paper = row
    return proposal_to_dict(proposal, fig=fig, paper=paper)


__all__ = [
    "FigureReviewError",
    "PROMPT_NAME",
    "ai_review_figure",
    "decide_proposal",
    "get_proposal",
    "list_proposals",
    "proposal_to_dict",
]
