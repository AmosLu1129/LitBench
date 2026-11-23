# SPDX-License-Identifier: AGPL-3.0-or-later
"""桌面版首次启动：在用户数据目录里建好 SQLite 表结构。

服务端本身不在启动时 create_all（生产走 Alembic）；桌面版没有迁移器，
用当前 ORM 模型建一份全新库即可。只建表，不写任何示例文献/配置。
"""
from __future__ import annotations

import asyncio

from litbench.db import dispose_engine, get_engine
from litbench.models import Base


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
