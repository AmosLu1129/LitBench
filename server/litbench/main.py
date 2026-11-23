# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI 入口。

    uvicorn litbench.main:app --reload
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import __version__
from .auth import resolve_session, system_user
from .config import settings
from .db import dispose_engine, get_session, get_sessionmaker
from .routers import (
    agent,
    auth,
    citations,
    dataset,
    figures,
    jobs,
    knowledge,
    metadata,
    ocr_review,
    papers,
    references,
    summaries,
    team,
    thesis,
    writing,
)
from .routers import (
    settings as settings_router,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


app = FastAPI(
    title="LitBench API",
    version=__version__,
    description="文献证据工作台 — 图表为一级对象，所有结论可回溯到 block。",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 认证开关打开后，除了登录/探针/文档，所有 /api/* 都要先登录。
# 认证默认关闭（LITBENCH_AUTH=0），单机开发/测试不受影响。
_PUBLIC_API_PATHS = frozenset(
    {
        "/api/auth/status",
        "/api/auth/setup",
        "/api/auth/login",
        "/api/auth/logout",
        # 容器/反代健康探针必须免登录：Docker HEALTHCHECK 打的就是它
        "/api/ready",
        "/api/openapi.json",
        "/api/docs",
    }
)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if not settings.auth_enabled:
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") or path in _PUBLIC_API_PATHS:
        return await call_next(request)

    # MCP 内部服务令牌（docs/17 §11.1）：认证打开后，MCP 用服务令牌以 system 身份调 API。
    # 用 compare_digest 做常量时间比较；令牌为空 = 不启用（外部请求一律走 cookie）。
    internal = (request.headers.get("X-LitBench-Internal-Token") or "").strip()
    if (
        settings.internal_api_token
        and internal
        and hmac.compare_digest(internal, settings.internal_api_token)
    ):
        request.state.user = system_user()
        request.state.internal_token = True
        return await call_next(request)

    request.state.internal_token = False
    token = request.cookies.get(settings.auth_cookie_name)
    async with get_sessionmaker()() as session:
        user = await resolve_session(session, token)
    request.state.user = user
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "未登录或登录已过期"})
    return await call_next(request)

app.include_router(auth.router, prefix="/api")
app.include_router(papers.router, prefix="/api")
app.include_router(metadata.router, prefix="/api")
app.include_router(ocr_review.router, prefix="/api")
app.include_router(figures.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(dataset.router, prefix="/api")
app.include_router(citations.router, prefix="/api")
app.include_router(knowledge.router, prefix="/api")
app.include_router(summaries.router, prefix="/api")
app.include_router(agent.router, prefix="/api")
app.include_router(writing.router, prefix="/api")
app.include_router(references.router, prefix="/api")
app.include_router(settings_router.router, prefix="/api")
app.include_router(team.router, prefix="/api")
app.include_router(thesis.router, prefix="/api")


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """存活探针。不碰数据库，所以数据库挂了这里仍然 200。"""
    return {"status": "ok", "version": __version__}


@app.get("/api/ready", tags=["meta"])
async def ready(session: AsyncSession = Depends(get_session)) -> dict:
    """就绪探针。会真的查一次库。"""
    await session.execute(text("select 1"))
    return {"status": "ready", "db": "ok"}
