# SPDX-License-Identifier: AGPL-3.0-or-later
"""知识库（M1）测试：collections CRUD / 文献过滤 / Zotero 只读导入。

覆盖决策 D1 的硬约束：
* 导入幂等（重复导入不重复建集合/文献）；
* Zotero 集合 → 本地目录树；条目按 zotero_item_key 匹配；
* sqlite 源用只读 URI，不改用户库（导入前后文件字节不变）；
* 手动集合不会被 Zotero 同步删掉。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import litbench.zotero as zotero_mod
from litbench.config import settings
from litbench.db import get_session
from litbench.main import app
from litbench.models import Base
from litbench.zotero import (
    ZCollection,
    ZItem,
    ZoteroImportError,
    load_api_source,
    load_source,
    load_sqlite_source,
)

FIXTURE = {
    "collections": [
        {"key": "COL1", "data": {"name": "传热学", "parentCollection": False}},
        {"key": "COL2", "data": {"name": "第7章 相变传热", "parentCollection": "COL1"}},
    ],
    "items": [
        {
            "key": "ITEM1",
            "version": 3,
            "data": {
                "itemType": "journalArticle",
                "title": "Phase change heat transfer",
                "date": "2020-05-01",
                "DOI": "10.1000/abc",
                "publicationTitle": "Int. J. Heat Mass Transfer",
                "abstractNote": "A study of phase change heat transfer.",
                "creators": [{"firstName": "Ada", "lastName": "Byron"}],
                "tags": [{"tag": "传热"}, {"tag": "相变"}],
                "collections": ["COL2"],
            },
        },
        {
            "key": "ITEM2",
            "data": {
                "itemType": "book",
                "title": "传热学（第六版）",
                "date": "2019",
                "publicationTitle": "高等教育出版社",
                "creators": [],
            },
        },
    ],
}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'kb.db').as_posix()}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with Session() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.tmp_path = tmp_path  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def _import(client, payload: dict) -> dict:
    r = await client.post("/api/import/zotero", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["stats"]


async def test_collections_crud_and_move(client) -> None:
    r = await client.post("/api/collections", json={"name": "父"})
    assert r.status_code == 201, r.text
    parent = r.json()
    r = await client.post("/api/collections", json={"name": "子", "parent_id": parent["id"]})
    child = r.json()

    r = await client.patch(
        f"/api/collections/{child['id']}", json={"parent_id": parent["id"]}
    )
    assert r.status_code == 200
    r = await client.patch(f"/api/collections/{parent['id']}", json={"parent_id": child["id"]})
    assert r.status_code == 422  # 不能挂到自己的子集合下

    r = await client.get("/api/collections")
    assert len(r.json()) == 2

    r = await client.delete(f"/api/collections/{parent['id']}")
    assert r.status_code == 200
    r = await client.get("/api/collections")
    assert len(r.json()) == 1
    assert r.json()[0]["parent_id"] is None  # 子集合上提


async def test_import_json_is_idempotent_and_builds_tree(client) -> None:
    fixture = client.tmp_path / "zotero.json"
    fixture.write_text(json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")

    stats = await _import(client, {"source": "json", "path": str(fixture)})
    assert stats["collections_created"] == 2
    assert stats["papers_created"] == 2
    assert stats["links_added"] == 1
    assert stats["source"] == "json"

    cols = (await client.get("/api/collections")).json()
    by_key = {c["zotero_key"]: c for c in cols}
    assert by_key["COL2"]["parent_id"] == by_key["COL1"]["id"]
    assert by_key["COL2"]["paper_count"] == 1

    papers = (await client.get("/api/papers")).json()
    item1 = next(p for p in papers if p["zotero_item_key"] == "ITEM1")
    assert item1["metadata_source"] == "zotero"
    assert item1["year"] == 2020
    assert item1["tags_json"] == ["传热", "相变"]
    assert "phase change" in (item1["abstract"] or "").lower()

    filtered = (await client.get("/api/papers", params={"collection_id": by_key["COL2"]["id"]})).json()
    assert [p["zotero_item_key"] for p in filtered] == ["ITEM1"]
    # 点父集合 = 看整棵子树
    filtered = (await client.get("/api/papers", params={"collection_id": by_key["COL1"]["id"]})).json()
    assert [p["zotero_item_key"] for p in filtered] == ["ITEM1"]

    # 第二次导入：不重复建
    stats2 = await _import(client, {"source": "json", "path": str(fixture)})
    assert stats2["collections_created"] == 0
    assert stats2["papers_created"] == 0
    assert len((await client.get("/api/papers")).json()) == 2


async def test_manual_collection_survives_zotero_sync(client) -> None:
    fixture = client.tmp_path / "zotero.json"
    fixture.write_text(json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")
    await _import(client, {"source": "json", "path": str(fixture)})

    paper = (await client.get("/api/papers")).json()[0]
    r = await client.post("/api/collections", json={"name": "我的收藏"})
    manual = r.json()
    await client.post(
        f"/api/collections/{manual['id']}/papers", json={"paper_id": paper["id"]}
    )

    await _import(client, {"source": "json", "path": str(fixture)})
    cols = (await client.get("/api/collections")).json()
    assert any(c["id"] == manual["id"] for c in cols)
    filtered = (
        await client.get("/api/papers", params={"collection_id": manual["id"]})
    ).json()
    assert [p["id"] for p in filtered] == [paper["id"]]


async def test_import_json_upload(client) -> None:
    """不上传本机 Zotero 也能用：把 Zotero 导出的 JSON 传上来。"""
    r = await client.post(
        "/api/import/zotero/file",
        files={"file": ("zotero.json", json.dumps(FIXTURE, ensure_ascii=False), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["papers_created"] == 2
    assert len((await client.get("/api/papers")).json()) == 2


def _make_zotero_sqlite(path) -> None:
    """造一个最小 Zotero sqlite 库（schema 与真机 Zotero 10 对齐的子集）。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table itemTypes(itemTypeID integer primary key, typeName text);
        create table items(itemID integer primary key, key text, itemTypeID integer);
        create table fields(fieldID integer primary key, fieldName text);
        create table itemDataValues(valueID integer primary key, value text);
        create table itemData(itemID integer, fieldID integer, valueID integer);
        create table collections(collectionID integer primary key, collectionName text,
                                  parentCollectionID integer, key text);
        create table collectionItems(collectionID integer, itemID integer);
        create table tags(tagID integer primary key, name text);
        create table itemTags(itemID integer, tagID integer);
        create table creators(creatorID integer primary key, firstName text, lastName text,
                              fieldMode integer);
        create table itemCreators(itemID integer, creatorID integer, orderIndex integer);
        insert into itemTypes values (1, 'journalArticle');
        insert into items values (10, 'ZKEY1', 1);
        insert into fields values (1, 'title'), (2, 'date'), (3, 'DOI');
        insert into itemDataValues values (1, 'Test paper'), (2, '2022'), (3, '10.1/x');
        insert into itemData values (10, 1, 1), (10, 2, 2), (10, 3, 3);
        insert into collections values (5, '我的文库子集', null, 'CKEY1');
        insert into collectionItems values (5, 10);
        insert into tags values (1, 'topic');
        insert into itemTags values (10, 1);
        insert into creators values (1, 'Ada', 'Byron', 0);
        insert into itemCreators values (10, 1, 0);
        """
    )
    conn.commit()
    conn.close()


async def test_sqlite_source_reads_without_writing(tmp_path) -> None:
    db = tmp_path / "zotero.sqlite"
    _make_zotero_sqlite(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    cols, items = load_sqlite_source(db)

    assert [c.name for c in cols] == ["我的文库子集"]
    assert len(items) == 1
    item = items[0]
    assert item.key == "ZKEY1"
    assert item.title == "Test paper"
    assert item.year == 2022
    assert item.doi == "10.1/x"
    assert item.tags == ["topic"]
    assert item.collection_keys == ["CKEY1"]
    assert item.creators == [{"firstName": "Ada", "lastName": "Byron"}]

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before  # 只读，没改用户库


# ---------------------------------------------------------------------------
# M1.5 真机实测后的补丁：auto 来源回退、锁库/未启用 local API 的可读报错
# ---------------------------------------------------------------------------
async def test_auto_falls_back_to_local_api_when_sqlite_locked(tmp_path, monkeypatch) -> None:
    """Zotero 运行时 sqlite 被锁（真机实测如此），auto 应自动改走 local API。"""
    db = tmp_path / "zotero.sqlite"
    db.write_bytes(b"placeholder")
    monkeypatch.setattr(settings, "zotero_sqlite", str(db))

    def _locked(_path, **_kwargs) -> None:
        raise ZoteroImportError("Zotero 正在运行，数据库被锁")

    async def _api(_base, **_kwargs):
        return (
            [ZCollection(key="C1", name="文献综述")],
            [ZItem(key="I1", title="Attention Is All You Need")],
        )

    monkeypatch.setattr(zotero_mod, "load_sqlite_source", _locked)
    monkeypatch.setattr(zotero_mod, "load_api_source", _api)

    async def _not_alive(_base: str) -> bool:
        return False

    monkeypatch.setattr(zotero_mod, "_api_alive", _not_alive)

    actual, cols, items = await load_source("auto")
    assert actual == "api"
    assert [c.name for c in cols] == ["文献综述"]
    assert [i.key for i in items] == ["I1"]


async def test_auto_prefers_local_api_when_zotero_running(tmp_path, monkeypatch) -> None:
    """Zotero 开着（local API 活着）时先走 API，不去白等 sqlite 的锁超时。"""
    db = tmp_path / "zotero.sqlite"
    db.write_bytes(b"placeholder")
    monkeypatch.setattr(settings, "zotero_sqlite", str(db))

    def _sqlite_should_not_be_called(_path, **_kwargs) -> None:
        raise AssertionError("auto 应优先 API，不应碰 sqlite")

    async def _api(_base, **_kwargs):
        return ([], [ZItem(key="I2", title="resnet")])

    async def _alive(_base: str) -> bool:
        return True

    monkeypatch.setattr(zotero_mod, "load_sqlite_source", _sqlite_should_not_be_called)
    monkeypatch.setattr(zotero_mod, "load_api_source", _api)
    monkeypatch.setattr(zotero_mod, "_api_alive", _alive)

    actual, _cols, items = await load_source("auto")
    assert actual == "api"
    assert [i.key for i in items] == ["I2"]


async def test_auto_reports_both_sources_when_unavailable(tmp_path, monkeypatch) -> None:
    db = tmp_path / "zotero.sqlite"
    db.write_bytes(b"placeholder")
    monkeypatch.setattr(settings, "zotero_sqlite", str(db))

    def _sqlite_fail(_path, **_kwargs) -> None:
        raise ZoteroImportError("sqlite 也挂了")

    async def _api_fail(_base, **_kwargs):
        raise ZoteroImportError("local API 也连不上")

    monkeypatch.setattr(zotero_mod, "load_sqlite_source", _sqlite_fail)
    monkeypatch.setattr(zotero_mod, "load_api_source", _api_fail)

    async def _not_alive(_base: str) -> bool:
        return False

    monkeypatch.setattr(zotero_mod, "_api_alive", _not_alive)

    with pytest.raises(ZoteroImportError) as excinfo:
        await load_source("auto")
    message = str(excinfo.value)
    assert "sqlite 也挂了" in message
    assert "local API 也连不上" in message


def test_sqlite_locked_error_is_actionable(tmp_path, monkeypatch) -> None:
    db = tmp_path / "zotero.sqlite"
    db.write_bytes(b"placeholder")

    def _raise(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", _raise)
    with pytest.raises(ZoteroImportError) as excinfo:
        load_sqlite_source(db)
    assert "退出 Zotero" in str(excinfo.value)


async def test_local_api_disabled_error_is_actionable(monkeypatch) -> None:
    class _FakeResponse:
        status_code = 403
        text = "Local API is not enabled"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            return _FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _FakeClient())
    with pytest.raises(ZoteroImportError) as excinfo:
        await load_api_source("http://127.0.0.1:23119/api/users/0")
    assert "local API" in str(excinfo.value)
    assert "localAPI.enabled" in str(excinfo.value)


def test_sqlite_falls_back_to_immutable_when_shm_not_writable(tmp_path, monkeypatch) -> None:
    """只读环境里 WAL 库建不了 -shm（真机 sandbox 里复现）；-wal 空时应退回 immutable 读主库。"""
    db = tmp_path / "zotero.sqlite"
    _make_zotero_sqlite(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    real_read = zotero_mod._read_sqlite
    calls = {"n": 0}

    def _fake_read(conn, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_read(conn, **_kwargs)

    monkeypatch.setattr(zotero_mod, "_read_sqlite", _fake_read)
    cols, items = load_sqlite_source(db)
    assert calls["n"] == 2  # 第一次失败 → immutable 重试成功
    assert [c.name for c in cols] == ["我的文库子集"]
    assert [i.key for i in items] == ["ZKEY1"]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# B 方案（docs/15 §12.9）：Zotero 附件 PDF 自动导入
# ---------------------------------------------------------------------------
_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _add_paper_item(
    db_path, *, item_id: int = 12, key: str = "ZKEY2", title: str = "Another paper", value_id: int = 4
) -> None:
    """往最小 Zotero 库里再加一篇文献（用于多文献/冲突用例）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("insert into items values (?, ?, 1)", (item_id, key))
    conn.execute("insert into itemDataValues values (?, ?)", (value_id, title))
    conn.execute("insert into itemData values (?, 1, ?)", (item_id, value_id))
    conn.commit()
    conn.close()


def _add_attachment(
    db_path,
    storage_root,
    *,
    item_id: int = 11,
    parent_item_id: int = 10,
    att_key: str = "ATTKEY1",
    filename: str = "paper.pdf",
    payload: bytes | None = _PDF_BYTES,
    link_mode: int = 0,
    content_type: str | None = "application/pdf",
    db_path_value: str | None = None,
    item_type_id: int = 2,
) -> str:
    """给最小库加一个附件条目，并按 linkMode 在磁盘上放好文件。

    ``payload=None`` 表示「DB 里有附件记录、但文件不在磁盘上」（真机常见：文件被删/未同步）。
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        create table if not exists itemAttachments(
            itemID integer primary key, parentItemID integer, linkMode integer,
            contentType text, charsetID integer, path text);
        """
    )
    conn.execute("insert into items values (?, ?, ?)", (item_id, att_key, item_type_id))
    raw = db_path_value if db_path_value is not None else f"storage:{filename}"
    conn.execute(
        "insert into itemAttachments values (?, ?, ?, ?, null, ?)",
        (item_id, parent_item_id, link_mode, content_type, raw),
    )
    conn.commit()
    conn.close()

    target = Path(storage_root) / att_key / filename if link_mode in (0, 1) else Path(raw)
    if payload is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return str(target)


async def test_sqlite_attachment_resolves_stored_pdf(tmp_path) -> None:
    db = tmp_path / "zotero.sqlite"
    _make_zotero_sqlite(db)
    storage = tmp_path / "storage"  # sqlite 源默认用 db 同级的 storage/
    target = _add_attachment(db, storage)

    _cols, items = load_sqlite_source(db)
    item = items[0]
    assert len(item.attachments) == 1
    att = item.attachments[0]
    assert att.filename == "paper.pdf"
    assert att.content_type == "application/pdf"
    assert att.file_path == target


def test_sqlite_linked_attachment_uses_absolute_path(tmp_path) -> None:
    db = tmp_path / "zotero.sqlite"
    _make_zotero_sqlite(db)
    linked = tmp_path / "Downloads" / "linked-paper.pdf"
    target = _add_attachment(
        db, tmp_path / "storage", link_mode=2, db_path_value=str(linked)
    )
    _cols, items = load_sqlite_source(db)
    att = items[0].attachments[0]
    assert att.filename == "linked-paper.pdf"
    assert att.file_path == target


async def test_api_source_reads_attachment_link_mode_and_enclosure(tmp_path, monkeypatch) -> None:
    """真机 2026-11-12 实测：local API 的 linkMode 是字符串、真实路径在 enclosure 里。"""
    pdf = tmp_path / "storage" / "ATT1" / "paper.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(_PDF_BYTES)

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url, params=None):
            params = params or {}
            if "itemType" in params:  # 附件列表
                return _Resp(
                    [
                        {
                            "key": "ATT1",
                            "data": {
                                "itemType": "attachment",
                                "parentItem": "ITEM1",
                                "linkMode": "imported_file",
                                "contentType": "application/pdf",
                                "filename": "paper.pdf",
                            },
                            "links": {"enclosure": {"href": pdf.as_uri()}},
                        }
                    ]
                )
            return _Resp(
                [{"key": "ITEM1", "data": {"itemType": "journalArticle", "title": "Paper"}}]
            )

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _FakeClient())
    _cols, items = await load_api_source("http://127.0.0.1:23119/api/users/0")
    assert [i.key for i in items] == ["ITEM1"]
    assert len(items[0].attachments) == 1
    att = items[0].attachments[0]
    assert att.link_mode == 0  # "imported_file" → 0，不能 int() 直接炸
    assert att.filename == "paper.pdf"
    assert att.file_path == str(pdf)


async def test_import_zotero_imports_attachment_pdf_and_queues_parse(client) -> None:
    """B 的核心：Zotero 附件 PDF 复制进本库、建版本、排解析任务；重复导入幂等。"""
    db = client.tmp_path / "zotero.sqlite"
    _make_zotero_sqlite(db)
    _add_attachment(db, client.tmp_path / "storage")

    stats = await _import(client, {"source": "sqlite", "path": str(db)})
    assert stats["papers_created"] == 1
    assert stats["pdfs_imported"] == 1
    assert stats["pdf_files_missing"] == 0

    papers = (await client.get("/api/papers")).json()
    paper = next(p for p in papers if p["zotero_item_key"] == "ZKEY1")
    assert paper["has_version"] is True
    # 元数据仍以 Zotero 为准
    assert paper["title"] == "Test paper"
    assert paper["metadata_source"] == "zotero"

    jobs = (await client.get("/api/jobs")).json()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "parse"
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["paper_version_id"]

    digest = hashlib.sha256(_PDF_BYTES).hexdigest()
    copied = client.tmp_path / "pdfs" / digest[:2] / f"{digest}.pdf"
    assert copied.read_bytes() == _PDF_BYTES

    # 第二次导入：不重复建版本/任务，只统计跳过
    stats2 = await _import(client, {"source": "sqlite", "path": str(db)})
    assert stats2["papers_created"] == 0
    assert stats2["pdfs_imported"] == 0
    assert stats2["pdfs_skipped"] == 1
    assert len((await client.get("/api/jobs")).json()) == 1


async def test_import_zotero_reports_missing_attachment_file(client) -> None:
    """有附件记录但磁盘上没文件：不报错、不建版本，统计里如实告诉用户。"""
    db = client.tmp_path / "zotero.sqlite"
    _make_zotero_sqlite(db)
    _add_attachment(db, client.tmp_path / "storage", payload=None)

    stats = await _import(client, {"source": "sqlite", "path": str(db)})
    assert stats["papers_created"] == 1
    assert stats["pdfs_imported"] == 0
    assert stats["pdf_files_missing"] == 1

    paper = (await client.get("/api/papers")).json()[0]
    assert paper["has_version"] is False
    assert (await client.get("/api/jobs")).json() == []


async def test_import_zotero_does_not_merge_shared_pdf(client) -> None:
    """同一个 PDF 挂在两个 Zotero 条目下：只给先到的那篇建版本，不替用户合并文献。"""
    db = client.tmp_path / "zotero.sqlite"
    _make_zotero_sqlite(db)
    _add_paper_item(db)
    storage = client.tmp_path / "storage"
    _add_attachment(db, storage, item_id=11, parent_item_id=10, att_key="ATT1")
    _add_attachment(db, storage, item_id=13, parent_item_id=12, att_key="ATT2")

    stats = await _import(client, {"source": "sqlite", "path": str(db)})
    assert stats["papers_created"] == 2
    assert stats["pdfs_imported"] == 1
    assert stats["pdfs_skipped"] == 1

    papers = (await client.get("/api/papers")).json()
    assert len(papers) == 2
    assert sum(1 for p in papers if p["has_version"]) == 1
    assert len((await client.get("/api/jobs")).json()) == 1


