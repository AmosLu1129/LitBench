# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden set 的测试：判分规则、导出接口、回放。

最该被保护的性质：**同一个解析器重放自己的 golden，recall 必须是 100%**。
这条不成立的话，后面所有「改了解析器 → 指标涨了」的结论都不可信。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from litbench.adapters import ParsedDocument, ParsedFigure
from litbench.config import settings
from litbench.db import get_session
from litbench.golden import (
    BBOX_SCHEMA,
    SCHEMA,
    bbox_iou,
    expects_box,
    replay,
    score_item,
    summarize,
)
from litbench.main import app
from litbench.models import Base, Figure, Paper, PaperVersion

BOX = [10.0, 20.0, 110.0, 70.0]


# ----------------------------------------------------------------------
# IoU 与判分
# ----------------------------------------------------------------------
def test_iou_identity_and_disjoint() -> None:
    assert bbox_iou(BOX, BOX) == 1.0
    assert bbox_iou(BOX, [200.0, 200.0, 300.0, 300.0]) == 0.0
    assert bbox_iou(BOX, None) == 0.0
    assert bbox_iou(BOX, [1.0, 2.0]) == 0.0


def test_iou_partial_overlap() -> None:
    # 右移 50：交集 50x50=2500，并集 5000+5000-2500=7500
    assert abs(bbox_iou(BOX, [60.0, 20.0, 160.0, 70.0]) - 2500 / 7500) < 1e-6


def make_doc(*, label: str | None = "Fig. 1", bbox: list[float] | None = None) -> ParsedDocument:
    figures = (
        [ParsedFigure(page=1, bbox=list(bbox or BOX), label_raw=label)] if label is not None else []
    )
    return ParsedDocument(
        page_count=1,
        engine="fake",
        engine_version="0",
        blocks=[],
        figures=figures,
        tables=[],
        text_layer=True,
    )


def item(**overrides) -> dict:
    base = {
        "item_id": "it1",
        "cite_key": "abc123-v1-fig1",
        "paper_title": "Paper",
        "label_norm": "fig:1",
        "review_status": "ok",
        "bbox": list(BOX),
    }
    base.update(overrides)
    return base


def test_ok_item_passes_when_box_matches() -> None:
    r = score_item(make_doc(), item())
    assert r["verdict"] == "pass"
    assert r["iou"] == 1.0


def test_ok_item_regresses_when_box_moved() -> None:
    r = score_item(make_doc(bbox=[300.0, 300.0, 400.0, 350.0]), item())
    assert r["verdict"] == "regression"


def test_ok_item_regresses_when_label_lost() -> None:
    r = score_item(make_doc(label=None), item())
    assert r["verdict"] == "no_box"
    assert r["anchored"] is False


def test_wrong_item_still_wrong_when_same_box() -> None:
    r = score_item(make_doc(), item(review_status="wrong"))
    assert r["verdict"] == "still_wrong"


def test_wrong_item_fixed_when_box_changes() -> None:
    r = score_item(make_doc(bbox=[300.0, 300.0, 400.0, 350.0]), item(review_status="wrong"))
    assert r["verdict"] == "fixed"


def test_wrong_item_no_box_also_counts_as_fixed() -> None:
    r = score_item(make_doc(label=None), item(review_status="wrong"))
    assert r["verdict"] == "fixed"


def test_expects_box_rules() -> None:
    assert expects_box(item()) is True
    assert expects_box(item(review_status="wrong")) is False
    # 人工给了修正框 = 正确答案就是这个框，属于最值钱的正样本
    assert expects_box(item(review_status="wrong", bbox_override=list(BOX))) is True


def test_corrected_item_should_reproduce_the_corrected_box() -> None:
    """判错 + 给了修正框：解析器应当复现修正后的框，而不是「只要不是错框就算过」。"""
    corrected = [60.0, 20.0, 160.0, 70.0]
    golden = item(review_status="wrong", bbox=corrected, bbox_override=corrected)

    # 解析器还停在旧框 → 算 regression（没达到人工给出的正确答案）
    old = score_item(make_doc(bbox=list(BOX)), golden)
    assert old["corrected"] is True
    assert old["verdict"] == "regression"

    # 解析器复现了修正框 → pass
    new = score_item(make_doc(bbox=corrected), golden)
    assert new["verdict"] == "pass"


def test_corrected_item_uses_stricter_threshold() -> None:
    """人工修过框的条目不能用 0.5 放水：偏 20pt（IoU≈0.8）仍判 regression。"""
    corrected = [10.0, 25.0, 110.0, 75.0]  # 相对 BOX 下移 5pt，IoU≈0.82
    golden = item(review_status="wrong", bbox=corrected, bbox_override=corrected)
    r = score_item(make_doc(bbox=list(BOX)), golden)
    assert 0.5 < r["iou"] < 0.9
    assert r["verdict"] == "regression"
    # 放宽阈值就过了——说明是阈值在起作用，不是算错
    loose = score_item(make_doc(bbox=list(BOX)), golden, corrected_threshold=0.5)
    assert loose["verdict"] == "pass"


def test_summarize_counts() -> None:
    results = [
        score_item(make_doc(), item(item_id="a")),
        score_item(make_doc(label=None), item(item_id="b")),
        score_item(make_doc(), item(item_id="c", review_status="wrong")),
        score_item(make_doc(label=None), item(item_id="d", review_status="wrong")),
    ]
    s = summarize(results)
    assert s["total"] == 4
    assert s["positive_total"] == 2 and s["positive_passed"] == 1
    assert s["recall"] == 0.5
    assert s["negative_total"] == 2
    assert s["negative_still_wrong"] == 1
    assert s["negative_fixed"] == 1


# ----------------------------------------------------------------------
# 导出接口
# ----------------------------------------------------------------------
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
        paper = Paper(title="Golden paper", year=2026, status="parsed")
        s.add(paper)
        await s.flush()
        version = PaperVersion(
            paper_id=paper.id,
            kind="preprint",
            file_sha256="f" * 64,
            file_path="pdfs/ab/figure.pdf",  # 相对 storage 根
            parser="docling",
            parser_version="2.150.0",
        )
        s.add(version)
        await s.flush()
        s.add_all(
            [
                Figure(
                    paper_version_id=version.id,
                    page=3,
                    bbox_json=list(BOX),
                    label_raw="Fig. 1",
                    label_norm="fig:1",
                    label_num=1,
                    caption="caption one",
                    cite_key="abc123-v1-fig1",
                    review_status="ok",
                    reviewed_by="human",
                ),
                Figure(
                    paper_version_id=version.id,
                    page=4,
                    bbox_json=[0.0, 0.0, 50.0, 50.0],
                    bbox_override_json=[20.0, 30.0, 120.0, 80.0],
                    label_raw="Fig. 2",
                    label_norm="fig:2",
                    label_num=2,
                    caption="caption two",
                    cite_key="abc123-v1-fig2",
                    review_status="wrong",
                    review_note="框到正文了",
                ),
                Figure(
                    paper_version_id=version.id,
                    page=5,
                    bbox_json=[0.0, 0.0, 10.0, 10.0],
                    label_raw="Fig. 3",
                    label_norm="fig:3",
                    label_num=3,
                    cite_key="abc123-v1-fig3",
                    review_status="pending",
                ),
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


async def test_export_only_reviewed_by_default(client) -> None:
    r = await client.get("/api/dataset/golden")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema"] == SCHEMA
    assert body["item_count"] == 2
    assert {i["review_status"] for i in body["items"]} == {"ok", "wrong"}


async def test_export_is_self_contained(client) -> None:
    body = (await client.get("/api/dataset/golden")).json()
    ok_item = next(i for i in body["items"] if i["review_status"] == "ok")
    assert ok_item["source_path"] == "pdfs/ab/figure.pdf"
    assert ok_item["parser"] == "docling"
    assert ok_item["bbox"] == BOX
    assert ok_item["reviewed_by"] == "human"
    assert ok_item["label_norm"] == "fig:1"

    # 判错的条目要用「修正后的框」——那才是用户认可的答案
    wrong_item = next(i for i in body["items"] if i["review_status"] == "wrong")
    assert wrong_item["bbox"] == [20.0, 30.0, 120.0, 80.0]
    assert wrong_item["bbox_parsed"] == [0.0, 0.0, 50.0, 50.0]
    assert wrong_item["review_note"] == "框到正文了"


async def test_export_can_include_pending(client) -> None:
    body = (await client.get("/api/dataset/golden", params={"include_pending": "true"})).json()
    assert body["item_count"] == 3
    assert "pending" in {i["review_status"] for i in body["items"]}


async def test_export_bboxes_only_keeps_boxes(client) -> None:
    """M9.4：bbox 集是轻量标注集，人工修正框优先，并保留 source_path/page。"""
    body = (await client.get("/api/dataset/golden", params={"format": "bboxes"})).json()
    assert body["schema"] == BBOX_SCHEMA
    assert body["item_count"] == 2
    assert body["skipped_no_bbox"] == 0
    ok_item = next(i for i in body["items"] if i["review_status"] == "ok")
    assert ok_item["bbox"] == BOX
    assert ok_item["bbox_source"] == "parsed"
    assert ok_item["source_path"] == "pdfs/ab/figure.pdf"
    assert ok_item["page"] == 3
    assert "caption" not in ok_item  # bbox 集不带正文，避免误当 golden 全量
    wrong_item = next(i for i in body["items"] if i["review_status"] == "wrong")
    assert wrong_item["bbox"] == [20.0, 30.0, 120.0, 80.0]
    assert wrong_item["bbox_source"] == "override"


# ----------------------------------------------------------------------
# 回放
# ----------------------------------------------------------------------
class FakeParser:
    name = "fake"

    def __init__(self, doc: ParsedDocument, counter: list[int]) -> None:
        self.doc = doc
        self.counter = counter

    async def parse(self, pdf_path):  # noqa: ANN001
        self.counter.append(1)
        return self.doc


def golden_of(*items: dict) -> dict:
    return {"schema": SCHEMA, "item_count": len(items), "items": list(items)}


async def test_replay_same_parser_recall_is_one(tmp_path) -> None:
    counter: list[int] = []
    report = await replay(
        golden_of(item(item_id="a"), item(item_id="b")),
        backend="fake",
        parser_factory=lambda name: FakeParser(make_doc(), counter),
        storage_root=tmp_path,
    )
    assert report["summary"]["recall"] == 1.0
    # 同一篇 PDF 的两条 golden 只应该解析一次
    assert len(counter) == 1


async def test_replay_detects_regression(tmp_path) -> None:
    report = await replay(
        golden_of(item(item_id="a")),
        backend="fake",
        parser_factory=lambda name: FakeParser(make_doc(label=None), []),
        storage_root=tmp_path,
    )
    assert report["summary"]["positive_passed"] == 0
    assert report["results"][0]["verdict"] == "no_box"


async def test_replay_reports_errors_instead_of_crashing(tmp_path) -> None:
    """PDF 越界/缺失时给一条 error，不影响其它条目。"""
    bad = item(item_id="bad")
    bad["source_path"] = "../outside.pdf"
    report = await replay(
        golden_of(bad), backend="fake", parser_factory=lambda name: FakeParser(make_doc(), []),
        storage_root=tmp_path,
    )
    assert report["summary"]["errors"] == 1
    assert report["results"][0]["verdict"] == "error"


# ----------------------------------------------------------------------
# 软删除
# ----------------------------------------------------------------------
async def test_export_skips_soft_deleted_figures(tmp_path, monkeypatch) -> None:
    """软删除的图不能进 golden：用户已经说它不该存在，当正样本会污染回放。"""
    from datetime import datetime, timezone

    from litbench.golden import export_golden

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'golden_deleted.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as s:
            paper = Paper(title="Soft deleted golden", status="parsed")
            s.add(paper)
            await s.flush()
            version = PaperVersion(
                paper_id=paper.id,
                file_sha256="e" * 64,
                file_path="pdfs/soft.pdf",
                parser="pdfium",
            )
            s.add(version)
            await s.flush()
            s.add_all(
                [
                    Figure(
                        paper_version_id=version.id,
                        page=1,
                        bbox_json=list(BOX),
                        label_raw="Fig. 1",
                        label_norm="fig:1",
                        cite_key="soft01-v1-fig1",
                        review_status="ok",
                    ),
                    Figure(
                        paper_version_id=version.id,
                        page=2,
                        bbox_json=list(BOX),
                        label_raw="Fig. 2",
                        label_norm="fig:2",
                        cite_key="soft01-v1-fig2",
                        review_status="ok",
                        deleted_at=datetime.now(timezone.utc),
                    ),
                ]
            )
            await s.commit()
            body = await export_golden(s)
        assert body["item_count"] == 1
        assert [entry["cite_key"] for entry in body["items"]] == ["soft01-v1-fig1"]
    finally:
        await engine.dispose()
