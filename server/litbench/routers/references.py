# SPDX-License-Identifier: AGPL-3.0-or-later
"""参考文献条目 API（2026-11-13；2026-11-16 M8 增加 CSL 引擎）。

三个端点：

* ``GET /references/styles``：格式清单 + 用示例文献生成的样例；同时报告 CSL 引擎是否可用；
* ``POST /references/format``：给一组 paper_id + 格式，返回逐条 references + 合并文本。
  ``engine=template`` 走 13 种手写模板（默认，无外部依赖）；
  ``engine=csl`` 走 pandoc --citeproc + 官方 CSL 样式（GB/T 7714-2015 / APA 7 / IEEE）；
* ``POST /references/csl-json``：导出 CSL JSON（pandoc / Zotero / 后续 docx 导出的通用输入）。

只读派生，不落库；生成依据是 paper 的著录字段（缺字段会在 `warnings` 里点名）。
CSL 渲染失败（没装 pandoc / 超时 / pandoc 报错）时模板引擎不受影响。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ..auth import current_user
from ..csl import (
    CslError,
    CslUnavailable,
    csl_engine_status,
    csl_style_keys,
    export_csl_json,
    render_bibliography,
)
from ..db import get_session
from ..models import AppUser, Paper
from ..references import (
    ReferenceError,
    format_references,
    list_styles,
)
from ..schemas import (
    CslJsonIn,
    CslJsonOut,
    ReferenceFormatIn,
    ReferenceFormatOut,
    ReferenceStylesOut,
)
from ..team import ensure_paper_access

router = APIRouter(tags=["references"])

# 格式清单的样例文献：字段全、英文期刊，能让每种格式都展示完整形态
_SAMPLE_PAPER = SimpleNamespace(
    id="sample",
    title="Slip-patterned microchannels for enhanced heat transport",
    authors_json=[
        {"firstName": "Wei", "lastName": "Zhang"},
        {"firstName": "Ming", "lastName": "Li"},
    ],
    year=2026,
    venue="International Journal of Heat and Mass Transfer",
    volume="231",
    issue="3",
    pages="112–128",
    doi="10.1016/j.ijheatmasstransfer.2026.112128",
    arxiv_id=None,
    cite_key="doi:10.1016/j.ijheatmasstransfer.2026.112128",
)


async def _load_papers(
    session: AsyncSession,
    user: AppUser | None,
    paper_ids: list[str],
) -> list[Paper]:
    """去重、校验可见性、按传入顺序取 Paper；缺文献 404、空列表 422。"""
    ids: list[str] = []
    for raw in paper_ids:
        value = str(raw).strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        raise HTTPException(status_code=422, detail="至少要选一篇文献")
    # 只能给自己看得见的文献生成条目（共享文献可以，别人的私有文献不行）
    for paper_id in ids:
        await ensure_paper_access(session, user, paper_id)
    rows = {
        paper.id: paper
        for paper in (await session.execute(select(Paper).where(Paper.id.in_(ids)))).scalars()
    }
    missing = [paper_id for paper_id in ids if paper_id not in rows]
    if missing:
        raise HTTPException(status_code=404, detail=f"文献不存在：{', '.join(missing)}")
    return [rows[paper_id] for paper_id in ids]


@router.get("/references/styles", response_model=ReferenceStylesOut)
async def get_reference_styles() -> ReferenceStylesOut:
    """主流期刊/学位论文格式预设（含样例与适用说明），并报告 CSL 引擎状态。"""
    styles = list_styles(_SAMPLE_PAPER)
    csl_keys = set(csl_style_keys())
    for row in styles:
        row["csl_available"] = row["key"] in csl_keys
    engine_available, csl_note = csl_engine_status()
    return ReferenceStylesOut(
        styles=styles,
        note=(
            "只按库里的著录字段生成，缺卷期页会省略并在条目的 warnings 里提示；"
            "BibTeX / RIS 输出多行记录，适合直接导入 LaTeX / EndNote / Zotero。"
            "CSL 引擎用官方 CSL 样式（GB/T 7714-2015 / APA 7 / IEEE），与 Zotero 输出更接近。"
        ),
        csl_engine_available=engine_available,
        csl_note=csl_note,
    )


@router.post("/references/format", response_model=ReferenceFormatOut)
async def format_reference_entries(
    body: ReferenceFormatIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReferenceFormatOut:
    """按格式预设生成 references 条目（保持传入顺序；重复 id 去重）。"""
    papers = await _load_papers(session, user, body.paper_ids)
    if body.engine == "csl":
        if body.style not in set(csl_style_keys()):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"格式 {body.style!r} 没有 CSL 样式；"
                    f"可用：{', '.join(csl_style_keys())}"
                ),
            )
        try:
            # pandoc 是子进程调用；放到线程池，别卡住事件循环
            payload = await run_in_threadpool(render_bibliography, papers, body.style)
        except CslUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CslError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return ReferenceFormatOut(**payload)
    try:
        payload = format_references(
            papers,
            body.style,
            numbered=body.numbered,
            template=body.template,
        )
    except ReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ReferenceFormatOut(**payload)


@router.post("/references/csl-json", response_model=CslJsonOut)
async def export_reference_csl_json(
    body: CslJsonIn,
    user: AppUser | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CslJsonOut:
    """导出所选文献的 CSL JSON（pandoc / Zotero 通用输入；不落库）。"""
    papers = await _load_papers(session, user, body.paper_ids)
    items = export_csl_json(papers)
    return CslJsonOut(count=len(items), items=items)
