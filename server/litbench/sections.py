# SPDX-License-Identifier: AGPL-3.0-or-later
"""章节树：把一篇文献的 block 序列切成「章节区间」（M2a）。

为什么不直接信 ``block.type``：
* **Docling / MinerU 教材**：标题块是 ``section``，但还混着 running header（每页重复）、
  习题/题解/表题、OCR 噪声；
* **pdfium（arXiv 等）**：没有标题类型，只有逐行 ``text``，两栏还会交错——无法可靠还原
  作者章节。

所以这里做「保守启发式」：只认明确像标题的行（章/编号/摘要/参考文献），过滤每页重复的
running header 和表图/习题类噪声；**切不出 ≥2 节时退化成按页窗口的伪章节**（``kind=page``），
在 UI/摘要里如实标明，不假装是作者章节。真实的章节质量留到 M2b 用模型调 prompt 时再提。

输出 ``Section`` 只依赖 block 的 (id, page, order_idx, type, text)，便于单测。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# --- 标题模式 ---------------------------------------------------------------
CHAPTER_RE = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百]+\s*章(?:[\s、.．:：]|$)"
    r"|(?:chapter|CHAPTER)\s+[0-9IVXivx]+(?:[\s.:：]|$))"
)
NUMBERED_RE = re.compile(r"^(\d{1,2}(?:[.．]\d{1,2}){1,3})[.．、]?\s+(\S.*)$")
SINGLE_NUM_RE = re.compile(r"^(\d{1,2})[.)．、]\s+([A-Za-z\u4e00-\u9fff].*)$")
CN_NUM_RE = re.compile(r"^([一二三四五六七八九十]+)[、.．]\s*(\S.*)$")
ROMAN_RE = re.compile(r"^([IVX]{1,5})\.\s+(\S.*)$")
KNOWN_RE = re.compile(r"^(?:abstract|摘要|references|参考文献|appendix|附录)(?:[\s:：]|$)", re.I)
# 「习题 / 复习题 / 参考文献」之后的都是练习区——不再当章节切，收成一个尾章节。
# 只在文档后段（≥70% 页）生效：教材选段可能从「上一章习题/参考文献」开始，
# 那种位置不能一刀切掉后面的真实章节（实测 04 燃烧学）。
STOP_RE = re.compile(
    r"^(?:[I|丨]\s*)?(?:习题|复习题|思考题|课后习题|参考文献|附录)(?:\d*\s*$|[|丨I]|\s)"
)
STOP_PAGE_RATIO = 0.7
CHAPTER_ONLY_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*章$")
SECTION_CN_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十]+\s*节[\s、.．:：]")

BAD_PREFIXES = (
    "表",
    "图",
    "式",
    "例题",
    "题解",
    "目录",
    "注：",
    "注:",
    "关键词",
    "关键字",
)
BAD_SUBSTRINGS = ("http://", "https://", "@", "……", "**")
SENT_END = "。！？；;，,：:"
RUNNING_HEADER_MIN_PAGES = 3
MAX_HEADING_CHARS = 80
MIN_BODY_CHARS = 8


@dataclass(slots=True)
class Section:
    """一个章节区间。``block_ids`` 只放可当证据的正文块（text/caption）。"""

    key: str
    title: str
    level: int
    kind: str  # heading | page
    start_idx: int  # 在「按 (page, order_idx) 排序后的 block 列表」里的下标
    end_idx: int  # 左闭右开
    page_start: int
    page_end: int
    block_ids: list[str]
    char_count: int
    figure_count: int = 0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "level": self.level,
            "kind": self.kind,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "block_count": len(self.block_ids),
            "char_count": self.char_count,
            "figure_count": self.figure_count,
        }


@dataclass(slots=True)
class _Heading:
    idx: int
    title: str
    level: int
    page: int


def _get(block, name: str):
    return block.get(name) if isinstance(block, dict) else getattr(block, name)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    t = str(text).replace("\u3000", " ").replace("\u00a0", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _norm_header(text: str) -> str:
    """running header 判定用：去掉空白，OCR 常把「第 4 章」写成「第4 章」「第 4章」。"""
    return re.sub(r"\s+", "", text)


def _is_running_header(titles: list[tuple[str, int]]) -> set[str]:
    """同一段文字（去空白后）出现在 ≥3 个不同页 → 当作 running header（每页重复的页眉）。"""
    pages: dict[str, set[int]] = {}
    for text, page in titles:
        pages.setdefault(_norm_header(text), set()).add(page)
    return {text for text, pgs in pages.items() if len(pgs) >= RUNNING_HEADER_MIN_PAGES}


def _pattern_level(text: str) -> tuple[int, bool] | None:
    """返回 (level, relative)。relative=True 表示「1. 小标题」这类按父级递推的级别。"""
    if CHAPTER_RE.match(text):
        return 1, False
    if SECTION_CN_RE.match(text):
        return 2, False
    m = NUMBERED_RE.match(text)
    if m:
        # 10.2 -> 2，10.2.1 -> 3；一级编号（2. 应川范围）也走这里但要按父级递推
        dots = m.group(1).count(".") + m.group(1).count("．")
        if dots == 0:
            return 0, True
        return 1 + dots, False
    if SINGLE_NUM_RE.match(text):
        return 0, True
    if CN_NUM_RE.match(text):
        return 0, True
    if ROMAN_RE.match(text):
        return 0, True
    return None


def _heading(block) -> tuple[str, int | None, bool] | None:
    """判断一个 block 是不是标题。返回 (清理后的标题, level|None, relative)。"""
    text = _clean(_get(block, "text"))
    btype = str(_get(block, "type") or "")
    if not text or len(text) > MAX_HEADING_CHARS:
        return None
    if text[-1] in SENT_END:
        return None
    if any(bad in text for bad in BAD_SUBSTRINGS):
        return None
    if text.startswith(BAD_PREFIXES):
        return None
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return None

    # 练习/参考区也当「尾章节」保留（后续 build_sections 会把它之后的内容收进来）
    compact = _norm_header(text)
    if STOP_RE.match(compact):
        return text, 2, False

    known = KNOWN_RE.match(text)
    if known:
        return text, 1, False
    pattern = _pattern_level(text)
    if pattern is not None:
        level, relative = pattern
        return text, (level or None), relative
    # 只有 section 类型才接受「没有编号的标题」；text 类型（pdfium 行）必须是强模式
    if btype == "section" and len(text) <= 40:
        return text, None, False
    return None


def _relative_style(text: str) -> str:
    """相对编号的风格：一、 / 1. / I. ——同一风格视为同级（兄弟），换风格视为下一级。"""
    if CN_NUM_RE.match(text):
        return "cn"
    if SINGLE_NUM_RE.match(text):
        return "num"
    if ROMAN_RE.match(text):
        return "roman"
    return "other"


def _section_key(title: str, start_block_id: str, level: int) -> str:
    raw = f"{level}|{title}|{start_block_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def build_sections(
    blocks,
    *,
    page_count: int | None = None,
    figure_pages: list[int] | None = None,
    parser: str | None = None,
) -> list[Section]:
    """把 block 列表切成章节区间。blocks 需有 id/page/order_idx/type/text。

    ``parser="pdfium"`` 时直接走按页伪章节：pdfium 只给逐行 text、两栏还常交错，
    从行里「猜标题」会切出一堆假章节（实测 arXiv：表头数字、句子碎片都能命中编号模式），
    不如如实按页切、在 UI 里标明 ``kind=page``。
    """
    ordered = sorted(blocks, key=lambda b: (int(_get(b, "page") or 0), int(_get(b, "order_idx") or 0)))
    if not ordered:
        return []

    if (parser or "").lower() == "pdfium":
        return _page_window_sections(ordered, page_count, figure_pages or [])

    # 1) 标题候选 + running header 过滤
    page_by_text: list[tuple[str, int]] = []
    for b in ordered:
        t = _clean(_get(b, "text"))
        if t:
            page_by_text.append((t, int(_get(b, "page") or 0)))
    repeated = _is_running_header(page_by_text)

    raw: list[_Heading] = []
    first_page_of: dict[str, int] = {}
    for idx, b in enumerate(ordered):
        got = _heading(b)
        if got is None:
            continue
        title, level, _relative = got
        page = int(_get(b, "page") or 0)
        norm = _norm_header(title)
        key_pages = first_page_of.setdefault(norm, page)
        if norm in repeated:
            # 每页重复的章名（running header）：只保留第一次出现当章节入口，其余丢掉
            if not CHAPTER_RE.match(title) or page != key_pages:
                continue
        raw.append(_Heading(idx=idx, title=title, level=level or 0, page=page))

    # 2) 「第 X 章」+ 同页紧邻的纯标题行合并成一行（docling 常把章号与章名拆两块）
    merged: list[_Heading] = []
    for h in raw:
        if merged:
            prev = merged[-1]
            prev_is_chapter_only = bool(CHAPTER_ONLY_RE.match(prev.title)) or bool(
                re.fullmatch(r"(?i)chapter\s+\d+", prev.title)
            )
            adjacent = h.page == prev.page and 0 < h.idx - prev.idx <= 3
            if prev_is_chapter_only and adjacent and h.level == 0 and len(h.title) <= 40:
                merged[-1] = _Heading(
                    idx=prev.idx, title=f"{prev.title} {h.title}", level=1, page=prev.page
                )
                continue
        merged.append(h)

    # 3) relative 级别按「编号风格栈」递推：同风格是兄弟，换风格是下一级，
    #    回到之前的风格时回到原来层级（一、→ 1. → 2. → 二、）。
    stack: list[tuple[str, int]] = []
    prev_level = 0
    for h in merged:
        if h.level == 0:
            style = _relative_style(h.title)
            existing = next((lv for st, lv in stack if st == style), None)
            if existing is not None:
                h.level = existing
            else:
                h.level = min(prev_level + 1, 5) if prev_level else 2
                stack.append((style, h.level))
        stack = [(st, lv) for st, lv in stack if lv <= h.level]
        prev_level = h.level

    # 4) 文档后段的「习题/复习题/参考文献」之后不再切章节：收成一个尾章节（练习/参考区）
    max_page = max(int(_get(b, "page") or 0) for b in ordered) or 1
    total_pages = max(int(page_count or 0), max_page, 1)
    stop_page = total_pages * STOP_PAGE_RATIO
    stop_at: int | None = None
    for i, h in enumerate(merged):
        if STOP_RE.match(_norm_header(h.title)) and h.page >= stop_page:
            stop_at = i
            h.title = _normalize_stop_title(h.title)
            h.level = min(h.level, 2)
            break
    if stop_at is not None:
        merged = merged[: stop_at + 1]

    # 5) 组装区间
    if len(merged) >= 1:
        sections = _sections_from_headings(ordered, merged, figure_pages or [])
        kept: list[Section] = []
        for i, section in enumerate(sections):
            next_level = sections[i + 1].level if i + 1 < len(sections) else 0
            # 空标题（只有标题、下面直接是更深一级）保留为父节点，方便树形展示
            if section.block_ids or next_level > section.level:
                kept.append(section)
        if len(kept) >= 1:
            return kept

    # 6) 退化：按页窗口切（pdfium 的 arXiv、或标题识别失败的扫描件）
    return _page_window_sections(ordered, page_count, figure_pages or [])


def _normalize_stop_title(title: str) -> str:
    """「I 习题 I」「I 参考文献 1」这类 OCR 头尾噪声 → 「习题」「参考文献」。"""
    text = re.sub(r"^[I|丨]\s*", "", title)
    text = re.sub(r"[\s\d|丨I]+$", "", text).strip()
    text = re.sub(r"\s+", "", text)
    return text or title


def _body_blocks(ordered, start: int, end: int, repeated: set[str]) -> list:
    out = []
    for b in ordered[start:end]:
        if str(_get(b, "type") or "") not in ("text", "caption"):
            continue
        text = _clean(_get(b, "text"))
        if len(text) < MIN_BODY_CHARS:
            continue
        if _norm_header(text) in repeated:
            continue
        out.append(b)
    return out


def _sections_from_headings(ordered, headings: list[_Heading], figure_pages: list[int]) -> list[Section]:
    repeated = _is_running_header(
        [(_clean(_get(b, "text")), int(_get(b, "page") or 0)) for b in ordered]
    )
    out: list[Section] = []
    for i, h in enumerate(headings):
        end = headings[i + 1].idx if i + 1 < len(headings) else len(ordered)
        body = _body_blocks(ordered, h.idx + 1, end, repeated)
        pages = [int(_get(b, "page") or 0) for b in ordered[h.idx:end]] or [h.page]
        start_id = str(_get(ordered[h.idx], "id"))
        out.append(
            Section(
                key=_section_key(h.title, start_id, h.level),
                title=h.title,
                level=h.level,
                kind="heading",
                start_idx=h.idx,
                end_idx=end,
                page_start=min(pages),
                page_end=max(pages),
                block_ids=[str(_get(b, "id")) for b in body],
                char_count=sum(len(_clean(_get(b, "text"))) for b in body),
                figure_count=sum(1 for p in figure_pages if min(pages) <= p <= max(pages)),
            )
        )
    return out


def _page_window_sections(
    ordered, page_count: int | None, figure_pages: list[int]
) -> list[Section]:
    max_page = int(_get(ordered[-1], "page") or 1)
    total_pages = max(int(page_count or 0), max_page, 1)
    window = max(1, -(-total_pages // 6))  # 约 6 个伪章节
    repeated = _is_running_header(
        [(_clean(_get(b, "text")), int(_get(b, "page") or 0)) for b in ordered]
    )
    out: list[Section] = []
    page = 1
    while page <= total_pages:
        last = min(page + window - 1, total_pages)
        idxs = [
            i
            for i, b in enumerate(ordered)
            if page <= int(_get(b, "page") or 0) <= last
        ]
        if not idxs:
            page = last + 1
            continue
        body = _body_blocks(ordered, idxs[0], idxs[-1] + 1, repeated)
        title = f"第 {page}–{last} 页" if last > page else f"第 {page} 页"
        out.append(
            Section(
                key=_section_key(title, str(_get(ordered[idxs[0]], "id")), 1),
                title=title,
                level=1,
                kind="page",
                start_idx=idxs[0],
                end_idx=idxs[-1] + 1,
                page_start=page,
                page_end=last,
                block_ids=[str(_get(b, "id")) for b in body],
                char_count=sum(len(_clean(_get(b, "text"))) for b in body),
                figure_count=sum(1 for p in figure_pages if page <= p <= last),
            )
        )
        page = last + 1
    return [s for s in out if s.block_ids]
