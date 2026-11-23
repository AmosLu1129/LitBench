# SPDX-License-Identifier: AGPL-3.0-or-later
"""正式字段变更审计（M3）。

只记录**已发生的正式写操作**：采纳/拒绝/回滚 AI 建议、人工改图号、人工改图注。
不记录每次模型调用（那是 proposal 自身的 tokens/cost 字段），也不保存文献正文。

``before/after`` 只留被修改字段的小快照；大文本用长度/hash 代替（见 ``_safe_json``），
避免审计表被整篇摘要撑爆。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog

# 单个字符串字段进审计前超过这个长度就只留 hash + 长度（不记正文）
_MAX_AUDIT_TEXT = 400


def _safe_json(value: Any) -> Any:
    """把任意 JSON 可序列化值收敛成审计友好的形状（深拷贝 + 长文本降级）。"""
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) <= _MAX_AUDIT_TEXT:
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return {"_text_sha256": digest, "_chars": len(value)}
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, (bool, int, float)):
        return value
    return str(value)[:200]


def write_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    before: dict | None = None,
    after: dict | None = None,
    evidence: list | None = None,
    note: str | None = None,
) -> AuditLog:
    """追加一条审计（不 commit；同事务里由调用方 commit）。

    注意是同步函数：只 ``session.add``，不 flush —— 调用点都在「即将 commit」的路径上，
    不需要立刻拿 id；写成 async 反而容易被漏 await（真踩过）。
    """
    row = AuditLog(
        actor=(actor or "human")[:64],
        action=(action or "unknown")[:64],
        target_type=(target_type or "figure")[:32],
        target_id=str(target_id or "")[:32],
        before_json=_safe_json(before),
        after_json=_safe_json(after),
        evidence_json=_safe_json(evidence) if evidence else None,
        note=(note or None) and str(note)[:2000],
    )
    session.add(row)
    return row


def audit_to_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "actor": row.actor,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "before": row.before_json,
        "after": row.after_json,
        "evidence": row.evidence_json,
        "note": row.note,
        "created_at": row.created_at,
    }


async def list_audit(
    session: AsyncSession,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = (await session.execute(stmt)).scalars().all()
    return [audit_to_dict(row) for row in rows]


__all__ = ["audit_to_dict", "list_audit", "write_audit"]
