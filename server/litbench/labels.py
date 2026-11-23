# SPDX-License-Identifier: AGPL-3.0-or-later
"""解析查询文本。

这里只有纯函数，不依赖 DB / HTTP，所以可以单独跑测试：

    python server/tests/test_labels.py

设计要点：**一次查询可以同时带多种线索**。
"Wang 2019 Fig. 3" 不是一个 intent，而是 (作者+年份) 与 (图号) 两个过滤条件的组合。
所以不做互斥的 intent routing，而是把线索全部抽出来，能抽到几个就约束几个。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- 图号 ---------------------------------------------------------------

_KIND = {
    "fig": "fig",
    "figure": "fig",
    "图": "fig",
    "tab": "tab",
    "table": "tab",
    "表": "tab",
}

LABEL_RE = re.compile(
    r"(?P<kind>figure|fig|table|tab|图|表)"
    r"\s*[.．。:：]?\s*"
    r"(?P<pre>[sS])?"
    r"(?P<num>\d{1,3})"
    # 中文教材有两套编号，都要收：
    #   「图 10-5」= 第 10 章第 5 图；
    #   「图 4.2.1」= 4.2 节第 1 图（自动控制原理这类书全用点分）。
    # 分隔符必须是 - / . 这类真分隔符，不能是空格——否则 "Fig. 3 of 10" 会被拼成 fig:310。
    r"(?P<subnum>(?:[-–—.．]\s*\d{1,3})*)"
    r"(?P<sub>[a-z])?",  # 子图字母必须紧贴数字：Fig. 3a 是子图，Fig. 3 of... 不是
    re.IGNORECASE,
)


# --- 图注判定（pdfium 行级解析和 MinerU 的块级输出都要用） -------------------
_CAPTION_SEPARATORS = ".:：。—–)"

# 正文里也常有 "Figure 3 shows that ..." 这种句子。如果只判断行首是不是
# "Fig 3"，会把整段正文误判成 caption，进而让「图号覆盖率」超过 100%。
# 真正的 caption：编号后面跟着分隔符（句点/冒号/破折号），或者紧接标题名词；
# 正文则以动词开头（shows / illustrates / ...）。
_BODY_VERBS = (
    "shows", "show", "showed", "illustrates", "illustrate", "depicts", "depict",
    "presents", "present", "reports", "report", "gives", "give", "compares",
    "compare", "indicates", "indicate", "summarizes", "is", "are",
    "plots", "plot", "reveals", "reveal", "suggests", "suggest", "describes",
    "describe", "lists", "list", "highlights", "highlight", "displays",
    "display", "denotes", "represent", "represents", "details", "detail",
    "outlines", "outline", "captures", "capture", "measures", "measure",
)


def looks_like_caption(text: str) -> bool:
    """这一行/块像不像图注（"Fig. 3" / "图 10-5 ..."）。"""
    stripped = (text or "").strip()
    match = LABEL_RE.match(stripped)
    if not match:
        return False
    matched = stripped[: match.end()].rstrip()
    if matched and matched[-1] in _CAPTION_SEPARATORS:
        return True
    rest = stripped[match.end():].lstrip()
    if not rest:
        return True
    if rest[0] in _CAPTION_SEPARATORS:
        return True
    first_word = rest.split(" ", 1)[0].strip(",，").lower()
    return first_word not in _BODY_VERBS


# --- 文献标识 -----------------------------------------------------------

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")
ARXIV_RE = re.compile(r"(?:arxiv\s*[:/]\s*|arxiv\.org/abs/)(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
# Zotero 风格 citekey：wang2019deep，必须带 4 位年份且无空格
CITEKEY_RE = re.compile(r"\b([a-z][a-z\-]{1,20}\d{4}[a-z0-9]{0,20})\b")

AUTHOR_YEAR_RE = re.compile(
    r"\b(?P<author>[A-Z][A-Za-z\-']{1,30})"
    r"(?:\s+(?:et\s+al\.?|and\s+[A-Z][A-Za-z\-']{1,30}))?"
    r"\s*[,，]?\s*"
    r"(?P<year>(?:19|20)\d{2})\b"
)

_PUNCT = " \t\r\n,，;；:：.。、-_/|()（）[]【】"


def normalize_label(raw: str) -> str | None:
    """把各种写法的图号归一成 ``fig:3`` / ``fig:3a`` / ``fig:s3`` / ``tab:2``。

    返回 None 表示这段文本里没有图号。
    展示用的原文写法不在这里处理——调用方要同时保存 ``label_raw``，
    因为写进论文的引用必须写成原文的「Fig. 3」，不能是归一化后的形式。
    """
    m = LABEL_RE.search(raw or "")
    if not m:
        return None
    kind = _KIND[m.group("kind").lower()]
    pre = "s" if m.group("pre") else ""
    num = m.group("num")
    subnum = m.group("subnum")
    sub = (m.group("sub") or "").lower()
    # 图 10-5 / 图 4.2.1 都归一成「短横线连接」的稳定形式：
    #   fig:10-5、fig:4-2-1。章节级编号不能丢，否则整章 14 张图全叫「图 10」；
    # 搜索走同一个函数，所以「图 4.2.1」与「图4-2-1」命中的是同一张。
    if subnum:
        parts = re.findall(r"\d{1,3}", subnum)
        num = "-".join([num, *parts])
    return f"{kind}:{pre}{num}{sub}"


def normalize_title(title: str) -> str:
    """标题归一化，用于模糊匹配。

    只保留字母数字和空格，压成单空格、转小写。
    不追求语言学正确——目的是让 "Deep Learning for NLP" 与
    "deep learning for nlp." 命中同一行。
    """
    s = (title or "").lower()
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def split_label(norm: str) -> tuple[int | None, str | None]:
    """把 ``fig:3a`` 拆成 ``(3, "a")``；``fig:s3`` 是补充材料，返回 ``(3, None)``。

    ``fig:10-5``（中文教材的「图 10-5」）拆成 ``(10, "5")``：
    10 是章号、5 是该章内的图序，两者都要留下。点分编号同理：
    ``fig:4-2-1``（「图 4.2.1」）拆成 ``(4, "2-1")``。
    """
    if not norm or ":" not in norm:
        return None, None
    tail = norm.split(":", 1)[1].lstrip("s")
    if "-" in tail:
        head, _, rest = tail.partition("-")
        digits = "".join(ch for ch in head if ch.isdigit())
        return (int(digits) if digits else None), (rest or None)
    digits = "".join(ch for ch in tail if ch.isdigit())
    sub = "".join(ch for ch in tail if ch.isalpha())
    return (int(digits) if digits else None), (sub or None)


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    out: list[str] = []
    pos = 0
    for start, end in sorted(spans):
        if start < pos:
            continue
        out.append(text[pos:start])
        pos = end
    out.append(text[pos:])
    return "".join(out)


@dataclass
class ParsedQuery:
    """一次检索请求拆出来的所有线索。字段非空即代表该约束生效。"""

    raw: str = ""
    label: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    citekey: str | None = None
    author: str | None = None
    year: int | None = None
    text: str = ""

    spans: list[tuple[int, int]] = field(default_factory=list, repr=False)

    @property
    def is_provenance(self) -> bool:
        """是否构成「出处检索」——确定性查找，不允许返回相似结果。"""
        return any([self.label, self.doi, self.arxiv, self.citekey, self.author])

    @property
    def has_paper_ref(self) -> bool:
        return any([self.doi, self.arxiv, self.citekey, self.author, self.text])


def parse_query(raw: str) -> ParsedQuery:
    q = ParsedQuery(raw=raw or "")
    if not q.raw.strip():
        return q

    # 1) DOI 最长匹配优先，避免被标题片段吃掉
    if m := DOI_RE.search(raw):
        q.doi = m.group(1).rstrip(").,;")
        q.spans.append(m.span(1))

    # 2) arXiv
    if m := ARXIV_RE.search(raw):
        q.arxiv = m.group(1)
        q.spans.append(m.span(1))

    # 3) citekey（全小写无空格，含 4 位年份）
    if not q.arxiv and (m := CITEKEY_RE.search(raw)):
        q.citekey = m.group(1)
        q.spans.append(m.span(1))

    # 4) 图号。直接在原文上找——label 必须带 fig/图 之类的前缀，
    #    不会和 DOI 里的数字混淆，也不会被误当成年份。
    if m := LABEL_RE.search(raw):
        q.label = normalize_label(m.group(0))
        q.spans.append(m.span(0))

    # 5) 作者 + 年份
    if m := AUTHOR_YEAR_RE.search(raw):
        q.author = m.group("author")
        q.year = int(m.group("year"))
        q.spans.append(m.span())

    # 6) 剩下的就是自由文本
    leftover = _remove_spans(raw, q.spans).strip(_PUNCT)
    q.text = leftover if len(leftover) >= 2 else ""
    return q


__all__ = [
    "LABEL_RE",
    "ParsedQuery",
    "looks_like_caption",
    "normalize_label",
    "normalize_title",
    "parse_query",
]
