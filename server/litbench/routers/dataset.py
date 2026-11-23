# SPDX-License-Identifier: AGPL-3.0-or-later
"""数据集出口：目前只有 golden set（M9.4 增加 bbox 子集）。

给谁用：①CI / 本地回放脚本；②agent（它复核完图之后可以自己把 golden 拉下来做回归）。
只读，不改数据。

认证打开时按文献可见性过滤：golden 条目带标题/DOI/图注/裁剪路径，不能把别人
私有库的标注当公共数据集导出（MCP 内部令牌走 system，仍是全库）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..golden import export_golden, export_golden_bboxes
from ..models import AppUser

router = APIRouter(prefix="/dataset", tags=["dataset"])


@router.get("/golden")
async def get_golden(
    include_pending: bool = Query(False, description="连未复核的图一起导出（做基线用）"),
    format: Literal["full", "bboxes"] = Query(
        "full", description="full=完整 golden；bboxes=只导图框标注集（M9.4）"
    ),
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """导出已复核的图 + 样本出处，用于解析器回归（只含当前用户可见的文献）。"""
    if format == "bboxes":
        return await export_golden_bboxes(
            session, include_pending=include_pending, user=user
        )
    return await export_golden(session, include_pending=include_pending, user=user)
