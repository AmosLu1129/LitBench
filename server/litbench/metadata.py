# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crossref / arXiv 元数据补全（M8，2026-11-16）。

写作台里「补全卷期页」一直是手填；上传的 PDF 或早期导入经常只有标题/arXiv id，
缺作者、刊名、卷期页、DOI。这块从 Crossref（按 DOI）和 arXiv（按 id）查回著录字段，
先给 **diff**（当前值 / 建议值 / 是否冲突），用户确认后才写 `paper` 表。

守三条：

1. **只读外部 API，不写 Zotero**；查询本身不落库，apply 是显式动作；
2. 只搬运 Crossref/arXiv 返回的字段，**不猜**：查不到就 404，网络失败就 502；
3. 默认只填空白字段；要覆盖已有值必须显式 ``overwrite=true``（避免误改人工/ Zotero
   元数据）。
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from . import references as refs
from .config import settings
from .labels import normalize_title

# ---------------------------------------------------------------------------
# 字段定义
# ---------------------------------------------------------------------------
FIELD_LABELS: dict[str, str] = {
    "title": "标题",
    "authors_json": "作者",
    "year": "年份",
    "venue": "期刊 / 会议",
    "volume": "卷",
    "issue": "期",
    "pages": "页码",
    "doi": "DOI",
    "arxiv_id": "arXiv ID",
    "entry_type": "条目类型",
}
# UI 顺序（也是 diff 的顺序）
DIFF_FIELDS: tuple[str, ...] = (
    "title",
    "authors_json",
    "year",
    "venue",
    "volume",
    "issue",
    "pages",
    "doi",
    "arxiv_id",
    "entry_type",
)
ALLOWED_FIELDS = frozenset(FIELD_LABELS)

ENTRY_TYPE_LABELS = {
    "journal": "期刊",
    "conference": "会议",
    "preprint": "预印本",
    "thesis": "学位论文",
    "book": "图书",
    "report": "报告",
    "other": "其他",
}

# Crossref work type → paper.entry_type（认不出就不建议）
_CROSSREF_TYPES = {
    "journal-article": "journal",
    "proceedings-article": "conference",
    "posted-content": "preprint",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "book-chapter": "book",
    "reference-book": "book",
    "report": "report",
    "report-component": "report",
    "dissertation": "thesis",
    "dataset": "other",
}

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(
    r"^(?:arxiv:\s*)?([a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?$", re.IGNORECASE
)
_URL_PREFIX_RE = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE
)
_ARXIV_URL_RE = re.compile(
    r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"

# 外部响应上限：正常 Crossref 单条 ~几十 KB、搜索 5 条 ~几百 KB；超过 5MB 直接拒绝，
# 防止上游异常/恶意大响应把 API 内存打满。
MAX_METADATA_BYTES = 5 * 1024 * 1024


class MetadataError(RuntimeError):
    """可读的补全失败（默认映射 422）。"""


class MetadataNotFound(MetadataError):
    """DOI/arXiv 不存在，或搜索没有候选（404）。"""


class MetadataUnavailable(MetadataError):
    """外部 API 网络/服务失败（502）。"""


class MetadataConflict(MetadataError):
    """要覆盖已有字段但没给 overwrite（409）。"""


# ---------------------------------------------------------------------------
# 标识符清洗
# ---------------------------------------------------------------------------
def normalize_doi(raw: Any) -> str:
    value = _clean_text(raw)
    return _URL_PREFIX_RE.sub("", value).rstrip(".").strip()


def normalize_arxiv_id(raw: Any) -> str:
    value = _clean_text(raw)
    value = _ARXIV_URL_RE.sub("", value)
    value = re.sub(r"\.pdf$", "", value, flags=re.IGNORECASE)
    match = _ARXIV_NEW_RE.match(value) or _ARXIV_OLD_RE.match(value)
    if match:
        return match.group(1)
    return ""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _clean_text(value: Any) -> str:
    text = unescape(_HTML_TAG_RE.sub("", str(value or "")))
    return _WS_RE.sub(" ", text).strip()


def _first(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = _clean_text(item)
            if text:
                return text
        return ""
    return _clean_text(value)


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1800 <= number <= 2100 else None


def _pages(raw: Any) -> str:
    text = _clean_text(raw)
    return re.sub(r"(\d)-(\d)", r"\1–\2", text)


def _author_display(authors: list[dict[str, str]]) -> str:
    out: list[str] = []
    for author in authors:
        if author.get("name"):
            out.append(author["name"])
        else:
            family = author.get("lastName", "")
            given = author.get("firstName", "")
            out.append(f"{family}, {given}".strip(", ") if family else given)
    return "; ".join(item for item in out if item)


def _value_text(field: str, value: Any) -> str:
    if value is None:
        return ""
    if field == "authors_json":
        return _author_display(value if isinstance(value, list) else [])
    if field == "entry_type":
        return ENTRY_TYPE_LABELS.get(str(value), str(value))
    return str(value)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, dict)):
        return len(value) == 0 or (isinstance(value, str) and not value.strip())
    return False


def _same(field: str, left: Any, right: Any) -> bool:
    if field == "authors_json":
        def norm(items: Any) -> list[tuple[str, str, str]]:
            out = []
            for item in items or []:
                if isinstance(item, dict):
                    out.append(
                        (
                            _clean_text(item.get("lastName")),
                            _clean_text(item.get("firstName")),
                            _clean_text(item.get("name")),
                        )
                    )
            return out

        return norm(left) == norm(right)
    if field == "doi":
        return normalize_doi(left).lower() == normalize_doi(right).lower()
    if field == "year":
        return _int_or_none(left) == _int_or_none(right)
    return str(left or "").strip() == str(right or "").strip()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    agent = "LitBench/0.1"
    mailto = settings.crossref_mailto.strip()
    if mailto:
        agent += f" (mailto:{mailto})"
    return {"User-Agent": agent}


async def _get_json(url: str, params: dict[str, Any] | None = None) -> dict:
    try:
        async with httpx.AsyncClient(
            timeout=settings.metadata_timeout_s, follow_redirects=True
        ) as client:
            response = await client.get(url, params=params, headers=_headers())
    except httpx.TimeoutException as exc:
        raise MetadataUnavailable("Crossref 请求超时，稍后再试") from exc
    except httpx.HTTPError as exc:
        raise MetadataUnavailable(
            f"Crossref 请求失败：{type(exc).__name__}"
        ) from exc
    if response.status_code == 404:
        raise MetadataNotFound("Crossref 没有收录这个 DOI")
    if response.status_code >= 400:
        raise MetadataUnavailable(f"Crossref 返回 HTTP {response.status_code}")
    if len(response.content) > MAX_METADATA_BYTES:
        raise MetadataUnavailable("Crossref 响应过大，已拒绝解析")
    try:
        return response.json()
    except ValueError as exc:
        raise MetadataUnavailable("Crossref 返回的不是 JSON") from exc


async def _get_xml(url: str, params: dict[str, Any] | None = None) -> ElementTree.Element:
    try:
        async with httpx.AsyncClient(
            timeout=settings.metadata_timeout_s, follow_redirects=True
        ) as client:
            response = await client.get(url, params=params, headers=_headers())
    except httpx.TimeoutException as exc:
        raise MetadataUnavailable("arXiv 请求超时，稍后再试") from exc
    except httpx.HTTPError as exc:
        raise MetadataUnavailable(f"arXiv 请求失败：{type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise MetadataUnavailable(f"arXiv 返回 HTTP {response.status_code}")
    if len(response.content) > MAX_METADATA_BYTES:
        raise MetadataUnavailable("arXiv 响应过大，已拒绝解析")
    try:
        return ElementTree.fromstring(response.text)
    except ElementTree.ParseError as exc:
        raise MetadataUnavailable("arXiv 返回的 XML 无法解析") from exc


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------
def _crossref_year(item: dict) -> int | None:
    for key in ("published-print", "published-online", "issued", "published", "created"):
        date = item.get(key) or {}
        parts = date.get("date-parts") if isinstance(date, dict) else None
        if parts and isinstance(parts, list) and parts[0]:
            year = _int_or_none(parts[0][0])
            if year:
                return year
    return None


def _crossref_authors(item: dict) -> list[dict[str, str]]:
    authors: list[dict[str, str]] = []
    for raw in item.get("author") or []:
        if not isinstance(raw, dict):
            continue
        family = _clean_text(raw.get("family"))
        given = _clean_text(raw.get("given"))
        literal = _clean_text(raw.get("name"))
        if family:
            author = {"lastName": family}
            if given:
                author["firstName"] = given
            authors.append(author)
        elif literal:
            authors.append({"name": literal})
    return authors


def _crossref_fields(item: dict) -> dict[str, Any]:
    """Crossref work → paper 字段（只返回库里可能用到的著录字段）。"""
    fields: dict[str, Any] = {}
    title = _first(item.get("title"))
    if title:
        fields["title"] = title
    authors = _crossref_authors(item)
    if authors:
        fields["authors_json"] = authors
    year = _crossref_year(item)
    if year:
        fields["year"] = year
    venue = _first(item.get("container-title")) or _first(
        (item.get("event") or {}).get("name") if isinstance(item.get("event"), dict) else ""
    )
    if venue:
        fields["venue"] = venue
    volume = _clean_text(item.get("volume"))
    if volume:
        fields["volume"] = volume
    issue = _clean_text(item.get("issue"))
    if issue:
        fields["issue"] = issue
    pages = _pages(item.get("page"))
    if pages:
        fields["pages"] = pages
    doi = normalize_doi(item.get("DOI"))
    if doi:
        fields["doi"] = doi
    entry_type = _CROSSREF_TYPES.get(str(item.get("type") or "").lower())
    if entry_type:
        fields["entry_type"] = entry_type
    return fields


async def lookup_crossref(doi: str) -> dict[str, Any]:
    doi = normalize_doi(doi)
    if not _DOI_RE.match(doi):
        raise MetadataError(f"DOI 格式不对：{doi or '（空）'}")
    url = f"{settings.crossref_api_base.rstrip('/')}/works/{quote(doi, safe='/')}"
    payload = await _get_json(url)
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        raise MetadataUnavailable("Crossref 返回结构异常")
    fields = _crossref_fields(message)
    if not fields.get("title") and not fields.get("doi"):
        raise MetadataNotFound("Crossref 返回空条目")
    return fields


async def search_crossref(query: str, rows: int = 5) -> list[dict[str, Any]]:
    query = _clean_text(query)
    if len(query) < 4:
        raise MetadataError("搜索词太短（至少 4 个字符）")
    base = settings.crossref_api_base.rstrip("/")
    payload = await _get_json(
        f"{base}/works",
        params={"query.bibliographic": query, "rows": max(1, min(rows, 10))},
    )
    message = payload.get("message") if isinstance(payload, dict) else None
    items = message.get("items") if isinstance(message, dict) else None
    candidates: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        fields = _crossref_fields(item)
        title = fields.get("title")
        if not title:
            continue
        candidates.append(
            {
                "source": "crossref",
                "doi": fields.get("doi"),
                "arxiv_id": None,
                "title": title,
                "year": fields.get("year"),
                "venue": fields.get("venue"),
                "authors": _author_display(fields.get("authors_json") or []),
                "score": item.get("score"),
            }
        )
    return candidates


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------
async def lookup_arxiv(arxiv_id: str) -> dict[str, Any]:
    arxiv_id = normalize_arxiv_id(arxiv_id)
    if not arxiv_id:
        raise MetadataError(f"arXiv ID 格式不对：{arxiv_id or '（空）'}")
    root = await _get_xml(
        settings.arxiv_api_base, params={"id_list": arxiv_id}
    )
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        raise MetadataNotFound("arXiv 没找到这个 ID")
    title = _clean_text(entry.findtext(f"{_ATOM}title") or "")
    if not title:
        raise MetadataNotFound("arXiv 条目没有标题")
    fields: dict[str, Any] = {
        "title": title,
        "entry_type": "preprint",
        "arxiv_id": arxiv_id,
    }
    authors: list[dict[str, str]] = []
    for node in entry.findall(f"{_ATOM}author"):
        name = _clean_text(node.findtext(f"{_ATOM}name") or "")
        if not name:
            continue
        family, given = refs._split_name(name)
        author = {"lastName": family or name}
        if given:
            author["firstName"] = given
        authors.append(author)
    if authors:
        fields["authors_json"] = authors
    published = _clean_text(entry.findtext(f"{_ATOM}published") or "")
    if len(published) >= 4:
        year = _int_or_none(published[:4])
        if year:
            fields["year"] = year
    journal_ref = _clean_text(entry.findtext(f"{_ARXIV_NS}journal_ref") or "")
    fields["venue"] = journal_ref or "arXiv"
    doi = normalize_doi(entry.findtext(f"{_ARXIV_NS}doi") or "")
    if doi:
        fields["doi"] = doi
    return fields


# ---------------------------------------------------------------------------
# diff / lookup 编排
# ---------------------------------------------------------------------------
def build_field_diff(paper: Any, fetched: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field in DIFF_FIELDS:
        suggested = fetched.get(field)
        if _is_empty(suggested):
            continue
        current = getattr(paper, field, None)
        if _same(field, current, suggested):
            continue
        empty = _is_empty(current)
        rows.append(
            {
                "field": field,
                "label": FIELD_LABELS[field],
                "current": current,
                "current_text": _value_text(field, current),
                "suggested": suggested,
                "suggested_text": _value_text(field, suggested),
                "empty": empty,
                "conflict": not empty,
            }
        )
    return rows


async def lookup_for_paper(
    paper: Any,
    *,
    mode: str = "auto",
    doi: str | None = None,
    arxiv_id: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """按 auto/doi/arxiv/search 查 Crossref/arXiv，返回字段 diff 或搜索候选。"""
    mode = (mode or "auto").strip().lower()
    if mode == "search":
        search_query = _clean_text(query or getattr(paper, "title", "") or "")
        if not search_query:
            raise MetadataError("没有可搜索的题名；请填一个查询词")
        candidates = await search_crossref(search_query)
        return {
            "paper_id": str(getattr(paper, "id", "")),
            "mode": "search",
            "source": "crossref",
            "identifier": "",
            "fields": [],
            "candidates": candidates,
            "warnings": [] if candidates else ["Crossref 没搜到候选"],
        }

    target_doi = normalize_doi(doi) if doi else ""
    target_arxiv = normalize_arxiv_id(arxiv_id) if arxiv_id else ""
    if mode == "doi" and target_doi:
        fetched = await lookup_crossref(target_doi)
        return {
            "paper_id": str(getattr(paper, "id", "")),
            "mode": "doi",
            "source": "crossref",
            "identifier": target_doi,
            "fields": build_field_diff(paper, fetched),
            "candidates": [],
            "warnings": [],
        }
    if mode == "arxiv" and target_arxiv:
        fetched = await lookup_arxiv(target_arxiv)
        return {
            "paper_id": str(getattr(paper, "id", "")),
            "mode": "arxiv",
            "source": "arxiv",
            "identifier": target_arxiv,
            "fields": build_field_diff(paper, fetched),
            "candidates": [],
            "warnings": [],
        }
    if mode in ("doi", "arxiv"):
        raise MetadataError("指定了模式但没给对应的 DOI / arXiv ID")

    # auto：显式参数 > paper 上已有标识
    if target_doi or getattr(paper, "doi", None):
        identifier = target_doi or normalize_doi(getattr(paper, "doi", ""))
        fetched = await lookup_crossref(identifier)
        return {
            "paper_id": str(getattr(paper, "id", "")),
            "mode": "auto",
            "source": "crossref",
            "identifier": identifier,
            "fields": build_field_diff(paper, fetched),
            "candidates": [],
            "warnings": [],
        }
    if target_arxiv or getattr(paper, "arxiv_id", None):
        identifier = target_arxiv or normalize_arxiv_id(getattr(paper, "arxiv_id", ""))
        fetched = await lookup_arxiv(identifier)
        return {
            "paper_id": str(getattr(paper, "id", "")),
            "mode": "auto",
            "source": "arxiv",
            "identifier": identifier,
            "fields": build_field_diff(paper, fetched),
            "candidates": [],
            "warnings": [],
        }
    raise MetadataError(
        "这篇文献没有 DOI / arXiv ID；可以先用「按题名搜索」找候选，或在论文 PDF 里补 DOI"
    )


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def _validate_value(field: str, value: Any) -> Any:
    if field == "title":
        text = _clean_text(value)
        if not 1 <= len(text) <= 500:
            raise MetadataError("标题不能为空且不超过 500 字")
        return text
    if field == "authors_json":
        if not isinstance(value, list) or not 1 <= len(value) <= 200:
            raise MetadataError("作者必须是 1–200 人的列表")
        authors: list[dict[str, str]] = []
        for raw in value:
            if not isinstance(raw, dict):
                raise MetadataError("作者项必须是对象")
            author = {}
            for key in ("lastName", "firstName", "name"):
                text = _clean_text(raw.get(key))
                if not text:
                    continue
                if len(text) > 200:
                    raise MetadataError("作者姓名过长（最多 200 字）")
                author[key] = text
            if not author:
                raise MetadataError("作者项至少要有一个姓名")
            authors.append(author)
        return authors
    if field == "year":
        year = _int_or_none(value)
        if year is None:
            raise MetadataError("年份必须在 1800–2100 之间")
        return year
    if field == "doi":
        doi = normalize_doi(value)
        if not _DOI_RE.match(doi):
            raise MetadataError(f"DOI 格式不对：{doi or '（空）'}")
        return doi
    if field == "arxiv_id":
        arxiv = normalize_arxiv_id(value)
        if not arxiv:
            raise MetadataError(f"arXiv ID 格式不对：{_clean_text(value) or '（空）'}")
        return arxiv
    if field == "entry_type":
        entry_type = _clean_text(value).lower()
        if entry_type not in refs.ENTRY_TYPES:
            raise MetadataError(f"entry_type 不支持：{entry_type}")
        return entry_type
    limits = {"venue": 255, "volume": 32, "issue": 32, "pages": 64}
    text = _clean_text(value)
    if len(text) > limits[field]:
        raise MetadataError(f"{FIELD_LABELS[field]}过长（上限 {limits[field]} 字）")
    return text


def apply_metadata(paper: Any, fields: dict[str, Any], *, overwrite: bool = False) -> list[str]:
    """把确认过的字段写进 Paper 对象（不 commit）；返回实际改动的字段名。"""
    unknown = [field for field in fields if field not in ALLOWED_FIELDS]
    if unknown:
        raise MetadataError(f"不允许补全的字段：{', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for field, raw in fields.items():
        if raw is None:
            continue
        if isinstance(raw, str) and not raw.strip():
            continue
        if isinstance(raw, list) and not raw:
            continue
        cleaned[field] = _validate_value(field, raw)
    if not cleaned:
        raise MetadataError("没有要写入的字段")
    conflicts = [
        FIELD_LABELS[field]
        for field, value in cleaned.items()
        if not _is_empty(getattr(paper, field, None))
        and not _same(field, getattr(paper, field, None), value)
    ]
    if conflicts and not overwrite:
        raise MetadataConflict(
            "这些字段已有值，勾选「覆盖已有值」后再试：" + "、".join(conflicts)
        )
    for field, value in cleaned.items():
        setattr(paper, field, value)
    if "title" in cleaned:
        paper.title_norm = normalize_title(cleaned["title"])
    return sorted(cleaned)


__all__ = [
    "ALLOWED_FIELDS",
    "DIFF_FIELDS",
    "ENTRY_TYPE_LABELS",
    "FIELD_LABELS",
    "MetadataConflict",
    "MetadataError",
    "MetadataNotFound",
    "MetadataUnavailable",
    "apply_metadata",
    "build_field_diff",
    "lookup_arxiv",
    "lookup_crossref",
    "lookup_for_paper",
    "normalize_arxiv_id",
    "normalize_doi",
    "search_crossref",
]
