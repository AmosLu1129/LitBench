# SPDX-License-Identifier: AGPL-3.0-or-later
"""接口语义修复批 E（E1–E10）的回归测试。

每个 E 编号一节；断言尽量走 HTTP（用户真实路径），只为了造多版本/多集合
数据才直接碰 session。

    cd server && python -m pytest tests/test_api_semantics.py -q
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.paper_summary as paper_summary_mod
from litbench.config import settings
from litbench.db import get_session
from litbench.labels import normalize_label, normalize_title
from litbench.llm import LLMResult
from litbench.main import app
from litbench.models import (
    Base,
    Block,
    Collection,
    Figure,
    Job,
    Paper,
    PaperCollection,
    PaperVersion,
    TableBlock,
)


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    # 摘要测试固定走 mock/假客户端，不受本机 llm-config 或环境变量影响
    monkeypatch.setattr(settings, "llm_config_file", None)
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "llm_provider", "mock")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'semantics.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.session_factory = Session  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


def _sha() -> str:
    return uuid.uuid4().hex * 2


async def _paper(
    session,
    *,
    title: str,
    cite_key: str | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
    year: int | None = None,
    abstract: str | None = None,
    created_at: datetime | None = None,
) -> Paper:
    paper = Paper(
        title=title,
        title_norm=normalize_title(title),
        cite_key=cite_key,
        doi=doi,
        arxiv_id=arxiv_id,
        year=year,
        abstract=abstract,
        status="parsed",
        authors_json=[],
    )
    if created_at is not None:
        paper.created_at = created_at
    session.add(paper)
    await session.flush()
    return paper


async def _version(
    session,
    paper: Paper,
    *,
    created_at: datetime | None = None,
    parser: str = "pdfium",
    page_count: int = 3,
) -> PaperVersion:
    version = PaperVersion(
        paper_id=paper.id,
        kind="uploaded",
        file_sha256=_sha(),
        file_path=f"pdfs/{uuid.uuid4().hex}.pdf",
        page_count=page_count,
        parser=parser,
    )
    if created_at is not None:
        version.created_at = created_at
    session.add(version)
    await session.flush()
    return version


async def _figure(
    session,
    version: PaperVersion,
    *,
    page: int = 1,
    label: str | None = "图 1-1",
    caption: str = "",
    figure_type: str = "chart",
    review_status: str = "pending",
) -> Figure:
    norm = normalize_label(label) if label else None
    fig = Figure(
        paper_version_id=version.id,
        page=page,
        bbox_json=[10.0, 10.0, 120.0, 120.0],
        label_raw=label,
        label_norm=norm,
        label_num=_label_num(norm),
        caption=caption,
        figure_type=figure_type,
        cite_key=f"t-{uuid.uuid4().hex[:12]}",
        origin="parser",
        review_status=review_status,
    )
    session.add(fig)
    await session.flush()
    return fig


def _label_num(norm: str | None) -> int | None:
    if not norm or ":" not in norm:
        return None
    digits = "".join(ch for ch in norm.split(":", 1)[1] if ch.isdigit())
    return int(digits[:3]) if digits else None


async def _blocks(session, version: PaperVersion, texts: list[str]) -> list[Block]:
    blocks: list[Block] = []
    for idx, text in enumerate(texts):
        btype = "section" if idx % 2 == 0 else "text"
        block = Block(
            paper_version_id=version.id,
            page=1 + idx // 3,
            order_idx=idx,
            bbox_json=[10.0, 10.0, 200.0, 40.0],
            type=btype,
            text=text,
        )
        session.add(block)
        blocks.append(block)
    await session.flush()
    return blocks


# ---------------------------------------------------------------------------
# E1：GET /api/figures?paper= 解析 paper_id / cite_key
# ---------------------------------------------------------------------------
async def test_figures_paper_param_accepts_paper_id_citekey_and_title(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        paper = await _paper(s, title="Coal pyrolysis kinetics", cite_key="wang2019coal")
        version = await _version(s, paper)
        fig_a = await _figure(s, version, page=1, label="图 1-1", caption="TG curves at 10 K/min")
        fig_b = await _figure(s, version, page=2, label="图 1-2", caption="XRD patterns")
        other = await _paper(s, title="Other unrelated paper", cite_key="li2020other")
        other_version = await _version(s, other)
        await _figure(s, other_version, page=1, label="图 2-1", caption="unrelated")
        await s.commit()
        paper_id = paper.id
        expected = {fig_a.id, fig_b.id}

    # 32 位内部 id：以前被当作文本查 semantic ILIKE，恒为 0 条
    body = (await client.get("/api/figures", params={"paper": paper_id, "limit": 5})).json()
    assert body["mode"] == "provenance"
    assert body["total"] == 2
    assert {item["figure_id"] for item in body["items"]} == expected

    # 裸 cite_key：同样走确定性匹配
    body = (await client.get("/api/figures", params={"paper": "wang2019coal"})).json()
    assert {item["figure_id"] for item in body["items"]} == expected

    # 都不匹配 → 退回文本检索（标题片段语义保留）
    body = (await client.get("/api/figures", params={"paper": "Coal pyrolysis"})).json()
    assert body["mode"] == "semantic"
    assert {item["figure_id"] for item in body["items"]} == expected

    # 不存在的 paper id：0 条，而不是把全库返回
    body = (await client.get("/api/figures", params={"paper": "0" * 32})).json()
    assert body["items"] == []
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# E2：GET /api/papers?q= 同时搜 cite_key
# ---------------------------------------------------------------------------
async def test_paper_list_q_searches_citekey(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        coal = await _paper(s, title="Coal pyrolysis kinetics", cite_key="wang2019coal")
        nlp = await _paper(s, title="Deep learning for NLP", cite_key="li2020deep")
        await s.commit()
        coal_id, nlp_id = coal.id, nlp.id

    by_citekey = (await client.get("/api/papers", params={"q": "wang2019coal"})).json()
    assert [p["id"] for p in by_citekey] == [coal_id]

    by_title = (await client.get("/api/papers", params={"q": "pyrolysis"})).json()
    assert [p["id"] for p in by_title] == [coal_id]

    assert (await client.get("/api/papers", params={"q": "li2020deep"})).json()[0]["id"] == nlp_id

    # M9.1：q= 升级为 BM25（标题/摘要/作者/citekey/标签），旧子串命中继续保留
    natural = (
        await client.get(
            "/api/papers", params={"q": "coal pyrolysis experiment details"}
        )
    ).json()
    assert natural and natural[0]["id"] == coal_id

    partial = (await client.get("/api/papers", params={"q": "pyroly"})).json()
    assert [p["id"] for p in partial] == [coal_id]

    Session = client.session_factory
    async with Session() as s:
        zh = await _paper(s, title="相变传热研究：凝结与沸腾", cite_key="zhang2024phase")
        zh.abstract = "本文研究膜状凝结与核态沸腾的换热规律。"
        zh.authors_json = [{"lastName": "Zhang", "firstName": "Wei"}]
        zh.tags_json = ["传热", "综述"]
        await s.commit()
        zh_id = zh.id

    chinese = (await client.get("/api/papers", params={"q": "膜状凝结"})).json()
    assert chinese and chinese[0]["id"] == zh_id

    by_author = (await client.get("/api/papers", params={"q": "Zhang Wei"})).json()
    assert by_author and by_author[0]["id"] == zh_id

    by_tag = (await client.get("/api/papers", params={"q": "综述"})).json()
    assert by_tag and by_tag[0]["id"] == zh_id


# ---------------------------------------------------------------------------
# E3：collection join 去重（同一文献挂父 + 子集合）
# ---------------------------------------------------------------------------
async def test_paper_list_collection_join_is_distinct(client) -> None:
    now = datetime.now(timezone.utc)
    Session = client.session_factory
    async with Session() as s:
        parent = Collection(name="父分组", source="manual")
        s.add(parent)
        await s.flush()
        child = Collection(name="子分组", parent_id=parent.id, source="manual")
        s.add(child)
        await s.flush()

        dup = await _paper(s, title="Duplicated paper", cite_key="dup2020", created_at=now)
        only_child = await _paper(
            s, title="Only in child", cite_key="child2020", created_at=now - timedelta(minutes=1)
        )
        only_parent = await _paper(
            s, title="Only in parent", cite_key="parent2020", created_at=now - timedelta(minutes=2)
        )
        s.add_all(
            [
                PaperCollection(paper_id=dup.id, collection_id=parent.id),
                PaperCollection(paper_id=dup.id, collection_id=child.id),
                PaperCollection(paper_id=only_child.id, collection_id=child.id),
                PaperCollection(paper_id=only_parent.id, collection_id=parent.id),
            ]
        )
        await s.commit()
        parent_id = parent.id
        expected = {dup.id, only_child.id, only_parent.id}
        dup_id = dup.id

    items = (await client.get("/api/papers", params={"collection_id": parent_id, "limit": 10})).json()
    ids = [p["id"] for p in items]
    assert len(ids) == len(set(ids)) == 3, f"同一文献在父+子集合里出现了重复卡片：{ids}"
    assert set(ids) == expected
    assert ids.count(dup_id) == 1

    # limit 也按去重后的行数算，不因 join 重复行提前截断
    limited = (await client.get("/api/papers", params={"collection_id": parent_id, "limit": 3})).json()
    assert len(limited) == 3


# ---------------------------------------------------------------------------
# E4：figures/tables 与列表计数只反映最新版本
# ---------------------------------------------------------------------------
async def test_multi_version_endpoints_and_counts_use_latest(client) -> None:
    now = datetime.now(timezone.utc)
    Session = client.session_factory
    async with Session() as s:
        paper = await _paper(s, title="Versioned paper", cite_key="ver2020")
        v1 = await _version(s, paper, created_at=now - timedelta(hours=2), parser="pdfium")
        v2 = await _version(s, paper, created_at=now - timedelta(hours=1), parser="docling")

        f1 = await _figure(s, v1, page=1, label="图 1-1", caption="v1 图一")
        f2 = await _figure(s, v1, page=2, label="图 1-2", caption="v1 图二")
        f3 = await _figure(s, v2, page=1, label="图 2-1", caption="v2 图一")

        s.add_all(
            [
                TableBlock(paper_version_id=v1.id, page=1, label_raw="表 1-1", label_norm="tab:1"),
                TableBlock(paper_version_id=v2.id, page=1, label_raw="表 2-1", label_norm="tab:2"),
                TableBlock(paper_version_id=v2.id, page=2, label_raw="表 2-2", label_norm="tab:3"),
            ]
        )
        await _blocks(
            s, v1, ["v1 引言正文足够长的一段文字。", "v1 方法正文足够长的一段文字。"]
        )
        await _blocks(s, v2, ["v2 引言正文足够长的一段文字。"])
        await s.commit()
        paper_id, v2_id = paper.id, v2.id
        latest_fig = f3.id
        v1_fig_ids = {f1.id, f2.id}

    paper_out = (await client.get(f"/api/papers/{paper_id}")).json()
    assert paper_out["figure_count"] == 1, "列表计数跨版本累加了"
    assert paper_out["table_count"] == 2
    assert paper_out["has_version"] is True

    figs = (await client.get(f"/api/papers/{paper_id}/figures")).json()
    assert [f["figure_id"] for f in figs] == [latest_fig]
    assert {f["version_id"] for f in figs} == {v2_id}
    assert not ({f["figure_id"] for f in figs} & v1_fig_ids)

    tables = (await client.get(f"/api/papers/{paper_id}/tables")).json()
    assert len(tables) == 2
    assert {t["version_id"] for t in tables} == {v2_id}

    # /text 原本就只取最新版本；顺手确认 helper 替换后行为一致
    text = (await client.get(f"/api/papers/{paper_id}/text")).json()
    assert text["version_id"] == v2_id

    listed = {p["id"]: p for p in (await client.get("/api/papers")).json()}
    assert listed[paper_id]["figure_count"] == 1
    assert listed[paper_id]["table_count"] == 2


# ---------------------------------------------------------------------------
# E5：figures total 是真实总数，offset 翻页不重复
# ---------------------------------------------------------------------------
async def test_figures_total_is_real_count_and_offset_pages(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        parent = Collection(name="父集合", source="manual")
        s.add(parent)
        await s.flush()
        child = Collection(name="子集合", parent_id=parent.id, source="manual")
        s.add(child)
        await s.flush()

        paper = await _paper(s, title="Five figures", cite_key="five2020")
        version = await _version(s, paper)
        # 全部放同一页、都没有图号：只按 page/label 排序无法确定先后，
        # 必须靠 id 兜底，否则 offset 翻页会重复/漏项。
        figs = [
            await _figure(s, version, page=1, label=None, caption=f"caption {i}")
            for i in range(5)
        ]
        # 同一文献挂父+子集合：不 distinct 的话 total/items 会翻倍
        s.add_all(
            [
                PaperCollection(paper_id=paper.id, collection_id=parent.id),
                PaperCollection(paper_id=paper.id, collection_id=child.id),
            ]
        )
        await s.commit()
        paper_id, parent_id = paper.id, parent.id
        all_ids = {f.id for f in figs}

    first = (
        await client.get("/api/figures", params={"paper": paper_id, "limit": 3})
    ).json()
    assert first["total"] == 5, "total 是当前页条数，不是真实总数"
    assert len(first["items"]) == 3

    second = (
        await client.get(
            "/api/figures", params={"paper": paper_id, "limit": 3, "offset": 3}
        )
    ).json()
    assert second["total"] == 5
    assert len(second["items"]) == 2

    ids_first = [item["figure_id"] for item in first["items"]]
    ids_second = [item["figure_id"] for item in second["items"]]
    assert not (set(ids_first) & set(ids_second)), "offset 翻页出现了重复项"
    assert set(ids_first) | set(ids_second) == all_ids

    # collection 过滤（join 可能产生重复行）下 total 仍是去重后的真实值
    by_col = (
        await client.get(
            "/api/figures", params={"collection_id": parent_id, "limit": 2}
        )
    ).json()
    assert by_col["total"] == 5
    assert len(by_col["items"]) == 2


# ---------------------------------------------------------------------------
# E6：publish 锁住摘要；整篇速览无 force 不静默覆盖
# ---------------------------------------------------------------------------
class _PaperSummaryClient:
    """把预设 JSON 当模型输出；证明整篇速览只调一次模型。"""

    provider = "fake"
    model = "fake-model"

    def __init__(self, *, confidence: float = 0.95, content: str = "速览正文") -> None:
        self.confidence = confidence
        self.content = content
        self.calls = 0

    async def complete(
        self, messages, *, json_schema=None, meta=None, max_tokens=None, timeout_s=None
    ):
        self.calls += 1
        evidence_ids = list((meta or {}).get("evidence_ids") or [])[:2]
        payload = {
            "summary": self.content,
            "key_points": ["要点一", "要点二"],
            "evidence_block_ids": evidence_ids,
            "confidence": self.confidence,
        }
        return LLMResult(
            text=json.dumps(payload, ensure_ascii=False),
            parsed=payload,
            provider=self.provider,
            model=self.model,
            tokens_in=10,
            tokens_out=10,
        )


def _patch_paper_summary_llm(monkeypatch, fake: _PaperSummaryClient) -> None:
    monkeypatch.setattr(paper_summary_mod, "get_client", lambda provider=None: fake)
    monkeypatch.setattr(paper_summary_mod, "effective_config", lambda: ("ui", None))


async def _seed_summary_paper(client) -> dict:
    Session = client.session_factory
    async with Session() as s:
        paper = await _paper(
            s,
            title="测试文献：整篇速览",
            cite_key="sum2020",
            abstract="本文研究接口语义。",
        )
        version = await _version(s, paper, parser="docling")
        blocks = await _blocks(
            s,
            version,
            [
                "1. 引言：本文研究接口语义与分页契约，提出一套可验证的修复方案。",
                "2. 方法：逐条对齐文档与实现，并在 SQLite/Postgres 下验证行为。",
                "3. 结论：接口语义修复后前端不再需要自行补时区与去重。",
            ],
        )
        await s.commit()
        return {
            "paper_id": paper.id,
            "version_id": version.id,
            "block_ids": [b.id for b in blocks],
        }


async def test_publish_locks_summary_and_forces_cover_with_revision(client, monkeypatch) -> None:
    seed = await _seed_summary_paper(client)
    fake = _PaperSummaryClient(confidence=0.5)  # 低于阈值 → draft，publish 才锁
    _patch_paper_summary_llm(monkeypatch, fake)

    r = await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["status"] == "created"
    summary_id = created["summary_id"]

    draft = (await client.get(f"/api/papers/{seed['paper_id']}/summary")).json()
    assert draft["status"] == "draft"
    assert draft["human_locked"] is False

    r = await client.post(f"/api/summaries/{summary_id}/publish")
    assert r.status_code == 200, r.text
    published = r.json()
    assert published["status"] == "published"
    assert published["human_locked"] is True, "发布后必须置 human_locked，否则会被静默覆盖"
    assert published["auto_accepted"] is False
    assert published["edited_by"] == "human"

    detail = (await client.get(f"/api/papers/{seed['paper_id']}/summary")).json()
    assert detail["human_locked"] is True

    revisions_before = (
        await client.get(f"/api/summaries/{summary_id}/revisions")
    ).json()

    # 不带 force：可读错误（不是 500，也不是静默覆盖）
    blocked = await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})
    assert blocked.status_code == 422
    detail_text = blocked.json()["detail"]
    assert "整篇速览" in detail_text and "覆盖" in detail_text

    # force=True：允许覆盖，且旧内容留 revision
    fake.content = "覆盖后的速览正文。"
    forced = await client.post(
        f"/api/papers/{seed['paper_id']}/summary", json={"force": True}
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["status"] == "updated"
    assert (await client.get(f"/api/papers/{seed['paper_id']}/summary")).json()[
        "content"
    ] == "覆盖后的速览正文。"

    revisions_after = (
        await client.get(f"/api/summaries/{summary_id}/revisions")
    ).json()
    assert len(revisions_after) == len(revisions_before) + 1
    assert revisions_after[0]["reason"] == "regenerated"


# ---------------------------------------------------------------------------
# E7：对外时间统一带 UTC 时区
# ---------------------------------------------------------------------------
def _is_utc_iso(value: str) -> bool:
    return isinstance(value, str) and (
        value.endswith("+00:00") or value.endswith("Z")
    )


async def test_response_datetimes_are_utc(client, monkeypatch) -> None:
    seed = await _seed_summary_paper(client)
    fake = _PaperSummaryClient(confidence=0.95)
    _patch_paper_summary_llm(monkeypatch, fake)

    assert (await client.post(f"/api/papers/{seed['paper_id']}/summary", json={})).status_code == 200

    Session = client.session_factory
    async with Session() as s:
        job = Job(kind="parse", paper_version_id=seed["version_id"], payload_json={})
        s.add(job)
        await s.commit()
        job_id = job.id

    papers = (await client.get("/api/papers")).json()
    assert papers and _is_utc_iso(papers[0]["created_at"]), papers[0]["created_at"]

    detail = (await client.get(f"/api/papers/{seed['paper_id']}")).json()
    assert _is_utc_iso(detail["created_at"])

    job_out = (await client.get(f"/api/jobs/{job_id}")).json()
    assert _is_utc_iso(job_out["created_at"])

    summary = (await client.get(f"/api/papers/{seed['paper_id']}/summary")).json()
    assert _is_utc_iso(summary["created_at"])
    assert _is_utc_iso(summary["updated_at"])

    revision = (await client.get(f"/api/summaries/{summary['summary_id']}/revisions")).json()
    if revision:  # 生成不产生 revision；这里只在这版行为变化时兜底
        assert _is_utc_iso(revision[0]["created_at"])


# ---------------------------------------------------------------------------
# E8：LIKE 通配符转义
# ---------------------------------------------------------------------------
async def test_like_wildcards_are_escaped(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        pct = await _paper(s, title="Accuracy 95% on coal", cite_key="pct2020")
        plain = await _paper(s, title="Solar cells", cite_key="plain2020")
        pct_ver = await _version(s, pct)
        plain_ver = await _version(s, plain)
        pct_fig = await _figure(s, pct_ver, caption="yield 95% at 800K")
        await _figure(s, plain_ver, caption="plain caption without wildcard")
        await s.commit()
        pct_id, plain_id, pct_fig_id = pct.id, plain.id, pct_fig.id

    # `%%` 若被当通配符会匹配全库；转义后只匹配含字面 "%%" 的行 → 0 条
    all_papers = (await client.get("/api/papers", params={"q": "%%"})).json()
    assert all_papers == [], "标题检索里的 %% 变成了全匹配"

    all_figures = (await client.get("/api/figures", params={"q": "%%"})).json()
    assert all_figures["items"] == [] and all_figures["total"] == 0

    # 正常的百分号片段仍能命中
    hit = (await client.get("/api/figures", params={"q": "95%"})).json()
    assert [item["figure_id"] for item in hit["items"]] == [pct_fig_id]

    hit_papers = (await client.get("/api/papers", params={"q": "95%"})).json()
    assert [p["id"] for p in hit_papers] == [pct_id]
    assert plain_id not in {p["id"] for p in hit_papers}


# ---------------------------------------------------------------------------
# E9：枚举参数非法值 422，而不是静默 200/空结果
# ---------------------------------------------------------------------------
async def test_figures_enum_params_reject_unknown_values(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        paper = await _paper(s, title="Enum paper", cite_key="enum2020")
        version = await _version(s, paper)
        fig = await _figure(s, version, figure_type="chart", review_status="pending")
        await s.commit()
        fig_id = fig.id

    bad_review = await client.get("/api/figures", params={"review": "bogus"})
    assert bad_review.status_code == 422

    bad_type = await client.get("/api/figures", params={"figure_type": "bogus"})
    assert bad_type.status_code == 422

    ok_review = (await client.get("/api/figures", params={"review": "pending"})).json()
    assert [item["figure_id"] for item in ok_review["items"]] == [fig_id]

    ok_type = (await client.get("/api/figures", params={"figure_type": "chart"})).json()
    assert [item["figure_id"] for item in ok_type["items"]] == [fig_id]

    empty_type = (await client.get("/api/figures", params={"figure_type": "photo"})).json()
    assert empty_type["items"] == []


# ---------------------------------------------------------------------------
# E10：/papers/{id}/figures 有 response_model（OpenAPI 契约不是 items={}）
# ---------------------------------------------------------------------------
async def test_paper_figures_has_response_model(client) -> None:
    Session = client.session_factory
    async with Session() as s:
        paper = await _paper(s, title="Contract paper", cite_key="contract2020")
        version = await _version(s, paper)
        fig = await _figure(s, version, label="图 3-1", caption="contract")
        await s.commit()
        paper_id, fig_id = paper.id, fig.id

    openapi = (await client.get("/api/openapi.json")).json()
    schema = openapi["paths"]["/api/papers/{paper_id}/figures"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert schema.get("type") == "array"
    assert schema.get("items", {}).get("$ref", "").endswith("/FigureOut"), schema

    body = (await client.get(f"/api/papers/{paper_id}/figures")).json()
    assert [item["figure_id"] for item in body] == [fig_id]
    assert body[0]["cite_key"]
    assert body[0]["paper_id"] == paper_id
