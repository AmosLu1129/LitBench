# SPDX-License-Identifier: AGPL-3.0-or-later
"""证据校验器（M2 的硬约束）。

设计原则来自 ADR：「所有 AI 生成物必须带证据，像图表一样可回溯」。
所以摘要落库前必须过这一关：

1. ``evidence_block_ids`` 非空；
2. 每个 id 都真实存在于**本篇版本**、且属于**本节**的正文块（不是别的章节/别的文献）；
3. 去重、保序；
4. summary 正文非空、key_points 是短字符串数组；
5. 校验失败抛 ``EvidenceError``，由服务层直接拒绝落库（不是记一条脏数据再报警）。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_CONTENT_CHARS = 4000
MAX_KEY_POINTS = 8
MAX_EVIDENCE = 20


class EvidenceError(ValueError):
    """证据不合格：直接拒绝落库。"""


@dataclass(slots=True)
class ValidatedSummary:
    content: str
    key_points: list[str]
    evidence_ids: list[str]
    confidence: float


def normalize_evidence_ids(
    raw: object, *, allowed: set[str], min_ids: int = 1, max_ids: int = MAX_EVIDENCE
) -> list[str]:
    if raw is None:
        raise EvidenceError("evidence_block_ids 为空：AI 生成物必须绑定证据")
    if not isinstance(raw, (list, tuple)):
        raise EvidenceError("evidence_block_ids 必须是数组")
    ids: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise EvidenceError("evidence_block_ids 里有空值")
        block_id = value.strip()
        if block_id not in allowed:
            raise EvidenceError(f"证据块 {block_id} 不属于本章节正文（拒绝落库）")
        if block_id not in ids:
            ids.append(block_id)
    if len(ids) < min_ids:
        raise EvidenceError(f"证据不足：至少需要 {min_ids} 条，实际 {len(ids)} 条")
    if len(ids) > max_ids:
        raise EvidenceError(f"证据过多：最多 {max_ids} 条，实际 {len(ids)} 条")
    return ids


def validate_summary_payload(
    payload: dict | None, *, allowed: set[str], min_ids: int = 1
) -> ValidatedSummary:
    if not isinstance(payload, dict):
        raise EvidenceError("模型输出不是 JSON 对象")
    content = payload.get("summary")
    if not isinstance(content, str) or not content.strip():
        raise EvidenceError("摘要正文为空")
    content = content.strip()
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS]

    raw_points = payload.get("key_points") or []
    if not isinstance(raw_points, list):
        raise EvidenceError("key_points 必须是数组")
    key_points: list[str] = []
    for point in raw_points[:MAX_KEY_POINTS]:
        if isinstance(point, str) and point.strip():
            key_points.append(point.strip()[:200])
    if not key_points:
        raise EvidenceError("key_points 为空")

    evidence = normalize_evidence_ids(
        payload.get("evidence_block_ids"), allowed=allowed, min_ids=min_ids
    )

    confidence = payload.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    return ValidatedSummary(
        content=content,
        key_points=key_points,
        evidence_ids=evidence,
        confidence=confidence,
    )


__all__ = [
    "EvidenceError",
    "ValidatedSummary",
    "normalize_evidence_ids",
    "validate_summary_payload",
]
