# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zotero 只读导入（M1，见 docs/15 决策 D1）。

三种来源，输出统一的 `ZCollection` / `ZItem`：

* ``json``   —— Zotero「导出库」/ local API 形状的 JSON 文件（离线、测试）；
* ``sqlite`` —— 用户指定的 ``zotero.sqlite``，以 **只读 URI** 打开（不复制、不写）；
* ``api``    —— Zotero 7 local API（``http://127.0.0.1:23119/api/users/0``，Zotero 运行时可用）。

为什么只读：Zotero 继续当文献库主人，这边只镜像集合树 + 条目元数据；LitBench 的标注、
摘要、增删图都不回写 Zotero（D1 决策）。附件 PDF 也只是**复制一份**进本库（原文件不动），
导入后由本库自己解析（B 方案，见 docs/15 §12.9）。导入是幂等的：集合按 ``zotero_key``、条目按
``zotero_item_key`` 匹配，重复导入只更新、不重复建；PDF 按 sha256 去重。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .labels import normalize_title
from .models import Collection, ImportRun, Job, Paper, PaperCollection, PaperVersion
from .paths import to_storage_relative

log = logging.getLogger("litbench.zotero")

# Zotero 里这些条目才是文献；附件/笔记/独立图片跳过
_ITEM_TYPES = {
    "journalArticle",
    "conferencePaper",
    "book",
    "bookSection",
    "thesis",
    "preprint",
    "report",
    "manuscript",
    "webpage",
    "encyclopediaArticle",
    "dictionaryEntry",
}

ARXIV_RE = re.compile(r"arXiv:?\s*(\d{4}\.\d{4,5})", re.IGNORECASE)

# Zotero ``itemType`` → ``paper.entry_type``（迁移 0015）。只做一层稳定映射：
# references 需要知道这是不是学位论文/书/报告，太细的分类对条目格式没有意义。
_ZOTERO_ENTRY_TYPES = {
    "journalArticle": "journal",
    "conferencePaper": "conference",
    "preprint": "preprint",
    "thesis": "thesis",
    "book": "book",
    "bookSection": "book",
    "report": "report",
}


def zotero_entry_type(item_type: str | None) -> str:
    """Zotero itemType → 库内 entry_type；未识别的归 ``other``（不落 journal 冒充期刊）。"""
    return _ZOTERO_ENTRY_TYPES.get((item_type or "").strip(), "other")


class ZoteroImportError(RuntimeError):
    """导入失败（Zotero 没开 / 路径不对 / schema 不识别）。错误信息要能直接给用户看。"""


@dataclass(slots=True)
class ZCollection:
    key: str
    name: str
    parent_key: str | None = None
    sort_order: int = 0


@dataclass(slots=True)
class ZAttachment:
    """Zotero 附件（M2b 起：只取 PDF，导入时复制进 LitBench 再解析）。"""

    key: str
    filename: str | None = None
    file_path: str | None = None  # 能解析到的绝对路径（stored 用 storage root 拼，linked 用原路径）
    content_type: str | None = None
    link_mode: int | None = None


@dataclass(slots=True)
class ZItem:
    key: str
    title: str
    version: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    year: int | None = None
    venue: str | None = None
    # references 条目要用的著录字段（2026-11-13）；Zotero 没有就留 None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    abstract: str | None = None
    tags: list[str] = field(default_factory=list)
    creators: list[dict] = field(default_factory=list)
    collection_keys: list[str] = field(default_factory=list)
    item_type: str = "journalArticle"
    # 条目类型与学位论文著录字段（迁移 0015）：Zotero 的 thesis 条目带
    # university / thesisType / place，导入时落库，references 生成 [D] 要用。
    entry_type: str = "journal"
    thesis_school: str | None = None
    thesis_degree: str | None = None
    thesis_place: str | None = None
    attachments: list[ZAttachment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 来源 1：JSON（local API 形状 / 自定义 {"collections","items"}）
# ---------------------------------------------------------------------------
def load_json_source(path: Path) -> tuple[list[ZCollection], list[ZItem]]:
    if not path.is_file():
        raise ZoteroImportError(f"JSON 文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 解析错误要转成用户能看懂的
        raise ZoteroImportError(f"JSON 解析失败：{exc}") from exc

    raw_collections: list = []
    raw_items: list = []
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_collections = data.get("collections") or []
        raw_items = data.get("items") or []
    else:
        raise ZoteroImportError("JSON 顶层必须是条目数组，或 {collections, items} 对象")

    collections: list[ZCollection] = []
    for entry in raw_collections:
        payload = entry.get("data") if isinstance(entry, dict) and "data" in entry else entry
        if not isinstance(payload, dict):
            continue
        key = str(entry.get("key") or payload.get("key") or "")
        name = str(payload.get("name") or "").strip()
        if not key or not name:
            continue
        parent = payload.get("parentCollection") or payload.get("parentCollectionKey")
        collections.append(
            ZCollection(
                key=key,
                name=name,
                parent_key=str(parent) if parent else None,
            )
        )

    items: list[ZItem] = []
    for entry in raw_items:
        payload = entry.get("data") if isinstance(entry, dict) and "data" in entry else entry
        if not isinstance(payload, dict):
            continue
        if str(payload.get("itemType") or "") in ("attachment", "note", "annotation"):
            continue
        item = _item_from_mapping(entry, payload)
        if item is not None:
            items.append(item)
    return collections, items


# ---------------------------------------------------------------------------
# 来源 2：Zotero SQLite（只读 URI，不写用户库）
# ---------------------------------------------------------------------------
def load_sqlite_source(
    path: Path, *, timeout: float = 5.0, storage_root: Path | None = None
) -> tuple[list[ZCollection], list[ZItem]]:
    if not path.is_file():
        raise ZoteroImportError(f"zotero.sqlite 不存在：{path}")
    if storage_root is None:
        storage_root = path.parent / "storage"
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    except sqlite3.Error as exc:
        raise _friendly_sqlite_error(exc) from exc
    conn.row_factory = sqlite3.Row
    try:
        try:
            _validate_zotero_schema(conn)
            return _read_sqlite(conn, storage_root=storage_root)
        except sqlite3.Error as exc:
            # WAL 库的只读连接需要创建/写 -shm；只读介质或受限进程会拿到
            # "unable to open database file"。若 -wal 已为空/不存在（Zotero 正常退出，
            # 主库就是最新状态），退回 immutable 只读打开主库——仍然不写用户库。
            if "unable to open database file" in str(exc).lower() and _wal_empty_or_missing(path):
                imm = sqlite3.connect(
                    f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True, timeout=timeout
                )
                imm.row_factory = sqlite3.Row
                try:
                    _validate_zotero_schema(imm)
                    return _read_sqlite(imm, storage_root=storage_root)
                finally:
                    imm.close()
            raise _friendly_sqlite_error(exc) from exc
    finally:
        conn.close()


def _validate_zotero_schema(conn: sqlite3.Connection) -> None:
    tables = {r["name"] for r in conn.execute("select name from sqlite_master where type='table'")}
    for required in ("items", "collections", "itemData", "fields", "itemDataValues"):
        if required not in tables:
            raise ZoteroImportError("这不是 Zotero 数据库（缺少 items/collections 等表）")


def _wal_empty_or_missing(path: Path) -> bool:
    wal = path.with_name(path.name + "-wal")
    try:
        return (not wal.exists()) or wal.stat().st_size == 0
    except OSError:
        return False


def _friendly_sqlite_error(exc: sqlite3.Error) -> ZoteroImportError:
    """把 sqlite 层的错误转成用户能照着做的一句话（M1.5 真机实测补充）。"""
    low = str(exc).lower()
    if "locked" in low or "busy" in low:
        return ZoteroImportError(
            "Zotero 正在运行，数据库被锁，只读读取失败。"
            "请先完全退出 Zotero 再用 sqlite 来源；或启用 Zotero local API 后改用 api/auto 来源"
        )
    return ZoteroImportError(f"读取 Zotero 数据库失败：{exc}")


def _read_sqlite(
    conn: sqlite3.Connection, *, storage_root: Path | None = None
) -> tuple[list[ZCollection], list[ZItem]]:
    col_rows = conn.execute(
        "select collectionID, collectionName, parentCollectionID, key from collections"
    ).fetchall()
    id_to_key = {int(r["collectionID"]): str(r["key"]) for r in col_rows}
    collections = [
        ZCollection(
            key=str(r["key"]),
            name=str(r["collectionName"]),
            parent_key=id_to_key.get(int(r["parentCollectionID"])) if r["parentCollectionID"] else None,
        )
        for r in col_rows
    ]

    type_rows = conn.execute("select itemTypeID, typeName from itemTypes").fetchall()
    type_name = {int(r["itemTypeID"]): str(r["typeName"]) for r in type_rows}
    item_rows = conn.execute("select itemID, key, itemTypeID from items").fetchall()
    item_meta = {
        int(r["itemID"]): (str(r["key"]), type_name.get(int(r["itemTypeID"]), ""))
        for r in item_rows
    }
    wanted_ids = {iid for iid, (_k, t) in item_meta.items() if t in _ITEM_TYPES}

    fields = {int(r["fieldID"]): str(r["fieldName"]) for r in conn.execute("select * from fields")}
    values: dict[int, str] = {}
    if "itemDataValues" in {r["name"] for r in conn.execute("select name from sqlite_master where type='table'")}:
        values = {int(r["valueID"]): str(r["value"]) for r in conn.execute("select * from itemDataValues")}
    data: dict[int, dict[str, str]] = {}
    for r in conn.execute("select itemID, fieldID, valueID from itemData"):
        iid = int(r["itemID"])
        if iid not in wanted_ids:
            continue
        fname = fields.get(int(r["fieldID"]))
        if fname:
            data.setdefault(iid, {})[fname] = values.get(int(r["valueID"]), "")

    tags: dict[int, list[str]] = {}
    for r in conn.execute("select itemID, name from itemTags t join tags g on g.tagID=t.tagID"):
        iid = int(r["itemID"])
        if iid in wanted_ids:
            tags.setdefault(iid, []).append(str(r["name"]))

    creators: dict[int, list[dict]] = {}
    for r in conn.execute(
        """select ic.itemID, ic.orderIndex, c.firstName, c.lastName, c.fieldMode
           from itemCreators ic join creators c on c.creatorID = ic.creatorID
           order by ic.itemID, ic.orderIndex"""
    ):
        iid = int(r["itemID"])
        if iid not in wanted_ids:
            continue
        if int(r["fieldMode"] or 0) == 1:
            name = str(r["lastName"] or "")
            creators.setdefault(iid, []).append({"name": name})
        else:
            creators.setdefault(iid, []).append(
                {"firstName": r["firstName"], "lastName": r["lastName"]}
            )

    links: dict[int, list[str]] = {}
    for r in conn.execute("select collectionID, itemID from collectionItems"):
        iid = int(r["itemID"])
        if iid in wanted_ids:
            ck = id_to_key.get(int(r["collectionID"]))
            if ck:
                links.setdefault(iid, []).append(ck)

    # 附件（M2b）：stored 附件在 <dataDir>/storage/<attachmentKey>/<filename>；
    # linked 附件在 path 里（可能是绝对路径或 attachments: 前缀）。
    attachments: dict[int, list[ZAttachment]] = {}
    try:
        att_rows = conn.execute(
            """select ia.itemID, ia.parentItemID, ia.path, ia.contentType, ia.linkMode, i.key
               from itemAttachments ia join items i on i.itemID = ia.itemID"""
        ).fetchall()
    except sqlite3.Error:
        att_rows = []
    for r in att_rows:
        parent_id = int(r["parentItemID"] or 0)
        if parent_id not in wanted_ids:
            continue
        att_key = str(r["key"])
        raw_path = str(r["path"] or "")
        mode = int(r["linkMode"] or 0)
        filename: str | None = None
        file_path: str | None = None
        if mode in (0, 1):  # 导入到 Zotero 的文件：path 存 "storage:<filename>"
            stored_name = raw_path[len("storage:"):] if raw_path.startswith("storage:") else raw_path
            filename = Path(stored_name).name if stored_name else None
            if filename and storage_root is not None:
                candidate = Path(storage_root) / att_key / filename
                file_path = str(candidate) if candidate.is_file() else None
        elif mode in (2, 3):  # 链接文件：path 是绝对路径（或 attachments: 前缀的相对路径）
            candidate_raw = raw_path[len("attachments:"):] if raw_path.startswith("attachments:") else raw_path
            if candidate_raw:
                candidate = Path(candidate_raw)
                filename = candidate.name
                file_path = str(candidate) if candidate.is_file() else None
        attachments.setdefault(parent_id, []).append(
            ZAttachment(
                key=att_key,
                filename=filename,
                file_path=file_path,
                content_type=str(r["contentType"] or "") or None,
                link_mode=mode,
            )
        )

    items = []
    for iid, (key, itype) in item_meta.items():
        if iid not in wanted_ids:
            continue
        f = data.get(iid, {})
        items.append(
            ZItem(
                key=key,
                title=(f.get("title") or "").strip() or "（无标题）",
                doi=(f.get("DOI") or None),
                arxiv_id=_arxiv_from(f),
                year=_year_from_date(f.get("date")),
                venue=f.get("publicationTitle") or f.get("proceedingsTitle"),
                volume=_text_or_none(f.get("volume")),
                issue=_text_or_none(f.get("issue")),
                pages=_text_or_none(f.get("pages")),
                abstract=f.get("abstractNote"),
                tags=tags.get(iid, []),
                creators=creators.get(iid, []),
                collection_keys=links.get(iid, []),
                item_type=itype,
                attachments=attachments.get(iid, []),
            )
        )
    return collections, items


# ---------------------------------------------------------------------------
# 来源 3：Zotero 7 local API
# ---------------------------------------------------------------------------
async def load_api_source(
    base: str, *, timeout: float = 10.0, storage_root: Path | None = None
) -> tuple[list[ZCollection], list[ZItem]]:
    import httpx

    if storage_root is None:
        storage_root = _default_storage_root()
    base = base.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            raw_cols = await _get_all(client, f"{base}/collections")
            raw_items = await _get_all(client, f"{base}/items")
            raw_atts = await _get_all(
                client, f"{base}/items", extra_params={"itemType": "attachment"}
            )
    except httpx.HTTPError as exc:
        raise ZoteroImportError(
            f"连不上 Zotero local API（{base}）：{exc}。请先打开 Zotero 7，或改用 sqlite/json 源"
        ) from exc

    # 复用 JSON 解析逻辑（local API 形状就是 {"key","version","data"}）
    cols, items = _parse_mapping({"collections": raw_cols, "items": raw_items})

    # 附件：local API 的 attachment data 带 parentItem / filename / linkMode（字符串）
    attachments: dict[str, list[ZAttachment]] = {}
    for entry in raw_atts:
        if not isinstance(entry, dict):
            continue
        data = entry.get("data") if isinstance(entry.get("data"), dict) else entry
        parent = str(data.get("parentItem") or "")
        att_key = str(entry.get("key") or data.get("key") or "")
        if not parent or not att_key:
            continue
        filename = str(data.get("filename") or "") or None
        mode = _link_mode(data.get("linkMode"))
        file_path: str | None = None
        # 先用 enclosure 的真实 file:// 路径（最准），退回 storage/<key>/<filename>
        enclosed = _path_from_enclosure(entry)
        if enclosed is not None and enclosed.is_file():
            file_path = str(enclosed)
            filename = filename or enclosed.name
        elif filename and storage_root is not None:
            candidate = Path(storage_root) / att_key / filename
            file_path = str(candidate) if candidate.is_file() else None
        attachments.setdefault(parent, []).append(
            ZAttachment(
                key=att_key,
                filename=filename,
                file_path=file_path,
                content_type=str(data.get("contentType") or "") or None,
                link_mode=mode,
            )
        )
    for item in items:
        item.attachments = attachments.get(item.key, [])
    return cols, items


async def _get_all(client, url: str, *, extra_params: dict | None = None) -> list:
    out: list = []
    start = 0
    while True:
        params: dict = {"limit": 100, "start": start}
        if extra_params:
            params.update(extra_params)
        resp = await client.get(url, params=params)
        if resp.status_code == 403 and "Local API is not enabled" in resp.text:
            raise ZoteroImportError(
                "Zotero 的 local API 没启用（403 Local API is not enabled）。"
                "在 Zotero 设置 → 高级 → 配置编辑器里把 extensions.zotero.httpServer.localAPI.enabled "
                "设为 true，重启 Zotero 后再试；或改用 sqlite/json 来源"
            )
        if resp.status_code != 200:
            raise ZoteroImportError(f"Zotero local API 返回 {resp.status_code}：{url}")
        chunk = resp.json()
        if not isinstance(chunk, list):
            raise ZoteroImportError(f"Zotero local API 返回格式不对：{url}")
        out.extend(chunk)
        if len(chunk) < 100:
            return out
        start += 100


async def _api_alive(base: str, *, timeout: float = 0.8) -> bool:
    """快速探测本机 Zotero local API 是否在跑。只用于 auto 决定先后顺序，不抛错。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(base.rstrip("/"))
        return resp.status_code < 500
    except (httpx.HTTPError, OSError):
        return False


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def _default_sqlite_paths() -> list[Path]:
    home = Path.home()
    return [
        home / "Zotero" / "zotero.sqlite",
        Path(r"C:\Users") / home.name / "Zotero" / "zotero.sqlite",
    ]


def _default_storage_root() -> Path:
    """Zotero 附件目录（stored attachments）。sqlite 源会优先用 db 同级的 storage/。"""
    from .config import settings

    if settings.zotero_storage is not None:
        return Path(settings.zotero_storage)
    return Path.home() / "Zotero" / "storage"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


async def load_source(
    source: str = "auto", *, path: str | None = None, api_base: str | None = None
) -> tuple[str, list[ZCollection], list[ZItem]]:
    """返回 (实际来源, 集合, 条目)。source=auto 时按 sqlite → api 顺序探测。"""
    from .config import settings

    api = api_base or settings.zotero_api_base
    sqlite_path = Path(path) if path else settings.zotero_sqlite
    if sqlite_path is None:
        sqlite_path = next((p for p in _default_sqlite_paths() if p.is_file()), None)
    storage_root = Path(settings.zotero_storage) if settings.zotero_storage else None

    if source == "auto":
        # M1.5 真机实测：sqlite 与 local API 恰好对应「Zotero 关 / 开」两种现场——
        # Zotero 开着时 zotero.sqlite 被 WAL 锁住，读不了（会白等 timeout）；
        # Zotero 关着时 local API 不存在。所以先花 0.8s 探一下 23119：
        # 活着就优先 API（快且是内存里的最新数据），否则先读 sqlite。
        attempts: list[str] = []
        order = ["sqlite", "api"]
        if await _api_alive(api):
            order = ["api", "sqlite"]
        for candidate in order:
            if candidate == "sqlite":
                if not (sqlite_path and Path(sqlite_path).is_file()):
                    attempts.append(
                        "sqlite：没找到 zotero.sqlite"
                        "（可传 path，或在 .env 里设 ZOTERO_SQLITE）"
                    )
                    continue
                try:
                    cols, items = await asyncio.to_thread(
                        load_sqlite_source,
                        Path(sqlite_path),
                        timeout=2.0,
                        storage_root=storage_root,
                    )
                    return "sqlite", cols, items
                except ZoteroImportError as exc:
                    attempts.append(f"sqlite（{sqlite_path}）：{exc}")
            else:
                try:
                    cols, items = await load_api_source(
                        api, storage_root=storage_root or _default_storage_root()
                    )
                    return "api", cols, items
                except ZoteroImportError as exc:
                    attempts.append(f"local API（{api}）：{exc}")
        raise ZoteroImportError(
            "auto 来源不可用（sqlite 与 local API 都失败）：\n- " + "\n- ".join(attempts)
        )

    if source == "json":
        if not path:
            raise ZoteroImportError("json 来源需要提供 path")
        cols, items = await asyncio.to_thread(load_json_source, Path(path))
        return "json", cols, items
    if source == "sqlite":
        if not sqlite_path:
            raise ZoteroImportError(
                "没找到 zotero.sqlite。请传 path，或在 .env 里设 ZOTERO_SQLITE"
            )
        cols, items = await asyncio.to_thread(
            load_sqlite_source,
            Path(sqlite_path),
            storage_root=storage_root,
        )
        return "sqlite", cols, items
    if source == "api":
        return "api", *(
            await load_api_source(api, storage_root=storage_root or _default_storage_root())
        )
    raise ZoteroImportError(f"未知来源：{source}（支持 auto|json|sqlite|api）")


_PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}


def _is_pdf_attachment(att: ZAttachment) -> bool:
    name = (att.filename or "").lower()
    ctype = (att.content_type or "").lower().split(";")[0].strip()
    return ctype in _PDF_CONTENT_TYPES or name.endswith(".pdf")


# local API 的 linkMode 是字符串（真机实测）；sqlite 里是整数
_LINK_MODES = {"imported_file": 0, "imported_url": 1, "linked_file": 2, "linked_url": 3}


def _link_mode(value) -> int:
    if isinstance(value, int):
        return value
    return _LINK_MODES.get(str(value or "").strip().lower(), 0)


def _path_from_enclosure(entry: dict) -> Path | None:
    """local API 的附件会在 ``links.enclosure.href`` 里给出 file:// 真实路径。

    比「storage/<key>/<filename>」更准（链接文件也能用），真机实测 2026-11-12。
    """
    links = entry.get("links") if isinstance(entry, dict) else None
    enclosure = (links or {}).get("enclosure") or {}
    href = enclosure.get("href")
    if not href:
        return None
    from urllib.parse import unquote, urlparse

    try:
        parsed = urlparse(str(href))
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path)
    if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":  # /C:/...
        raw = raw[1:]
    return Path(raw)


def _copy_pdf(src: Path, dest: Path) -> None:
    """把 Zotero 的 PDF 复制一份到本库。**只 copy 不 move**——Zotero 仍是文件主人。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == src.stat().st_size:
        return
    tmp = dest.with_name(f".{dest.name}.part")
    shutil.copyfile(src, tmp)
    tmp.replace(dest)


async def _import_item_pdfs(
    session: AsyncSession,
    paper: Paper,
    attachments: list[ZAttachment],
    stats: dict,
) -> None:
    """把条目下**第一个可用 PDF** 导入成 ``paper_version`` + 解析任务。

    幂等靠 sha256：同一文件重复导入不建新版本；已经属于别的文献就跳过（不替用户合并文献）。
    额外 PDF（补充材料、重复下载）暂不导入，计入 ``pdfs_skipped``。
    """
    from .config import settings

    pdfs = [a for a in attachments if _is_pdf_attachment(a)]
    if not pdfs:
        return
    source: ZAttachment | None = None
    for att in pdfs:
        if att.file_path and Path(att.file_path).is_file():
            source = att
            break
    if source is None:
        # 有附件记录、但文件不在磁盘上（Zotero 里是「链接文件」且路径已失效等）
        stats["pdf_files_missing"] += len(pdfs)
        return

    src = Path(source.file_path)  # type: ignore[arg-type]
    try:
        digest = await asyncio.to_thread(_sha256_file, src)
    except OSError:
        stats["pdf_files_missing"] += 1
        return

    existing = (
        await session.execute(select(PaperVersion).where(PaperVersion.file_sha256 == digest))
    ).scalar_one_or_none()
    if existing is not None:
        # 已经导入过（同一篇）或已属于别的文献（不合并）
        stats["pdfs_skipped"] += len(pdfs)
        return

    dest = settings.pdf_dir / digest[:2] / f"{digest}.pdf"
    try:
        await asyncio.to_thread(_copy_pdf, src, dest)
    except OSError:
        stats["pdf_files_missing"] += 1
        return

    version = PaperVersion(
        paper_id=paper.id,
        kind="imported",
        file_sha256=digest,
        file_path=to_storage_relative(settings.storage_dir, dest),
    )
    session.add(version)
    await session.flush()
    session.add(Job(kind="parse", paper_version_id=version.id, payload_json={}))
    paper.status = "new"
    stats["pdfs_imported"] += 1
    stats["pdfs_skipped"] += len(pdfs) - 1


async def import_zotero(
    session: AsyncSession,
    *,
    source: str = "auto",
    path: str | None = None,
    api_base: str | None = None,
    owner_user_id: str | None = None,
    is_superuser: bool = False,
) -> dict:
    """执行一次只读导入，返回统计（同时写 import_run 审计）。

    ``owner_user_id``：认证打开时把新建的文献/集合归到当前账号；不传 = legacy
    （单机模式或旧调用），行为与 2026-11-15 之前完全一致。

    越权防线（SOTA 审查 F12）：``zotero_key`` / ``zotero_item_key`` 是全局唯一键，
    但 upsert **只允许命中调用者自己的行**（owner 相同；superuser 可维护全部，
    与 can_edit_paper 同口径）。命中他人的行时跳过并计入 ``*_skipped_foreign``，
    不改名、不覆盖元数据、不注入 PDF 版本、不动集合关联——否则成员 B 上传同一
    份 Zotero 导出就能改写成员 A 的私有文献。DOI 回退匹配同样只认自己的行。
    """
    started = datetime.now(timezone.utc)
    run = ImportRun(source=source, started_at=started, stats_json={})
    session.add(run)
    await session.flush()

    stats = {
        "source": source,
        "collections_seen": 0,
        "collections_created": 0,
        "collections_updated": 0,
        "items_seen": 0,
        "papers_created": 0,
        "papers_updated": 0,
        "links_added": 0,
        "links_removed": 0,
        "skipped": 0,
        # 命中他人 owner 的行：跳过不动（F12 越权防线）
        "collections_skipped_foreign": 0,
        "papers_skipped_foreign": 0,
        # B 方案（docs/15 §12.9）：附件 PDF 复制进本库并排队解析
        "pdfs_imported": 0,
        "pdfs_skipped": 0,
        "pdf_files_missing": 0,
    }
    try:
        actual_source, zcols, zitems = await load_source(
            source, path=path, api_base=api_base
        )
        stats["source"] = actual_source
        stats["collections_seen"] = len(zcols)
        stats["items_seen"] = len(zitems)

        # ---- 集合：先按 key 全量 upsert，再补父子关系（父可能在子后面） ----
        def _updatable(row_owner: str | None) -> bool:
            return is_superuser or row_owner == owner_user_id

        existing_cols = {
            c.zotero_key: c
            for c in (
                await session.execute(
                    select(Collection).where(Collection.zotero_key.isnot(None))
                )
            ).scalars()
        }
        key_to_col: dict[str, Collection] = {}
        for zc in zcols:
            col = existing_cols.get(zc.key)
            if col is not None and not _updatable(col.owner_user_id):
                stats["collections_skipped_foreign"] += 1
                continue
            if col is None:
                col = Collection(
                    name=zc.name,
                    source="zotero",
                    zotero_key=zc.key,
                    owner_user_id=owner_user_id,
                )
                session.add(col)
                await session.flush()
                stats["collections_created"] += 1
            else:
                if col.name != zc.name or col.source != "zotero":
                    col.name = zc.name
                    col.source = "zotero"
                    stats["collections_updated"] += 1
            key_to_col[zc.key] = col

        for zc in zcols:
            col = key_to_col.get(zc.key)
            if col is None:
                continue  # 他人集合被跳过，父子关系也不动
            parent = key_to_col.get(zc.parent_key) if zc.parent_key else None
            parent_id = parent.id if parent is not None else None
            if col.parent_id != parent_id:
                col.parent_id = parent_id
                stats["collections_updated"] += 1

        # ---- 条目 ----
        existing_papers = {
            p.zotero_item_key: p
            for p in (
                await session.execute(
                    select(Paper).where(Paper.zotero_item_key.isnot(None))
                )
            ).scalars()
        }
        now = datetime.now(timezone.utc)
        for zi in zitems:
            paper = existing_papers.get(zi.key)
            if paper is not None and not _updatable(paper.owner_user_id):
                # 同 key 的文献属于别人：不覆盖、不注入版本、不动关联
                stats["papers_skipped_foreign"] += 1
                continue
            if paper is None and zi.doi:
                candidates = (
                    await session.execute(select(Paper).where(Paper.doi == zi.doi))
                ).scalars().all()
                paper = next(
                    (p for p in candidates if _updatable(p.owner_user_id)), None
                )
            if paper is None:
                paper = Paper(
                    title=zi.title or "（无标题）",
                    title_norm=normalize_title(zi.title or ""),
                    doi=zi.doi,
                    arxiv_id=zi.arxiv_id,
                    year=zi.year,
                    venue=zi.venue,
                    volume=zi.volume,
                    issue=zi.issue,
                    pages=zi.pages,
                    entry_type=zi.entry_type,
                    thesis_school=zi.thesis_school,
                    thesis_degree=zi.thesis_degree,
                    thesis_place=zi.thesis_place,
                    authors_json=zi.creators,
                    status="new",
                    cite_key=f"zot-{zi.key[:8]}",
                    abstract=zi.abstract,
                    tags_json=zi.tags,
                    metadata_source="zotero",
                    zotero_item_key=zi.key,
                    imported_at=now,
                    owner_user_id=owner_user_id,
                )
                session.add(paper)
                await session.flush()
                stats["papers_created"] += 1
            else:
                changed = False
                # Zotero 是元数据的上游：标题/作者/年份/DOI/摘要/标签以 Zotero 为准；
                # 不动解析产生的字段（status/versions/图等）。
                for attr, value in (
                    ("title", zi.title),
                    ("doi", zi.doi),
                    ("arxiv_id", zi.arxiv_id),
                    ("year", zi.year),
                    ("venue", zi.venue),
                    ("volume", zi.volume),
                    ("issue", zi.issue),
                    ("pages", zi.pages),
                    ("entry_type", zi.entry_type),
                    ("thesis_school", zi.thesis_school),
                    ("thesis_degree", zi.thesis_degree),
                    ("thesis_place", zi.thesis_place),
                    ("abstract", zi.abstract),
                ):
                    if value is not None and getattr(paper, attr) != value:
                        setattr(paper, attr, value)
                        changed = True
                if zi.creators:
                    paper.authors_json = zi.creators
                if zi.tags:
                    paper.tags_json = zi.tags
                if paper.title_norm != normalize_title(zi.title or ""):
                    paper.title_norm = normalize_title(zi.title or "")
                paper.zotero_item_key = zi.key
                paper.metadata_source = "zotero"
                paper.imported_at = now
                if changed:
                    stats["papers_updated"] += 1
            existing_papers[zi.key] = paper

            # ---- 集合关联：只同步 zotero 来源的集合，手动集合不动 ----
            current = {
                row.collection_id
                for row in (
                    await session.execute(
                        select(PaperCollection).where(PaperCollection.paper_id == paper.id)
                    )
                ).scalars()
            }
            wanted: set[str] = set()
            for ck in zi.collection_keys:
                col = key_to_col.get(ck)
                if col is None:
                    stats["skipped"] += 1
                    continue
                wanted.add(col.id)
                if col.id not in current:
                    session.add(PaperCollection(paper_id=paper.id, collection_id=col.id))
                    stats["links_added"] += 1
            # 删除「Zotero 集合里已经没了」的关联（手动集合保留）
            if current - wanted:
                zotero_col_ids = {c.id for c in key_to_col.values()}
                for rel in (
                    await session.execute(
                        select(PaperCollection).where(
                            PaperCollection.paper_id == paper.id,
                            PaperCollection.collection_id.in_(current - wanted),
                            PaperCollection.collection_id.in_(zotero_col_ids),
                        )
                    )
                ).scalars():
                    await session.delete(rel)
                    stats["links_removed"] += 1

            # ---- 附件 PDF：复制进本库并排队解析（B 方案，docs/15 §12.9） ----
            await _import_item_pdfs(session, paper, zi.attachments, stats)

        stats["duration_ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        run.finished_at = datetime.now(timezone.utc)
        run.stats_json = stats
        await session.commit()
        log.info("Zotero 导入完成：%s", stats)
        return stats
    except Exception as exc:  # noqa: BLE001 — 失败要落审计并给用户看原因
        await session.rollback()
        run = ImportRun(
            source=source,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            stats_json=stats,
            error=str(exc),
        )
        session.add(run)
        await session.commit()
        log.exception("Zotero 导入失败")
        raise


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _item_from_mapping(entry: dict, payload: dict) -> ZItem | None:
    key = str(entry.get("key") or payload.get("key") or "")
    if not key:
        return None
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    creators_raw = payload.get("creators") or []
    creators = [
        {k: c.get(k) for k in ("firstName", "lastName", "name") if c.get(k)}
        for c in creators_raw
        if isinstance(c, dict)
    ]
    tags_raw = payload.get("tags") or []
    tags = [str(t.get("tag")) for t in tags_raw if isinstance(t, dict) and t.get("tag")]
    collections = payload.get("collections") or []
    date = payload.get("date")
    item_type = str(payload.get("itemType") or "journalArticle")
    return ZItem(
        key=key,
        title=title,
        version=entry.get("version") or payload.get("version"),
        doi=(payload.get("DOI") or None),
        arxiv_id=_arxiv_from(
            {
                "archiveID": payload.get("archiveID"),
                "extra": payload.get("extra"),
                "url": payload.get("url"),
            }
        ),
        year=_year_from_date(date),
        venue=payload.get("publicationTitle") or payload.get("proceedingsTitle"),
        volume=_text_or_none(payload.get("volume")),
        issue=_text_or_none(payload.get("issue")),
        pages=_text_or_none(payload.get("pages")),
        abstract=payload.get("abstractNote"),
        tags=tags,
        creators=creators,
        collection_keys=[str(c) for c in collections],
        item_type=item_type,
        entry_type=zotero_entry_type(item_type),
        thesis_school=_text_or_none(payload.get("university")),
        thesis_degree=_text_or_none(payload.get("thesisType")),
        thesis_place=_text_or_none(payload.get("place")),
    )


def _parse_mapping(munged: dict) -> tuple[list[ZCollection], list[ZItem]]:
    # 与 load_json_source 共用同一解析逻辑，但直接吃内存对象
    collections_raw = munged.get("collections") or []
    items_raw = munged.get("items") or []
    collections: list[ZCollection] = []
    for entry in collections_raw:
        payload = entry.get("data") if isinstance(entry, dict) and "data" in entry else entry
        if not isinstance(payload, dict):
            continue
        key = str(entry.get("key") or payload.get("key") or "")
        name = str(payload.get("name") or "").strip()
        if not key or not name:
            continue
        parent = payload.get("parentCollection")
        collections.append(ZCollection(key=key, name=name, parent_key=str(parent) if parent else None))
    items: list[ZItem] = []
    for entry in items_raw:
        payload = entry.get("data") if isinstance(entry, dict) and "data" in entry else entry
        if not isinstance(payload, dict):
            continue
        if str(payload.get("itemType") or "") in ("attachment", "note", "annotation"):
            continue
        item = _item_from_mapping(entry, payload)
        if item is not None:
            items.append(item)
    return collections, items


def _text_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _year_from_date(raw: str | None) -> int | None:
    if not raw:
        return None
    m = re.search(r"(1[89]\d{2}|20\d{2})", str(raw))
    return int(m.group(1)) if m else None


def _arxiv_from(fields: dict) -> str | None:
    for value in (fields.get("archiveID"), fields.get("extra"), fields.get("url")):
        if not value:
            continue
        m = ARXIV_RE.search(str(value))
        if m:
            return m.group(1)
    return None


__all__ = [
    "ZCollection",
    "ZItem",
    "ZoteroImportError",
    "import_zotero",
    "load_api_source",
    "load_json_source",
    "load_sqlite_source",
    "load_source",
]
