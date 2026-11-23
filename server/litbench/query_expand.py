# SPDX-License-Identifier: AGPL-3.0-or-later
"""M9.1b-lite：复用已配置的 chat 模型做检索查询改写。

为什么不是向量召回：当前产品决策是「配置一个模型 API 管所有」。但 chat completion
模型不提供 embedding 接口，真正的向量索引仍需要 embedding provider。所以这里做的是
**词面检索的语义辅助**：把中文/口语查询改写成英文论文里会出现的术语，再交给 BM25
排序。它解决「中文问英文图表」和一部分同义改写，但不是 embedding 相似度，不改变
``engine=bm25`` 的事实。

纪律：

* 只在查询含 CJK 时自动触发（英文查询不额外烧模型）；
* 未配置模型 / 调用失败 / 超时 → 返回 None，检索静默回退 BM25，不抛错；
* 结果按查询文本做进程内缓存，重复搜索不重复付费；
* 返回体里带 ``provider``/``model``/``keywords``，调用方要如实展示「本次做过改写」。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .config import settings
from .llm import LLMError, effective_config, get_client, parse_json_object

logger = logging.getLogger("litbench.query_expand")

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CACHE: dict[str, "ExpandedQuery"] = {}

PROMPT_VERSION = "query-expand/1"


@dataclass(slots=True)
class ExpandedQuery:
    original: str
    text: str
    keywords: list[str] = field(default_factory=list)
    english_query: str = ""
    provider: str = ""
    model: str = ""
    prompt_version: str = PROMPT_VERSION


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def should_expand(raw: str | None) -> bool:
    """默认只在中文查询上触发；英文查询继续走纯 BM25（不额外花钱）。"""
    text = (raw or "").strip()
    return bool(settings.query_expand_enabled and len(text) >= 2 and has_cjk(text))


def _clean_keywords(raw) -> list[str]:  # noqa: ANN001 — 模型输出不可信
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        value = str(item or "").strip().strip(",;，；。")
        if not value or len(value) > 40:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) >= settings.query_expand_max_keywords:
            break
    return cleaned


def _cache_key(raw: str) -> str:
    return " ".join((raw or "").lower().split())


def _remember(raw: str, value: ExpandedQuery) -> None:
    key = _cache_key(raw)
    _CACHE[key] = value
    while len(_CACHE) > max(1, settings.query_expand_cache_size):
        _CACHE.pop(next(iter(_CACHE)))


def reset_cache() -> None:
    _CACHE.clear()


def _prompt(raw: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "你是学术文献检索助手。把用户的中文/口语查询改写成适合在英文论文库里"
                "做词面检索的英文术语；保留方法名、材料名、缩写和数字。不要解释，不要"
                "编造用户没提到的对象。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户查询：{raw}\n\n"
                '输出 JSON：{"english_query":"一句英文检索式",'
                '"keywords":["英文技术词或同义词，最多8个"]}'
            ),
        },
    ]


async def expand_query(raw: str | None) -> ExpandedQuery | None:
    """把查询改写成英文检索词；不可用/失败/无有效关键词时返回 None。"""
    text = (raw or "").strip()
    if not should_expand(text):
        return None
    cached = _CACHE.get(_cache_key(text))
    if cached is not None:
        return cached
    source, _cfg = effective_config()
    if source == "mock":
        return None
    try:
        client = get_client()
        # 检索辅助不能被慢模型拖死：用独立的短超时，不走默认 60s。
        result = await client.complete(
            _prompt(text),
            json_schema={"type": "object"},
            meta={"task": "query_expand", "prompt_version": PROMPT_VERSION},
            max_tokens=400,
            timeout_s=settings.query_expand_timeout_s,
        )
    except (LLMError, OSError, TimeoutError) as exc:
        logger.info("query expansion skipped: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — 检索辅助失败不能让搜索 500
        logger.warning("query expansion failed unexpectedly: %s", exc)
        return None

    parsed = result.parsed if isinstance(result.parsed, dict) else None
    if parsed is None:
        parsed = parse_json_object(result.text)
    parsed = parsed or {}
    english = str(parsed.get("english_query") or "").strip()
    keywords = _clean_keywords(parsed.get("keywords"))
    if not english and not keywords:
        return None
    # 原始中文保留（图号/中文标题场景可能仍有用），英文改写词追加到后面
    combined = " ".join(part for part in (text, english, *keywords) if part).strip()
    expanded = ExpandedQuery(
        original=text,
        text=combined[:600],
        keywords=keywords,
        english_query=english,
        provider=result.provider,
        model=result.model,
    )
    _remember(text, expanded)
    return expanded


__all__ = [
    "ExpandedQuery",
    "PROMPT_VERSION",
    "expand_query",
    "has_cjk",
    "reset_cache",
    "should_expand",
]
