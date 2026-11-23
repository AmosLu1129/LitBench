# SPDX-License-Identifier: AGPL-3.0-or-later
"""阶段 3 校核入口的接口测试：低置信筛选、改图号、系列图拆/并。

口径：这些接口是给人工/agent 用的纠错入口，和 review 通道同一条产品线；
所有改动都要落库并能在检索里立刻体现。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base, Figure, Paper, PaperVersion


def make_figure(version_id: str, *, key: str, label: str | None, norm: str | None = None,
                conf: float | None = None, source: str | None = None,
                group: str | None = None, sub: str | None = None, page: int = 3) -> Figure:
    return Figure(
        paper_version_id=version_id,
        page=page,
        label_raw=label,
        label_norm=norm,
        label_confidence=conf,
        label_source=source,
        group_key=group,
        sublabel=sub,
        caption="caption",
        cite_key=key,
        bbox_json=[10.0, 20.0, 30.0, 40.0],
    )


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        paper = Paper(title="自动控制原理", status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id, file_sha256="c" * 64, file_path="/tmp/c.pdf", page_count=10
        )
        s.add(version)
        await s.flush()
        s.add_all(
            [
                # 正常图号
                make_figure(version.id, key="v1-fig4-2-1", label="图 4.2.1", norm="fig:4-2-1",
                            conf=0.95, source="text", page=3),
                # 无编号
                make_figure(version.id, key="v1-x1", label=None, conf=0.0, source="none", page=4),
                # 两套 OCR 冲突
                make_figure(version.id, key="v1-fig4-e-1", label="图 4.e.1", norm=None, conf=0.35,
                            source="ocr_cross_unresolved", page=5),
                # 系列子图里的非父成员
                make_figure(version.id, key="v1-fig4-3-1b", label="图 4.3.1", norm="fig:4-3-1",
                            conf=0.7, source="rule_inherit", group="p6-g1", sub="b", page=6),
            ]
        )
        await s.commit()

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_label_stats(client) -> None:
    r = await client.get("/api/figures/label-stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unlabeled"] == 1
    assert body["unresolved"] == 1
    assert body["review_total"] == 2  # 无号 + unresolved（继承的子图 conf 0.7 不算）


async def test_filter_by_label_state(client) -> None:
    r = await client.get("/api/figures", params={"label_state": "unlabeled"})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["label_raw"] is None

    r = await client.get("/api/figures", params={"label_state": "review"})
    assert r.json()["total"] == 2  # 无号 + unresolved；继承的子图 conf 0.7 不算
    r = await client.get("/api/figures", params={"label_state": "bogus"})
    assert r.status_code == 422


async def test_set_label_normalizes_and_marks_review(client) -> None:
    r = await client.get("/api/figures", params={"label_state": "unlabeled"})
    fid = r.json()["items"][0]["figure_id"]
    r = await client.post(f"/api/figures/{fid}/label", json={"label": "图 5-1", "reviewer": "agent"})
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["label_raw"] == "图 5-1"
    assert item["label_norm"] == "fig:5-1"
    assert item["label_source"] == "review"
    assert item["label_confidence"] == 1.0
    # 检索立刻生效
    r = await client.get("/api/figures", params={"q": "图 5-1"})
    assert r.json()["total"] == 1


async def test_set_label_keeps_unparsable_human_text(client) -> None:
    """人工确认的「图 4.e.1」保留原文，但不再写 label_norm。"""
    r = await client.get("/api/figures", params={"label_state": "unresolved"})
    fid = r.json()["items"][0]["figure_id"]
    r = await client.post(f"/api/figures/{fid}/label", json={"label": "图 4.E.1"})
    assert r.status_code == 200, r.text
    assert r.json()["label_raw"] == "图 4.E.1"
    assert r.json()["label_norm"] is None
    assert r.json()["label_source"] == "review"


async def test_set_label_rejects_non_label_text(client) -> None:
    r = await client.get("/api/figures")
    fid = r.json()["items"][0]["figure_id"]
    r = await client.post(f"/api/figures/{fid}/label", json={"label": "hello world"})
    assert r.status_code == 422


async def test_set_label_stores_letter_only_sublabel(client) -> None:
    """「图 4.2.1」的编号尾段（2-1）不是子图字母，不能写进 sublabel。"""
    r = await client.get("/api/figures", params={"label_state": "unlabeled"})
    fid = r.json()["items"][0]["figure_id"]

    r = await client.post(f"/api/figures/{fid}/label", json={"label": "图 4.2.1"})
    assert r.status_code == 200, r.text
    assert r.json()["label_norm"] == "fig:4-2-1"
    assert r.json()["sublabel"] is None

    r = await client.post(f"/api/figures/{fid}/label", json={"label": "图 4.2.1a"})
    assert r.status_code == 200, r.text
    assert r.json()["sublabel"] == "a"


async def test_split_group(client) -> None:
    r = await client.get("/api/figures", params={"q": "图 4.3.1"})
    fid = r.json()["items"][0]["figure_id"]
    r = await client.post(f"/api/figures/{fid}/group", json={"action": "split"})
    assert r.status_code == 200, r.text
    assert r.json()["group_key"] is None
    assert r.json()["sublabel"] is None


async def test_merge_group(client) -> None:
    r = await client.get("/api/figures")
    ids = [i["figure_id"] for i in r.json()["items"]]
    r = await client.post(
        f"/api/figures/{ids[0]}/group", json={"action": "merge", "member_ids": ids[1:3]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_key"] and body["group_key"].startswith("manual-")
    # 同组其它图也带上了同一个 key
    r2 = await client.get(f"/api/figures/{ids[1]}")
    assert r2.json()["group_key"] == body["group_key"]
