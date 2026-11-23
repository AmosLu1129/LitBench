# SPDX-License-Identifier: AGPL-3.0-or-later
"""数据库会话。

引擎是**惰性**创建的：
- 没装 asyncpg（例如只跑纯逻辑测试）时，import 本模块不会炸；
- 没配置数据库时，`/health` 之类的端点照常可用。

这不是过度设计。上一版在 import 时就 ``create_async_engine``，
结果测试环境少一个驱动就整个应用起不来——这类耦合要在骨架期就断掉。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = settings.database_url
        kwargs: dict = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            # 本地开发用 SQLite 时要调三个 pragma：
            #  - temp_store=MEMORY：大事务会溢出到临时文件，而本机 TEMP 可能落在
            #    含中文的路径下（Windows + sqlite 的老毛病），表现为
            #    "unable to open database file"，且只在数据量大时才出现。
            #  - WAL：读写并发好一些。
            #  - synchronous=OFF：本地开发库不需要掉电安全，快很多。
            kwargs["connect_args"] = {"timeout": 30}
        _engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            from sqlalchemy import event

            @event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute("PRAGMA synchronous=OFF")
                # 外键默认是关的：不开的话所有 ondelete=CASCADE 在 dev 里都是摆设
                # （删 figure / version 会留下孤儿行）。
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
