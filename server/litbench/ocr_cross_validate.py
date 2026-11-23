# SPDX-License-Identifier: AGPL-3.0-or-later
"""OCR × 视觉大模型交叉校验（轻量 OCR 的第二道关）。

流程：轻量 OCR 先出文字 → 把整页图片 + OCR 文字交给用户配置的视觉模型逐字校对：
* 没有出入：直接采用 OCR 结果（不再打扰用户）；
* 有出入：写一条 ``OcrProposal``，由用户在阅读页决定接受 OCR 还是接受模型修订。

这里只做「校对」，不做摘要/改写；模型不可用/未配置时直接跳过（保留 OCR 结果）。
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .lite_ocr import page_image_jpeg
from .llm import LLMError, effective_config, get_client, parse_json_object, vision_message

log = logging.getLogger("litbench.ocr_cross_validate")

_SYSTEM_PROMPT = """你是严格的 OCR 校对员。用户会给你一张扫描页图片和 OCR 得到的文字。
你的任务只是逐字核对，不做总结、改写、翻译、补全或润色：
- 不要改变原文的换行、标点、公式、表格中的数字和单位；
- 只修正 OCR 明显识别错的字符；
- 图片看不清的内容保持原样，不要猜测。
输出严格 JSON：
{"same": true/false, "corrected_text": "完整校对后的文字（same=false 时必填）",
 "differences": [{"ocr": "原 OCR 片段", "model": "页面上的正确片段", "reason": "一句话"}]}
same=true 表示 OCR 与页面一致（differences 可为空）。"""

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "same": {"type": "boolean"},
        "corrected_text": {"type": "string"},
        "differences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ocr": {"type": "string"},
                    "model": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["ocr", "model"],
            },
        },
    },
    "required": ["same"],
}

MAX_PAGES = 80
SIMILARITY_SAME = 0.995


class CrossCheckUnavailable(RuntimeError):
    """模型不可用/不支持图片等；继续下一页也没有意义。"""


@dataclass(slots=True)
class OcrCrossCheck:
    same: bool
    model_text: str
    differences: list[dict] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    latency_ms: int = 0


def _normalize(text: str) -> str:
    return "".join(str(text or "").split())


def _similarity(left: str, right: str) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


async def cross_check_page(
    pdf_path: Path, page_no: int, ocr_text: str
) -> OcrCrossCheck | None:
    """校对一页。返回 None 表示跳过（未配置模型 / 渲染失败 / 接口失败）。"""
    text = (ocr_text or "").strip()
    if not text:
        return None
    source, cfg = effective_config()
    if cfg is None:
        log.info("未配置模型，跳过 OCR 交叉校验（保留 OCR 结果）")
        return None
    image = page_image_jpeg(Path(pdf_path), int(page_no))
    if image is None:
        return None
    prompt = (
        f"这是第 {page_no} 页的整页图片。下面是 OCR 得到的文字，请逐字校对：\n\n"
        f"----- OCR 开始 -----\n{text}\n----- OCR 结束 -----"
    )
    try:
        client = get_client()
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            *vision_message(prompt, image, media_type="image/jpeg"),
        ]
        result = await client.complete(
            messages,
            json_schema=_JSON_SCHEMA,
            meta={"source": source, "purpose": "ocr_cross_check"},
            max_tokens=min(4096, max(512, len(text) * 3)),
        )
    except LLMError as exc:
        log.warning("第 %s 页 OCR 交叉校验失败（保留 OCR 结果）：%s", page_no, exc)
        raise CrossCheckUnavailable(str(exc)) from exc
    parsed = result.parsed if isinstance(result.parsed, dict) else parse_json_object(result.text)
    if not isinstance(parsed, dict) or "same" not in parsed:
        log.warning("第 %s 页交叉校验返回格式不可解析：%s", page_no, result.text[:200])
        return None
    model_text = str(parsed.get("corrected_text") or "").strip()
    raw_diffs = parsed.get("differences") or []
    diffs = [d for d in raw_diffs if isinstance(d, dict)] if isinstance(raw_diffs, list) else []
    declared_same = bool(parsed.get("same"))
    if declared_same and not model_text:
        model_text = text
    if not declared_same and not model_text:
        log.warning("第 %s 页模型报告有差异但没有给 corrected_text，跳过", page_no)
        return None
    similarity = _similarity(text, model_text) if model_text else 1.0
    # 模型说 same、但修订文本和 OCR 差很多 → 不信任 same，写成提案；
    # 模型说不同但文本几乎一致（只改了空白）→ 当一致，避免噪声提案。
    same = bool(declared_same and similarity >= SIMILARITY_SAME and not diffs)
    if not declared_same and similarity >= 0.999 and not diffs:
        same = True
    log.info(
        "第 %s 页 OCR 交叉校验：same=%s similarity=%.4f diffs=%s model=%s",
        page_no,
        same,
        similarity,
        len(diffs),
        result.model,
    )
    return OcrCrossCheck(
        same=same,
        model_text=model_text or text,
        differences=diffs,
        provider=result.provider,
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost=result.cost,
        latency_ms=result.latency_ms,
    )


async def cross_check_pages(
    pdf_path: Path, pages: list[tuple[int, str]], *, limit: int | None = None
) -> list[tuple[int, OcrCrossCheck]]:
    """逐页校验；最多 limit 页（控制费用）。"""
    results: list[tuple[int, OcrCrossCheck]] = []
    for page_no, text in pages[: limit or MAX_PAGES]:
        try:
            checked = await cross_check_page(Path(pdf_path), page_no, text)
        except CrossCheckUnavailable as exc:
            # 模型不支持图片/额度/网络问题：第一页失败就停，不逐页重复烧请求
            log.warning("停止 OCR 交叉校验：%s", exc)
            break
        if checked is not None:
            results.append((page_no, checked))
    return results


__all__ = [
    "MAX_PAGES",
    "CrossCheckUnavailable",
    "OcrCrossCheck",
    "cross_check_page",
    "cross_check_pages",
]
