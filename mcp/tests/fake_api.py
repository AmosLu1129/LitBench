# SPDX-License-Identifier: MIT
"""测试用的假 LitBench API（``httpx.MockTransport``）。

单测不依赖 8000 端口与真实数据：这里按 docs/09 的 API 契约返回最小的可控数据。
故意实现成「和真实 API 同样的行为」（q 只搜标题/cite_key、`/api/figures?paper=` 能解析
DOI/arXiv/cite_key、404 语义等），这样能覆盖工具的解析/兜底逻辑。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

# ---- 固定 ID（32 位 hex，和真实 paper/figure/summary id 同形） -------------
PAPER_ATTENTION = "a" * 32
PAPER_COAL = "b" * 32
PAPER_METADATA_ONLY = "c" * 32
PAPER_FALLBACK = "d" * 32

VERSION_ATTENTION = "a1" * 16
VERSION_COAL = "b1" * 16
VERSION_FALLBACK = "d1" * 16

FIG_ATTENTION_1 = "f1" * 16
FIG_ATTENTION_2 = "f2" * 16
FIG_COAL_1 = "f3" * 16
FIG_FALLBACK_1 = "f4" * 16

TABLE_ATTENTION_1 = "t1" * 16

SUMMARY_ATTENTION_WHOLE = "90" * 16
SUMMARY_ATTENTION_SECTION = "80" * 16

BLOCK_INTRO = "11" * 16
BLOCK_ATTN = "22" * 16
BLOCK_TRAIN = "33" * 16
BLOCK_LONG = "44" * 16
BLOCK_COAL = "55" * 16
BLOCK_FALLBACK = "66" * 16

# M5：提案/任务的固定测试 id
PROPOSAL_ID = "aa" * 16
TASK_QUEUED = "ab" * 16
TASK_RUNNING = "ac" * 16
TASK_FAILED = "ad" * 16

COLLECTION_NLP = "c1" * 16
COLLECTION_TRANSFORMERS = "c2" * 16
COLLECTION_CHEM = "c3" * 16

INTRO_TEXT = (
    "The Transformer uses self attention to dispense with recurrence and "
    "convolutions entirely, allowing much more parallelization during training."
)
ATTN_TEXT = (
    "Scaled dot product attention computes compatibility scores between queries "
    "and keys and normalizes them with a softmax function."
)
TRAIN_TEXT = "We train on the WMT 2014 English German translation task with 8 GPUs."
LONG_TEXT = "This paragraph is intentionally long to exercise truncation behaviour. " * 60
COAL_TEXT = (
    "Coal pyrolysis kinetics were measured by thermogravimetric analysis at "
    "heating rates of 10, 20 and 40 K per minute."
)
FALLBACK_TEXT = "Softmax scaling improves the numerical stability of attention weights across layers."

_LABEL_RE = re.compile(
    r"(?:figure|fig|table|tab|图|表)\s*[.．:：]?\s*(\d{1,3}(?:\s*[-–—.．]\s*\d{1,3})*)",
    re.IGNORECASE,
)


def normalize_label(text: str) -> str | None:
    match = _LABEL_RE.search(text or "")
    if match is None:
        return None
    parts = re.findall(r"\d{1,3}", match.group(1))
    return "fig:" + "-".join(parts)


def normalize_table_label(text: str) -> str | None:
    match = _LABEL_RE.search(text or "")
    if match is None:
        return None
    parts = re.findall(r"\d{1,3}", match.group(1))
    return "tab:" + "-".join(parts)


def _paper(
    paper_id: str,
    title: str,
    *,
    cite_key: str | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
    year: int | None = None,
    venue: str | None = None,
    favorite: bool = False,
    figure_count: int = 0,
    table_count: int = 0,
    has_version: bool = True,
    created_at: str = "2026-11-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "id": paper_id,
        "title": title,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "cite_key": cite_key,
        "year": year,
        "venue": venue,
        "status": "parsed",
        "figure_count": figure_count,
        "table_count": table_count,
        "created_at": created_at,
        "abstract": None,
        "tags_json": [],
        "reading_status": "unread",
        "metadata_source": "upload",
        "zotero_item_key": None,
        "has_version": has_version,
        "note": None,
        "favorite": favorite,
        "collection_ids": [],
    }


def _figure(
    figure_id: str,
    paper_id: str,
    *,
    paper_title: str,
    year: int | None,
    version_id: str,
    page: int,
    label_raw: str,
    label_norm: str,
    caption: str,
    bbox: list[float],
) -> dict[str, Any]:
    return {
        "figure_id": figure_id,
        "cite_key": f"{paper_id[:6]}-v1-{label_norm.replace(':', '')}",
        "paper_id": paper_id,
        "paper_title": paper_title,
        "year": year,
        "version_id": version_id,
        "version_kind": "published",
        "page": page,
        "label_raw": label_raw,
        "label_norm": label_norm,
        "sublabel": None,
        "caption": caption,
        "figure_type": "diagram",
        "license_hint": "unknown",
        "bbox": bbox,
        "image_url": None,
        "label_source": "text",
        "label_confidence": 1.0,
        "group_key": None,
        "origin": "parser",
        "review_status": "pending",
        "review_note": None,
        "bbox_override": None,
    }


class FakeLibrary:
    """一组可以被工具读到的假数据。测试可自由增删。"""

    def __init__(self) -> None:
        self.papers: list[dict[str, Any]] = []
        self.texts: dict[str, dict[str, Any]] = {}
        self.figures: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.summaries: dict[str, dict[str, Any]] = {}
        # paper_id -> 该文献的（章节）摘要 id 列表；顺序即 API 返回顺序
        self.paper_summaries: dict[str, list[str]] = {}
        # paper_id -> 整篇速览 summary id
        self.paper_whole_summary: dict[str, str] = {}
        self.collections: list[dict[str, Any]] = []
        self.collection_members: dict[str, set[str]] = {}
        # paper_id -> 模型复核响应（M9.4）；为空时按「未配置模型」返回 503，
        # 让 cite.resolve(review=true) 走词面回退分支。
        self.citation_reviews: dict[str, dict[str, Any]] = {}
        self.golden: dict[str, Any] = {
            "schema": "litbench.golden/v1",
            "exported_at": "2026-11-13T00:00:00+00:00",
            "include_pending": False,
            "item_count": 0,
            "items": [],
        }
        self.golden_bboxes: dict[str, Any] = {
            "schema": "litbench.golden.bboxes/1",
            "exported_at": "2026-11-13T00:00:00+00:00",
            "include_pending": False,
            "item_count": 0,
            "skipped_no_bbox": 0,
            "items": [],
        }


def default_library() -> FakeLibrary:
    """覆盖 8 个工具的正常/空/降级场景的默认数据。"""
    lib = FakeLibrary()
    lib.papers = [
        _paper(
            PAPER_COAL,
            "Coal pyrolysis kinetics",
            cite_key="wang2019coal",
            doi="10.1016/j.coal.2019.01.001",
            year=2019,
            venue="Fuel",
            figure_count=1,
            created_at="2026-11-02T00:00:00Z",
        ),
        _paper(
            PAPER_ATTENTION,
            "Attention Is All You Need",
            cite_key="vaswani2017attention",
            doi="10.5555/attention.2017",
            arxiv_id="1706.03762",
            year=2017,
            venue="NeurIPS",
            figure_count=2,
            table_count=1,
            created_at="2026-11-03T00:00:00Z",
        ),
        _paper(
            PAPER_METADATA_ONLY,
            "Metadata only paper",
            cite_key="meta2020",
            year=2020,
            has_version=False,
            created_at="2026-11-04T00:00:00Z",
        ),
        _paper(
            PAPER_FALLBACK,
            "Numerical stability notes",
            cite_key="note2018",
            year=2018,
            figure_count=1,
            created_at="2026-11-05T00:00:00Z",
        ),
    ]

    lib.texts[PAPER_ATTENTION] = {
        "paper_id": PAPER_ATTENTION,
        "version_id": VERSION_ATTENTION,
        "page_count": 2,
        "block_count": 4,
        "sections": [
            {
                "key": "sec-1",
                "title": "1 Introduction",
                "level": 1,
                "kind": "heading",
                "page_start": 1,
                "page_end": 1,
                "blocks": [
                    {
                        "block_id": BLOCK_INTRO,
                        "page": 1,
                        "type": "text",
                        "text": INTRO_TEXT,
                        "bbox": [10.0, 20.0, 300.0, 40.0],
                    },
                    {
                        "block_id": BLOCK_ATTN,
                        "page": 1,
                        "type": "text",
                        "text": ATTN_TEXT,
                        "bbox": [10.0, 60.0, 300.0, 80.0],
                    },
                ],
            },
            {
                "key": "sec-2",
                "title": "3 Results",
                "level": 1,
                "kind": "heading",
                "page_start": 2,
                "page_end": 2,
                "blocks": [
                    {
                        "block_id": BLOCK_TRAIN,
                        "page": 2,
                        "type": "text",
                        "text": TRAIN_TEXT,
                        "bbox": [10.0, 20.0, 300.0, 40.0],
                    },
                    {
                        "block_id": BLOCK_LONG,
                        "page": 2,
                        "type": "text",
                        "text": LONG_TEXT,
                        "bbox": [10.0, 60.0, 300.0, 80.0],
                    },
                ],
            },
        ],
    }
    lib.texts[PAPER_COAL] = {
        "paper_id": PAPER_COAL,
        "version_id": VERSION_COAL,
        "page_count": 1,
        "block_count": 1,
        "sections": [
            {
                "key": "sec-1",
                "title": "2 Experiments",
                "level": 1,
                "kind": "heading",
                "page_start": 1,
                "page_end": 1,
                "blocks": [
                    {
                        "block_id": BLOCK_COAL,
                        "page": 1,
                        "type": "text",
                        "text": COAL_TEXT,
                        "bbox": [10.0, 20.0, 300.0, 40.0],
                    }
                ],
            }
        ],
    }
    lib.texts[PAPER_FALLBACK] = {
        "paper_id": PAPER_FALLBACK,
        "version_id": VERSION_FALLBACK,
        "page_count": 1,
        "block_count": 1,
        "sections": [
            {
                "key": "sec-1",
                "title": "Notes",
                "level": 1,
                "kind": "heading",
                "page_start": 1,
                "page_end": 1,
                "blocks": [
                    {
                        "block_id": BLOCK_FALLBACK,
                        "page": 1,
                        "type": "text",
                        "text": FALLBACK_TEXT,
                        "bbox": [10.0, 20.0, 300.0, 40.0],
                    }
                ],
            }
        ],
    }

    lib.figures = [
        _figure(
            FIG_ATTENTION_1,
            PAPER_ATTENTION,
            paper_title="Attention Is All You Need",
            year=2017,
            version_id=VERSION_ATTENTION,
            page=1,
            label_raw="Figure 1",
            label_norm="fig:1",
            caption="Transformer architecture: encoder and decoder stacks",
            bbox=[50.0, 100.0, 250.0, 180.0],
        ),
        _figure(
            FIG_ATTENTION_2,
            PAPER_ATTENTION,
            paper_title="Attention Is All You Need",
            year=2017,
            version_id=VERSION_ATTENTION,
            page=2,
            label_raw="Figure 2",
            label_norm="fig:2",
            caption="Multi head attention heads",
            bbox=[50.0, 100.0, 250.0, 180.0],
        ),
        _figure(
            FIG_COAL_1,
            PAPER_COAL,
            paper_title="Coal pyrolysis kinetics",
            year=2019,
            version_id=VERSION_COAL,
            page=1,
            label_raw="图 1-1",
            label_norm="fig:1-1",
            caption="TG curves of coal pyrolysis",
            bbox=[50.0, 100.0, 250.0, 180.0],
        ),
        _figure(
            FIG_FALLBACK_1,
            PAPER_FALLBACK,
            paper_title="Numerical stability notes",
            year=2018,
            version_id=VERSION_FALLBACK,
            page=1,
            label_raw="Figure 1",
            label_norm="fig:1",
            caption="Softmax scaling of attention weights across layers",
            bbox=[50.0, 100.0, 250.0, 180.0],
        ),
    ]

    lib.tables = [
        {
            "table_id": TABLE_ATTENTION_1,
            "paper_id": PAPER_ATTENTION,
            "version_id": VERSION_ATTENTION,
            "page": 4,
            "label_raw": "Table 1",
            "label_norm": "tab:1",
            "caption": "BLEU scores on WMT 2014",
            "quality": "structured",
            "html": "<table><tr><td>BLEU</td><td>28.4</td></tr></table>",
        }
    ]

    whole_summary = {
        "summary_id": SUMMARY_ATTENTION_WHOLE,
        "paper_version_id": VERSION_ATTENTION,
        "section_key": "__paper__",
        "section_title": "整篇速览",
        "level": 0,
        "content": "本文提出 Transformer，用 self attention 替代循环与卷积，训练更可并行。",
        "key_points": ["self-attention", "parallel training"],
        "evidence_block_ids": [BLOCK_INTRO, BLOCK_ATTN],
        "source": "agent",
        "provider": "fake",
        "model": "fake-model",
        "prompt_version": "p1",
        "status": "published",
        "auto_accepted": True,
        "human_locked": False,
        "tokens_in": 10,
        "tokens_out": 20,
        "cost": 0.0,
        "confidence": 0.95,
        "created_at": "2026-11-13T00:00:00Z",
        "updated_at": "2026-11-13T00:00:00Z",
        "edited_by": None,
        "edited_at": None,
        "evidence": [
            {
                "block_id": BLOCK_INTRO,
                "page": 1,
                "type": "text",
                "text": INTRO_TEXT,
                "bbox": [10.0, 20.0, 300.0, 40.0],
            },
            {
                "block_id": BLOCK_ATTN,
                "page": 1,
                "type": "text",
                "text": ATTN_TEXT,
                "bbox": [10.0, 60.0, 300.0, 80.0],
            },
        ],
    }
    section_summary = {
        "summary_id": SUMMARY_ATTENTION_SECTION,
        "paper_version_id": VERSION_ATTENTION,
        "section_key": "sec-1",
        "section_title": "1 Introduction",
        "level": 1,
        "content": "引言速览：Transformer 完全基于注意力机制。",
        "key_points": ["no recurrence"],
        "evidence_block_ids": [BLOCK_INTRO],
        "source": "agent",
        "provider": "fake",
        "model": "fake-model",
        "prompt_version": "p1",
        "status": "published",
        "auto_accepted": True,
        "human_locked": False,
        "tokens_in": 5,
        "tokens_out": 6,
        "cost": 0.0,
        "confidence": 0.9,
        "created_at": "2026-11-13T00:00:00Z",
        "updated_at": "2026-11-13T00:00:00Z",
        "edited_by": None,
        "edited_at": None,
        "evidence": [
            {
                "block_id": BLOCK_INTRO,
                "page": 1,
                "type": "text",
                "text": INTRO_TEXT,
                "bbox": [10.0, 20.0, 300.0, 40.0],
            }
        ],
    }
    lib.summaries = {
        SUMMARY_ATTENTION_WHOLE: whole_summary,
        SUMMARY_ATTENTION_SECTION: section_summary,
    }
    lib.paper_summaries[PAPER_ATTENTION] = [
        SUMMARY_ATTENTION_WHOLE,
        SUMMARY_ATTENTION_SECTION,
    ]
    lib.paper_whole_summary[PAPER_ATTENTION] = SUMMARY_ATTENTION_WHOLE

    lib.collections = [
        {
            "id": COLLECTION_NLP,
            "name": "NLP",
            "parent_id": None,
            "sort_order": 0,
            "source": "manual",
            "zotero_key": None,
            "paper_count": 1,
        },
        {
            "id": COLLECTION_TRANSFORMERS,
            "name": "Transformers",
            "parent_id": COLLECTION_NLP,
            "sort_order": 1,
            "source": "manual",
            "zotero_key": None,
            "paper_count": 1,
        },
        {
            "id": COLLECTION_CHEM,
            "name": "化工",
            "parent_id": None,
            "sort_order": 2,
            "source": "manual",
            "zotero_key": None,
            "paper_count": 1,
        },
    ]
    lib.collection_members = {
        COLLECTION_NLP: {PAPER_ATTENTION},
        COLLECTION_TRANSFORMERS: {PAPER_ATTENTION},
        COLLECTION_CHEM: {PAPER_COAL},
    }
    lib.golden = {
        "schema": "litbench.golden/v1",
        "exported_at": "2026-11-13T00:00:00+00:00",
        "include_pending": False,
        "item_count": 1,
        "items": [
            {
                "item_id": FIG_ATTENTION_1,
                "paper_id": PAPER_ATTENTION,
                "paper_title": "Attention Is All You Need",
                "cite_key": "vaswani-v1-fig1",
                "label_norm": "fig:1",
                "caption": "Transformer architecture: encoder and decoder stacks",
                "bbox": [50.0, 100.0, 250.0, 180.0],
                "review_status": "ok",
            }
        ],
    }
    return lib


class FakeAPI:
    """把 :class:`FakeLibrary` 暴露成 httpx MockTransport。"""

    def __init__(self, library: FakeLibrary) -> None:
        self.library = library
        # (method, path) -> HTTP status，用于注入上游故障
        self.failures: dict[tuple[str, str], int] = {}
        self.calls: list[dict[str, Any]] = []
        # M5 写侧状态：提案 / 任务（内存，测试间各自新建 FakeAPI）
        self.proposals: list[dict[str, Any]] = [
            {
                "proposal_id": PROPOSAL_ID,
                "figure_id": FIG_ATTENTION_1,
                "paper_version_id": VERSION_ATTENTION,
                "kind": "figure_review",
                "proposed": {
                    "needs_change": True,
                    "suggested_label": "Figure 1",
                    "cleaned_caption": "",
                },
                "evidence": [{"block_id": BLOCK_ATTN, "page": 1, "text": ATTN_TEXT}],
                "confidence": 0.96,
                "status": "pending",
                "provider": "external",
                "model": "dsh-agent",
                "created_at": "2026-11-13T00:00:00Z",
            }
        ]
        self.tasks: list[dict[str, Any]] = [
            {
                "id": TASK_QUEUED,
                "kind": "figure_review",
                "target_type": "figure",
                "target_id": FIG_ATTENTION_1,
                "payload": {"figure_id": FIG_ATTENTION_1},
                "status": "queued",
                "priority": 0,
                "attempts": 0,
                "max_attempts": 3,
                "actor": None,
                "lease_until": None,
                "result": None,
                "error": None,
                "cost": 0.0,
                "created_at": "2026-11-13T00:00:00Z",
            },
            {
                "id": TASK_FAILED,
                "kind": "figure_review",
                "target_type": "figure",
                "target_id": FIG_ATTENTION_1,
                "payload": {"figure_id": FIG_ATTENTION_1},
                "status": "failed",
                "priority": 0,
                "attempts": 3,
                "max_attempts": 3,
                "actor": "dsh-agent",
                "lease_until": None,
                "result": None,
                "error": "timeout",
                "cost": 0.0,
                "created_at": "2026-11-13T00:00:00Z",
            },
        ]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- 路由 ---------------------------------------------------------
    async def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append({"method": method, "path": path, "params": params})
        failure = self.failures.get((method, path))
        if failure is not None:
            return _json({"detail": "fake upstream failure"}, status=failure)

        parts = [part for part in path.split("/") if part]
        if parts[:2] == ["api", "papers"]:
            return self._papers(parts[2:], params)
        if parts[:2] == ["api", "figures"]:
            return self._figures(parts[2:], params)
        if parts[:2] == ["api", "summaries"]:
            return self._summaries(parts[2:])
        if parts[:2] == ["api", "collections"]:
            return _json(self.library.collections)
        if parts[:2] == ["api", "dataset"] and parts[2:] == ["golden"]:
            return _json(self._golden(params))
        if parts[:2] == ["api", "citations"] and parts[2:] == ["review"]:
            raw = request.content
            body = json.loads(raw) if raw else {}
            return self._citation_review(body)
        if parts[:2] == ["api", "proposals"]:
            body: dict[str, Any] = {}
            if method == "POST":
                raw = request.content
                body = json.loads(raw) if raw else {}
            return self._proposals(rest=parts[2:], params=params, method=method, body=body)
        if parts[:2] == ["api", "tasks"]:
            body = {}
            if method == "POST":
                raw = request.content
                body = json.loads(raw) if raw else {}
            return self._tasks(rest=parts[2:], params=params, method=method, body=body)
        return _json({"detail": "not found"}, status=404)

    def _papers(self, rest: list[str], params: dict[str, str]) -> httpx.Response:
        lib = self.library
        if not rest:
            return _json(self._list_papers(params))
        paper_id = rest[0]
        paper = next((item for item in lib.papers if item["id"] == paper_id), None)
        if paper is None:
            return _json({"detail": "paper not found"}, status=404)
        if len(rest) == 1:
            return _json(paper)
        action = rest[1]
        if action == "text":
            payload = lib.texts.get(paper_id)
            if payload is None:
                return _json({"detail": "这篇文献还没有解析版本"}, status=404)
            return _json(payload)
        if action == "figures":
            return _json([fig for fig in lib.figures if fig["paper_id"] == paper_id])
        if action == "tables":
            return _json([table for table in lib.tables if table["paper_id"] == paper_id])
        if action == "summaries":
            ids = lib.paper_summaries.get(paper_id, [])
            return _json(
                [
                    {key: lib.summaries[summary_id].get(key) for key in (
                        "summary_id", "paper_version_id", "section_key", "section_title",
                        "level", "content", "key_points", "source", "provider", "model",
                        "prompt_version", "status", "auto_accepted", "human_locked",
                        "confidence", "created_at", "updated_at", "edited_by", "edited_at",
                    )}
                    for summary_id in ids
                ]
            )
        if action == "summary":
            summary_id = lib.paper_whole_summary.get(paper_id)
            if summary_id is None:
                return _json({"detail": "这篇文献还没有整篇速览"}, status=404)
            return _json(lib.summaries[summary_id])
        return _json({"detail": "not found"}, status=404)

    def _figures(self, rest: list[str], params: dict[str, str]) -> httpx.Response:
        lib = self.library
        if rest:
            figure = next((item for item in lib.figures if item["figure_id"] == rest[0]), None)
            if figure is None:
                return _json({"detail": "figure not found"}, status=404)
            return _json(figure)
        return _json(self._search_figures(params))

    def _summaries(self, rest: list[str]) -> httpx.Response:
        if not rest:
            return _json({"detail": "not found"}, status=404)
        summary = self.library.summaries.get(rest[0])
        if summary is None:
            return _json({"detail": "summary not found"}, status=404)
        return _json(summary)

    # -- M5：提案 / 任务 -------------------------------------------------
    def _proposals(
        self,
        *,
        rest: list[str],
        params: dict[str, str],
        method: str,
        body: dict[str, Any],
    ) -> httpx.Response:
        if method == "POST" and not rest:
            evidence = body.get("evidence_block_ids") or []
            if not evidence:
                return _json(
                    {"detail": "提案必须带证据 block_id（至少 1 条）"}, status=422
                )
            proposal = {
                "proposal_id": PROPOSAL_ID,
                "figure_id": body.get("figure_id"),
                "paper_version_id": VERSION_ATTENTION,
                "kind": body.get("kind") or "figure_review",
                "proposed": body.get("proposed") or {},
                "evidence": [{"block_id": eid} for eid in evidence],
                "confidence": float(body.get("confidence") or 0.0),
                "status": "pending",
                "provider": "external",
                "model": body.get("actor") or "stdio",
                "created_at": "2026-11-13T00:00:00Z",
            }
            self.proposals.insert(0, proposal)
            return _json(proposal, status=201)
        if rest:
            match = next(
                (item for item in self.proposals if item["proposal_id"] == rest[0]), None
            )
            if match is None:
                return _json({"detail": "proposal not found"}, status=404)
            return _json(match)
        status = (params.get("status") or "").strip()
        items = list(self.proposals)
        if status and status != "all":
            items = [item for item in items if item["status"] == status]
        paper_id = params.get("paper_id")
        if paper_id:
            allowed = {
                figure["figure_id"]
                for figure in self.library.figures
                if figure["paper_id"] == paper_id
            }
            items = [item for item in items if item["figure_id"] in allowed]
        return _json(_paginate(items, params))

    def _tasks(
        self,
        *,
        rest: list[str],
        params: dict[str, str],
        method: str,
        body: dict[str, Any],
    ) -> httpx.Response:
        if method == "POST" and not rest:
            task = {
                "id": TASK_RUNNING,
                "kind": body.get("kind") or "figure_review",
                "target_type": body.get("target_type") or "figure",
                "target_id": body.get("target_id") or "",
                "payload": body.get("payload") or {},
                "status": "queued",
                "priority": 0,
                "attempts": 0,
                "max_attempts": 3,
                "actor": None,
                "lease_until": None,
                "result": None,
                "error": None,
                "cost": 0.0,
                "created_at": "2026-11-13T00:00:00Z",
            }
            self.tasks.append(task)
            return _json(task, status=201)
        if not rest:
            items = list(self.tasks)
            status = (params.get("status") or "").strip()
            if status:
                items = [item for item in items if item["status"] == status]
            kind = (params.get("kind") or "").strip()
            if kind:
                items = [item for item in items if item["kind"] == kind]
            return _json(_paginate(items, params))
        task = next((item for item in self.tasks if item["id"] == rest[0]), None)
        if task is None:
            return _json({"detail": "task not found"}, status=404)
        if len(rest) == 1:
            return _json(task)
        action = rest[1]
        actor = body.get("actor") or "stdio"
        if action == "claim":
            if task["status"] != "queued":
                return _json({"detail": "任务不在排队中"}, status=409)
            task.update(
                status="running",
                actor=actor,
                attempts=int(task.get("attempts") or 0) + 1,
                lease_until="2026-11-13T00:05:00Z",
            )
            return _json(task)
        if action == "heartbeat":
            if task["status"] != "running" or task.get("actor") != actor:
                return _json({"detail": "任务不在运行中"}, status=409)
            task["lease_until"] = "2026-11-13T00:10:00Z"
            return _json(task)
        if action == "complete":
            if task["status"] != "running" or task.get("actor") != actor:
                return _json({"detail": "任务不在运行中"}, status=409)
            task.update(
                status="done",
                result=body.get("result") or {},
                cost=float(body.get("cost") or 0.0),
                lease_until=None,
            )
            return _json(task)
        if action == "fail":
            if task["status"] != "running" or task.get("actor") != actor:
                return _json({"detail": "任务不在运行中"}, status=409)
            task["error"] = body.get("error") or ""
            task["status"] = "queued" if body.get("retry", True) else "failed"
            task["actor"] = None if task["status"] == "queued" else actor
            task["lease_until"] = None
            return _json(task)
        if action == "retry":
            if task["status"] not in ("failed", "canceled"):
                return _json({"detail": "只有 failed / canceled 的任务能重试"}, status=409)
            task.update(status="queued", attempts=0, actor=None, error=None, cost=0.0)
            return _json(task)
        return _json({"detail": "not found"}, status=404)

    # -- 过滤逻辑（尽量贴近真实 API） ---------------------------------
    def _list_papers(self, params: dict[str, str]) -> list[dict[str, Any]]:
        items = list(self.library.papers)
        query = (params.get("q") or "").strip()
        if query:
            lowered = query.lower()
            items = [
                paper
                for paper in items
                if lowered in str(paper.get("cite_key") or "").lower()
                or lowered in str(paper.get("title") or "").lower()
            ]
        collection_id = params.get("collection_id")
        if collection_id:
            allowed = self.library.collection_members.get(collection_id, set())
            items = [paper for paper in items if paper["id"] in allowed]
        favorite = params.get("favorite")
        if favorite is not None:
            expected = favorite.lower() == "true"
            items = [paper for paper in items if bool(paper.get("favorite")) == expected]
        items.sort(key=lambda paper: paper.get("created_at") or "", reverse=True)
        return _paginate(items, params)

    def _match_papers(self, ref: str) -> list[dict[str, Any]]:
        wanted = ref.strip().lower()
        return [
            paper
            for paper in self.library.papers
            if paper["id"] == ref
            or str(paper.get("cite_key") or "").lower() == wanted
            or str(paper.get("doi") or "").lower() == wanted
            or str(paper.get("arxiv_id") or "").lower() == wanted
        ]

    def _search_figures(self, params: dict[str, str]) -> dict[str, Any]:
        items = list(self.library.figures)
        paper_ref = params.get("paper")
        mode = "browse"
        if paper_ref:
            matched = {paper["id"] for paper in self._match_papers(paper_ref)}
            items = [figure for figure in items if figure["paper_id"] in matched]
            mode = "provenance"
        query = (params.get("q") or "").strip()
        if query:
            lowered = query.lower()
            title_by_id = {paper["id"]: paper.get("title") or "" for paper in self.library.papers}
            items = [
                figure
                for figure in items
                if lowered in str(figure.get("caption") or "").lower()
                or lowered in str(title_by_id.get(figure["paper_id"]) or "").lower()
            ]
            if not paper_ref:
                mode = "semantic"
        label = params.get("label")
        if label:
            wanted = normalize_label(label)
            items = [figure for figure in items if figure.get("label_norm") == wanted]
            mode = "provenance"
        favorite = params.get("favorite")
        if favorite is not None:
            expected = favorite.lower() == "true"
            favorite_ids = {
                paper["id"]
                for paper in self.library.papers
                if bool(paper.get("favorite")) == expected
            }
            items = [figure for figure in items if figure["paper_id"] in favorite_ids]
        collection_id = params.get("collection_id")
        if collection_id:
            allowed = self.library.collection_members.get(collection_id, set())
            items = [figure for figure in items if figure["paper_id"] in allowed]
        total = len(items)
        page = _paginate(items, params)
        return {
            "mode": mode,
            "degraded": mode == "semantic",
            "hint": "语义检索为降级实现（仅匹配 caption 与标题）" if mode == "semantic" else None,
            "total": total,
            "items": page,
        }

    def _citation_review(self, body: dict[str, Any]) -> httpx.Response:
        paper_id = str(body.get("paper_id") or "")
        review = self.library.citation_reviews.get(paper_id)
        if review is None:
            return _json(
                {
                    "detail": {
                        "code": "llm_not_configured",
                        "message": "fake: 没有配置模型（测试里显式设置 citation_reviews）",
                    }
                },
                status=503,
            )
        payload = {
            "verdict": review.get("verdict", "insufficient"),
            "confidence": review.get("confidence", 0.5),
            "reason": review.get("reason", ""),
            "provider": review.get("provider", "fake"),
            "model": review.get("model", "fake-cite-review"),
            "block_ids": body.get("block_ids") or [],
            "tokens_in": review.get("tokens_in", 1),
            "tokens_out": review.get("tokens_out", 1),
            "latency_ms": review.get("latency_ms", 1),
            "prompt_version": "cite-review/1",
        }
        return _json(payload)

    def _golden(self, params: dict[str, str]) -> dict[str, Any]:
        include_pending = (params.get("include_pending") or "").lower() in ("true", "1")
        if (params.get("format") or "") == "bboxes":
            payload = dict(self.library.golden_bboxes)
            payload["include_pending"] = include_pending
            return payload
        payload = dict(self.library.golden)
        payload["include_pending"] = include_pending
        return payload


def _paginate(items: list[dict[str, Any]], params: dict[str, str]) -> list[dict[str, Any]]:
    try:
        limit = int(params.get("limit") or 50)
    except ValueError:
        limit = 50
    try:
        offset = int(params.get("offset") or 0)
    except ValueError:
        offset = 0
    return items[offset : offset + max(1, limit)]


def _json(payload: Any, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=json.loads(json.dumps(payload, ensure_ascii=False)))


__all__ = [
    "ATTN_TEXT",
    "BLOCK_ATTN",
    "BLOCK_COAL",
    "BLOCK_FALLBACK",
    "BLOCK_INTRO",
    "BLOCK_LONG",
    "BLOCK_TRAIN",
    "COAL_TEXT",
    "COLLECTION_CHEM",
    "COLLECTION_NLP",
    "COLLECTION_TRANSFORMERS",
    "FALLBACK_TEXT",
    "FIG_ATTENTION_1",
    "FIG_ATTENTION_2",
    "FIG_COAL_1",
    "FIG_FALLBACK_1",
    "INTRO_TEXT",
    "PAPER_ATTENTION",
    "PAPER_COAL",
    "PAPER_FALLBACK",
    "PAPER_METADATA_ONLY",
    "SUMMARY_ATTENTION_SECTION",
    "SUMMARY_ATTENTION_WHOLE",
    "TABLE_ATTENTION_1",
    "TRAIN_TEXT",
    "VERSION_ATTENTION",
    "FakeAPI",
    "FakeLibrary",
    "default_library",
    "normalize_label",
    "normalize_table_label",
]
