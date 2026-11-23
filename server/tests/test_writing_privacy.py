# SPDX-License-Identifier: AGPL-3.0-or-later
"""引用校验的可见性（自修批，2026-11-16）。

`/api/writing/check-citations` 早期实现查全库 paper，会把别人私有库的 cite_key /
标题解析出来。改成只对照当前用户可见文献；这里直接测函数层，避免动不动起登录流程。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.models import AppUser, Base, Paper
from litbench.writing import check_citations


async def _session(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'privacy.db').as_posix()}",
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


async def test_check_citations_respects_paper_visibility(tmp_path, monkeypatch) -> None:
    # 这个测试要验证「认证打开」时的可见性；全局 conftest 默认关认证
    monkeypatch.setattr(settings, "auth_enabled", True)
    engine, Session = await _session(tmp_path)
    alice = AppUser(id="a" * 32, username="alice", display_name="Alice", password_hash="x")
    bob = AppUser(id="b" * 32, username="bob", display_name="Bob", password_hash="x")
    bob_paper = Paper(
        id="c" * 32,
        title="Bob 的私有论文",
        title_norm="bob 的私有论文",
        cite_key="bob-secret-key",
        owner_user_id=bob.id,
        authors_json=[{"name": "Bob"}],
    )
    async with Session() as session:
        session.add_all([alice, bob, bob_paper])
        await session.commit()

    text = "别人库里的引用\\cite{bob-secret-key}。"
    async with Session() as session:
        as_alice = await check_citations(session, text, user=alice)
        as_bob = await check_citations(session, text, user=bob)
        as_none = await check_citations(session, text)
    assert as_alice["unknown"] == 1
    assert as_alice["citations"][0]["paper_id"] is None
    assert as_bob["unknown"] == 0
    assert as_bob["citations"][0]["paper_id"] == bob_paper.id
    # 认证关闭 / system 主体仍是全库（单机旧行为不变）
    assert as_none["unknown"] == 0

    await engine.dispose()
