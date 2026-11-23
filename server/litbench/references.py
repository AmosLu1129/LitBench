# SPDX-License-Identifier: AGPL-3.0-or-later
"""参考文献条目生成（2026-11-13）：按格式预设一键生成 references。

设计边界（先说清楚，免得把它当文献管理器）：

* **只用库里的著录信息**（作者 / 标题 / 刊名 / 年 / 卷期页 / DOI / arXiv / cite_key），
  字段缺就省略并在 ``warnings`` 里点名，**不猜、不补、不编**；
* 每种格式只实现「单篇期刊/会议/预印本」的常用写法，不追求边角规则
  （译者、版次、专利、学位论文类型等留给后续按需加）；
* 输出是**纯文本条目**（BibTeX / RIS 除外），不内嵌斜体/粗体标记——
  复制到 Word/LaTeX 里再加样式，比偷偷塞 Markdown 星号干净；
* 中文作者按原样保留，英文作者按各格式的「姓名顺序 + 缩写点」规则处理；
  不认识的姓名（单名、复姓）宁可原样输出，也不做错误拆分。

风格清单见 :data:`REFERENCE_STYLES`；``custom`` 用占位符模板。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 作者解析
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_WS_RE = re.compile(r"\s+")
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_ARXIV_PREFIX_RE = re.compile(r"^(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)", re.IGNORECASE)


@dataclass(frozen=True)
class Author:
    family: str
    given: str
    literal: str
    cjk: bool

    @property
    def initials(self) -> str:
        """Given names → 缩写：Wei-Ming → W.-M.; 空 given 返回 ''。"""
        if not self.given:
            return ""
        parts = []
        for token in self.given.split():
            pieces = []
            for piece in token.split("-"):
                stripped = piece.strip(".")
                if stripped:
                    pieces.append(stripped[0].upper() + ".")
            if pieces:
                parts.append("-".join(pieces))
        return " ".join(parts)


def _clean(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _split_name(raw: str) -> tuple[str, str]:
    """把 'Wei Zhang' / 'Zhang, Wei' / 'van der Waals, Johannes' 拆成 (family, given)。"""
    text = _clean(raw)
    if not text:
        return "", ""
    if _CJK_RE.search(text):
        return text, ""
    if "," in text:
        family, given = text.split(",", 1)
        return family.strip(), given.strip()
    parts = text.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    # 姓氏前的小写介词（van / der / de / von …）归到 family
    index = len(parts) - 1
    while index - 1 >= 0 and parts[index - 1][:1].islower():
        index -= 1
    return " ".join(parts[index:]), " ".join(parts[:index])


def parse_authors(paper: Any) -> list[Author]:
    out: list[Author] = []
    for raw in getattr(paper, "authors_json", None) or []:
        if isinstance(raw, dict):
            family = _clean(raw.get("lastName"))
            given = _clean(raw.get("firstName"))
            literal = _clean(raw.get("name"))
            if not family and not given and literal:
                family, given = _split_name(literal)
                literal = ""
            elif family and given:
                literal = ""
            elif not literal:
                literal = _clean(f"{given} {family}")
            candidate = Author(
                family=family or literal,
                given=given,
                literal=literal,
                cjk=bool(_CJK_RE.search(family or literal)),
            )
        else:
            literal = _clean(raw)
            family, given = _split_name(literal)
            candidate = Author(
                family=family or literal,
                given=given,
                literal=literal,
                cjk=bool(_CJK_RE.search(literal)),
            )
        if candidate.family or candidate.literal:
            out.append(candidate)
    return out


# ---------------------------------------------------------------------------
# 字段读取（ORM 或只读快照都兼容）
# ---------------------------------------------------------------------------
def _get(paper: Any, name: str) -> str:
    value = getattr(paper, name, None)
    if value is None:
        return ""
    return _clean(value)


def _year(paper: Any) -> str:
    value = getattr(paper, "year", None)
    return str(value) if value else ""


def _doi(paper: Any) -> str:
    doi = _DOI_PREFIX_RE.sub("", _clean(getattr(paper, "doi", None)))
    return doi.rstrip(".")


def _arxiv(paper: Any) -> str:
    value = _clean(getattr(paper, "arxiv_id", None))
    return _ARXIV_PREFIX_RE.sub("", value)


def _link(paper: Any) -> str:
    doi = _doi(paper)
    if doi:
        return f"https://doi.org/{doi}"
    arxiv = _arxiv(paper)
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv}"
    return ""


def _venue(paper: Any) -> str:
    venue = _get(paper, "venue")
    if venue:
        return venue
    if _arxiv(paper):
        return "arXiv"
    return ""


ENTRY_TYPES = ("journal", "conference", "preprint", "thesis", "book", "report", "other")

_CONFERENCE_RE = re.compile(r"conference|proceedings|symposium|workshop|congress")


def _entry_type(paper: Any) -> str:
    """条目类型：``paper.entry_type`` 优先，缺失/journal 再按 venue/arXiv 旧规则推断。

    迁移 0015 之前入库的数据一律是 ``journal``（列默认值），如果不做推断，
    历史上按「Proceedings」识别成会议的老格式会退回 [J]。所以这里保留旧推断：
    explicit 只有非 journal 才覆盖；``journal`` 仍走 venue/arXiv 推断。
    ``other`` 是 Zotero 未识别类型的兜底，显式返回，不让它再被猜成期刊。
    """
    explicit = _clean(getattr(paper, "entry_type", "") or "").lower()
    if explicit == "other":
        return "other"
    if explicit and explicit != "journal" and explicit in ENTRY_TYPES:
        return explicit
    venue = _get(paper, "venue").lower()
    if _CONFERENCE_RE.search(venue):
        return "conference"
    if _arxiv(paper) and not _doi(paper) and not venue:
        return "preprint"
    return "journal"


# ---------------------------------------------------------------------------
# 学位论文：学校 / 学位 / 出版地（迁移 0015）
# ---------------------------------------------------------------------------
def _thesis_school(paper: Any) -> str:
    return _get(paper, "thesis_school")


def _thesis_degree(paper: Any) -> str:
    return _get(paper, "thesis_degree")


def _thesis_place(paper: Any) -> str:
    return _get(paper, "thesis_place")


def _thesis_kind(paper: Any) -> str:
    """博士 / 硕士 / 未知。中英文 Zotero 的 thesisType 都认；认不出就不猜。"""
    text = _thesis_degree(paper).lower()
    if re.search(r"博士|doctor|ph\.?\s*d", text):
        return "doctoral"
    if re.search(r"硕士|master|m\.?\s*s", text):
        return "master"
    return "unknown"


# 各风格对「学位论文」的措辞；unknown 用中性 thesis，不替用户认成博士
_THESIS_EN = {
    "doctoral": "Doctoral dissertation",
    "master": "Master's thesis",
    "unknown": "Thesis",
}
_THESIS_IEEE = {
    "doctoral": "Ph.D. dissertation",
    "master": "M.S. thesis",
    "unknown": "thesis",
}
_THESIS_CHICAGO = {
    "doctoral": "PhD diss.",
    "master": "master's thesis",
    "unknown": "thesis",
}
_THESIS_NATURE = {
    "doctoral": "PhD thesis",
    "master": "Master's thesis",
    "unknown": "thesis",
}


def _pages(paper: Any) -> str:
    return _get(paper, "pages").replace("--", "–")


# ---------------------------------------------------------------------------
# 作者串：每种格式各一套
# ---------------------------------------------------------------------------
def _join(items: list[str], *, sep: str = ", ", last: str = " & ") -> str:
    items = [item for item in items if item]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return sep.join(items[:-1]) + last + items[-1]


def _inv(a: Author) -> str:
    """Family, I. N.（APA/Nature/ACS/Elsevier 用的倒装）"""
    if a.cjk:
        return a.literal or a.family
    return f"{a.family}, {a.initials}".rstrip(", ") if a.initials else a.family


def _natural(a: Author) -> str:
    """Given Family（IEEE/MLA 后续作者用）"""
    if a.cjk:
        return a.literal or a.family
    return f"{a.given} {a.family}".strip() if a.given else a.family


def _family_initials(a: Author) -> str:
    """Family I. N.（Vancouver 用：姓氏首字母大写，名缩写）。"""
    if a.cjk:
        return a.literal or a.family
    return f"{a.family} {a.initials}".strip()


def _gbt_one(a: Author) -> str:
    """GB/T 7714 的英文作者：姓氏全大写 + 名缩写（SMITH J）。"""
    if a.cjk:
        return a.literal or a.family
    family = a.family.upper()
    return f"{family} {a.initials.replace('.', '')}".strip()


def _ieee_one(a: Author) -> str:
    if a.cjk:
        return a.literal or a.family
    return f"{a.initials} {a.family}".strip() if a.initials else a.family


def _apa_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 20:
        head = ", ".join(_inv(a) for a in authors[:19])
        return f"{head}, … {_inv(authors[-1])}"
    names = [_inv(a) for a in authors]
    if len(names) == 1:
        return names[0]
    # APA 7：两位作者用 ", &"，三位以上是 "A, B, & C"
    return ", ".join(names[:-1]) + ", & " + names[-1]


def _ieee_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 6:
        return f"{_ieee_one(authors[0])} et al."
    names = [_ieee_one(a) for a in authors]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]


def _gbt_authors(authors: list[Author], *, chinese: bool) -> str:
    if not authors:
        return ""
    names = [_gbt_one(a) for a in authors]
    if len(names) > 3:
        return ", ".join(names[:3]) + (", 等" if chinese else ", et al.")
    return ", ".join(names)


def _mla_one(author: Author) -> str:
    """MLA/Chicago 书目条：姓氏, 名（全名，不缩写）。"""
    if author.cjk:
        return author.literal or author.family
    return f"{author.family}, {author.given}".strip(", ") if author.given else author.family


def _mla_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 2:
        return f"{_mla_one(authors[0])}, et al."
    if len(authors) == 1:
        return _mla_one(authors[0])
    return f"{_mla_one(authors[0])}, and {_natural(authors[1])}"


def _chicago_authors(authors: list[Author]) -> str:
    return _mla_authors(authors)


def _nature_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 5:
        return f"{_inv(authors[0])} et al."
    return _join([_inv(a) for a in authors], last=" & ")


def _science_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 5:
        return f"{_ieee_one(authors[0])} et al."
    return _join([_ieee_one(a) for a in authors], last=" & ")


def _acs_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 10:
        return "; ".join(_inv(a) for a in authors[:10]) + "; et al."
    return "; ".join(_inv(a) for a in authors)


def _elsevier_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 6:
        return ", ".join(_inv(a) for a in authors[:6]) + ", et al."
    return ", ".join(_inv(a) for a in authors)


def _vancouver_authors(authors: list[Author]) -> str:
    if not authors:
        return ""
    if len(authors) > 6:
        return ", ".join(_family_initials(a).replace(".", "") for a in authors[:6]) + ", et al."
    return ", ".join(_family_initials(a).replace(".", "") for a in authors)


# ---------------------------------------------------------------------------
# 单篇格式化
# ---------------------------------------------------------------------------
def _warnings(paper: Any, *, needs_issue: bool = True) -> list[str]:
    out: list[str] = []
    kind = _entry_type(paper)
    if kind == "thesis":
        if not _year(paper):
            out.append("缺出版年")
        if not _thesis_school(paper):
            out.append("缺学校")
        if not _thesis_degree(paper):
            out.append("缺学位类型")
        if not _thesis_place(paper):
            out.append("缺出版地")
        if not parse_authors(paper):
            out.append("缺作者")
        return out
    if kind in ("book", "report", "other"):
        if not _year(paper):
            out.append("缺出版年")
        if not _venue(paper):
            out.append("缺出版者/机构")
        if not parse_authors(paper):
            out.append("缺作者")
        return out
    if not _year(paper):
        out.append("缺出版年")
    if not _venue(paper) and not _arxiv(paper):
        out.append("缺期刊/会议名")
    if _venue(paper) and not _get(paper, "volume"):
        out.append("缺卷号")
    elif needs_issue and _get(paper, "volume") and not _get(paper, "issue"):
        out.append("缺期号")
    if not _get(paper, "pages"):
        out.append("缺页码")
    if not _doi(paper) and not _arxiv(paper):
        out.append("缺 DOI/arXiv")
    if not parse_authors(paper):
        out.append("缺作者")
    return out


def _num(index: int | None, numbered: bool) -> str:
    return f"[{index}] " if numbered and index is not None else ""


def _title(paper: Any) -> str:
    return _get(paper, "title") or "（无标题）"


def _format_gbt7714(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _gbt_authors(parse_authors(paper), chinese=bool(_CJK_RE.search(_title(paper))))
    title = _title(paper)
    year = _year(paper)
    venue = _venue(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper)
    doi = _doi(paper)
    arxiv = _arxiv(paper)
    kind = _entry_type(paper)
    if kind == "thesis":
        # 作者. 题名[D]. 出版地: 学校, 年份.
        place = _thesis_place(paper)
        school = _thesis_school(paper)
        where = f"{place}: {school}" if place and school else (school or place)
        entry = f"{authors}. {title}[D]."
        if where:
            entry += f" {where},"
        if year:
            entry += f" {year}."
        else:
            entry += "."
    elif kind in ("book", "report", "other"):
        label = {"book": "M", "report": "R", "other": "Z"}[kind]
        entry = f"{authors}. {title}[{label}]. {venue}"
        if year:
            entry += f", {year}"
        entry += "."
    elif kind == "preprint":
        container = f"arXiv:{arxiv}" if arxiv else venue
        entry = f"{authors}. {title}[EB/OL]. {container}"
        if year:
            entry += f", {year}"
        entry += "."
    elif kind == "conference":
        entry = f"{authors}. {title}[C]// {venue}"
        if year:
            entry += f", {year}"
        if pages:
            entry += f": {pages}"
        entry += "."
    else:
        entry = f"{authors}. {title}[J]. {venue}"
        if year:
            entry += f", {year}"
        if volume:
            entry += f", {volume}"
            if issue:
                entry += f"({issue})"
        if pages:
            entry += f": {pages}"
        entry += "."
    if doi:
        entry += f" DOI: {doi}."
    return _num(index, numbered) + entry


def _format_apa7(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _apa_authors(parse_authors(paper))
    title = _title(paper)
    year = _year(paper)
    venue = _venue(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper)
    link = _link(paper)
    year_part = f"({year})." if year else "(n.d.)."
    if authors:
        entry = f"{authors}. {year_part} {title}."
    else:
        # APA 无作者时把题名提到作者位（Title. (Year). …），不要以年份开头
        entry = f"{title}. {year_part}"
    kind = _entry_type(paper)
    if kind == "preprint":
        entry += " arXiv."
    elif kind == "thesis":
        # Author. (Year). Title [Doctoral dissertation, School].
        label = _THESIS_EN[_thesis_kind(paper)]
        school = _thesis_school(paper)
        entry += f" [{label}, {school}]." if school else f" [{label}]."
    elif kind in ("book", "report", "other") and venue:
        entry += f" {venue}."
    elif venue:
        entry += f" {venue}"
        if volume:
            entry += f", {volume}"
            if issue:
                entry += f"({issue})"
        if pages:
            entry += f", {pages}"
        entry += "."
    if link:
        entry += f" {link}"
    return entry


def _format_ieee(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _ieee_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper)
    kind = _entry_type(paper)
    prefix = _num(index, numbered)
    if kind == "thesis":
        # Authors, "Title," Ph.D. dissertation, School, Place, Year.
        label = _THESIS_IEEE[_thesis_kind(paper)]
        parts = [f'{authors}, "{title}," {label}']
        school = _thesis_school(paper)
        place = _thesis_place(paper)
        if school:
            parts.append(school)
        if place:
            parts.append(place)
        if year:
            parts.append(year)
        return prefix + ", ".join(parts) + "."
    if kind == "preprint":
        entry = f'{authors}, "{title}," arXiv preprint'
        if _arxiv(paper):
            entry += f" arXiv:{_arxiv(paper)}"
        entry += f", {year}." if year else "."
        return prefix + entry
    container = f"in {venue}" if kind == "conference" else venue
    entry = f'{authors}, "{title}," {container}'
    if volume:
        entry += f", vol. {volume}"
    if issue:
        entry += f", no. {issue}"
    if pages:
        entry += f", pp. {pages}"
    if year:
        entry += f", {year}"
    entry += "."
    if _doi(paper):
        entry += f" doi: {_doi(paper)}."
    return prefix + entry


def _format_mla9(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _mla_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper)
    head = f"{authors}. " if authors else ""
    kind = _entry_type(paper)
    if kind == "thesis":
        # Author. "Title." Doctoral dissertation, School, Year.
        label = _THESIS_EN[_thesis_kind(paper)]
        school = _thesis_school(paper)
        entry = f'{head}"{title}." {label}'
        if school:
            entry += f", {school}"
        if year:
            entry += f", {year}"
        entry += "."
        if _doi(paper):
            entry += f" https://doi.org/{_doi(paper)}."
        return entry
    entry = f'{head}"{title}."'
    if venue:
        entry += f" {venue}"
        if volume:
            entry += f", vol. {volume}"
        if issue:
            entry += f", no. {issue}"
        if year:
            entry += f", {year}"
        if pages:
            entry += f", pp. {pages}"
        entry += "."
    elif year:
        entry += f" {year}."
    if _doi(paper):
        entry += f" https://doi.org/{_doi(paper)}."
    return entry


def _format_chicago(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _chicago_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper)
    head = f"{authors}. " if authors else ""
    if year:
        head += f"{year}. "
    kind = _entry_type(paper)
    if kind == "thesis":
        # Author. Year. "Title." PhD diss., School.
        label = _THESIS_CHICAGO[_thesis_kind(paper)]
        school = _thesis_school(paper)
        entry = f'{head}"{title}." {label}'
        if school:
            entry += f", {school}"
        entry += "."
        if _doi(paper):
            entry += f" https://doi.org/{_doi(paper)}."
        return entry
    entry = f'{head}"{title}."'
    if venue:
        entry += f" {venue}"
        if volume:
            entry += f" {volume}"
            if issue:
                entry += f" ({issue})"
        if pages:
            entry += f": {pages}"
        entry += "."
    if _doi(paper):
        entry += f" https://doi.org/{_doi(paper)}."
    return entry


def _format_nature(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _nature_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    pages = _pages(paper)
    head = f"{authors}. " if authors else ""
    entry = f"{head}{title}."
    kind = _entry_type(paper)
    if kind == "thesis":
        # Author. Title. PhD thesis, School (Year).
        label = _THESIS_NATURE[_thesis_kind(paper)]
        school = _thesis_school(paper)
        entry += f" {label}"
        if school:
            entry += f", {school}"
        if year:
            entry += f" ({year})"
        entry += "."
        return entry
    if venue:
        entry += f" {venue}"
        if volume:
            entry += f" {volume}"
        if pages:
            entry += f", {pages}"
        if year:
            entry += f" ({year})"
        entry += "."
    elif year:
        entry += f" ({year})."
    if _doi(paper):
        entry += f" https://doi.org/{_doi(paper)}"
    return entry


def _format_science(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _science_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    pages = _pages(paper)
    head = f"{authors}. " if authors else ""
    entry = f"{head}{title}."
    kind = _entry_type(paper)
    if kind == "thesis":
        # A. B. Author. Title. PhD thesis, School (Year).
        label = _THESIS_NATURE[_thesis_kind(paper)]
        school = _thesis_school(paper)
        entry += f" {label}"
        if school:
            entry += f", {school}"
        if year:
            entry += f" ({year})"
        entry += "."
        return entry
    if venue:
        entry += f" {venue}"
        if volume:
            entry += f" {volume}"
        if pages:
            entry += f", {pages}"
        if year:
            entry += f" ({year})"
        entry += "."
    elif year:
        entry += f" ({year})."
    if _doi(paper):
        entry += f" https://doi.org/{_doi(paper)}"
    return entry


def _format_acs(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _acs_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper)
    head = f"{authors}. " if authors else ""
    kind = _entry_type(paper)
    if kind == "thesis":
        # Author. Title. Ph.D. Thesis, School, Place, Year.
        label = {"doctoral": "Ph.D. Thesis", "master": "M.S. Thesis", "unknown": "Thesis"}[
            _thesis_kind(paper)
        ]
        parts = [label]
        for value in (_thesis_school(paper), _thesis_place(paper), year):
            if value:
                parts.append(value)
        entry = f"{head}{title}. " + ", ".join(parts) + "."
        if _doi(paper):
            entry += f" DOI: {_doi(paper)}."
        return entry
    entry = f'{head}{title}.'
    if venue:
        entry += f" {venue}"
        if year:
            entry += f" {year}"
    elif year:
        entry += f" {year}"
    if volume and venue:
        entry += f", {volume}"
    if issue and venue:
        entry += f" ({issue})"
    if pages and venue:
        entry += f", {pages}"
    entry += "."
    if _doi(paper):
        entry += f" DOI: {_doi(paper)}."
    return entry


def _format_elsevier(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _elsevier_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper)
    head = f"{authors}" if authors else ""
    if year:
        head += f", {year}." if head else f"{year}."
    entry = f"{head} {title}."
    kind = _entry_type(paper)
    if kind == "thesis":
        # Author, Year. Title. PhD thesis, School, Place.
        label = _THESIS_NATURE[_thesis_kind(paper)]
        parts = [label]
        for value in (_thesis_school(paper), _thesis_place(paper)):
            if value:
                parts.append(value)
        entry += " " + ", ".join(parts) + "."
        link = _link(paper)
        if link:
            entry += f" {link}"
        return entry
    if venue:
        entry += f" {venue}"
        if volume:
            entry += f" {volume}"
            if issue:
                entry += f" ({issue})"
        if pages:
            entry += f", {pages}"
        entry += "."
    link = _link(paper)
    if link:
        entry += f" {link}"
    return entry


def _format_vancouver(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    authors = _vancouver_authors(parse_authors(paper))
    title = _title(paper)
    venue = _venue(paper)
    year = _year(paper)
    volume = _get(paper, "volume")
    issue = _get(paper, "issue")
    pages = _pages(paper).replace("–", "-")
    prefix = _num(index, numbered)
    head = f"{authors}. " if authors else ""
    kind = _entry_type(paper)
    if kind == "thesis":
        # Author. Title [dissertation]. Place: School; Year.
        school = _thesis_school(paper)
        place = _thesis_place(paper)
        entry = f"{head}{title} [dissertation]."
        if place and school:
            entry += f" {place}: {school};"
        elif school:
            entry += f" {school};"
        elif place:
            entry += f" {place};"
        if year:
            entry += f" {year}."
        else:
            entry += "."
        return prefix + entry
    entry = f"{head}{title}. {venue}. {year}"
    if volume:
        entry += f";{volume}"
        if issue:
            entry += f"({issue})"
    if pages:
        entry += f":{pages}"
    entry += "."
    if _doi(paper):
        entry += f" doi:{_doi(paper)}."
    return prefix + entry


def _format_bibtex(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    key = _get(paper, "cite_key") or f"litbench-{str(getattr(paper, 'id', ''))[:8]}"

    def esc(value: str) -> str:
        return (
            value.replace("\\", r"\textbackslash{}")
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("&", r"\&")
            .replace("%", r"\%")
            .replace("_", r"\_")
            .replace("#", r"\#")
        )

    authors = [a for a in parse_authors(paper)]
    kind = _entry_type(paper)
    entry_type = "@article"
    if kind == "thesis":
        # 学位论文用标准条目类型：硕士 @mastersthesis，其余（博士/未知）@phdthesis
        entry_type = "@mastersthesis" if _thesis_kind(paper) == "master" else "@phdthesis"
    elif kind == "conference":
        entry_type = "@inproceedings"
    elif kind == "preprint":
        entry_type = "@misc"
    elif kind == "book":
        entry_type = "@book"
    elif kind == "report":
        entry_type = "@techreport"
    elif kind == "other":
        entry_type = "@misc"

    fields: list[tuple[str, str]] = []
    if _title(paper):
        fields.append(("title", _title(paper)))
    if authors:
        fields.append(("author", " and ".join(_bib_author(a) for a in authors)))
    if kind == "thesis":
        if _thesis_school(paper):
            fields.append(("school", _thesis_school(paper)))
        if _thesis_place(paper):
            fields.append(("address", _thesis_place(paper)))
        if _year(paper):
            fields.append(("year", _year(paper)))
        if _thesis_degree(paper):
            fields.append(("type", _thesis_degree(paper)))
    elif kind == "conference":
        if _venue(paper):
            fields.append(("booktitle", _venue(paper)))
        if _year(paper):
            fields.append(("year", _year(paper)))
        if _get(paper, "volume"):
            fields.append(("volume", _get(paper, "volume")))
        if _get(paper, "pages"):
            fields.append(("pages", _get(paper, "pages").replace("–", "--")))
    elif kind in ("book", "report"):
        if _venue(paper):
            fields.append(
                ("publisher" if kind == "book" else "institution", _venue(paper))
            )
        if _year(paper):
            fields.append(("year", _year(paper)))
    else:
        if _venue(paper):
            fields.append(("journal", _venue(paper)))
        if _year(paper):
            fields.append(("year", _year(paper)))
        if _get(paper, "volume"):
            fields.append(("volume", _get(paper, "volume")))
        if _get(paper, "issue"):
            fields.append(("number", _get(paper, "issue")))
        if _get(paper, "pages"):
            fields.append(("pages", _get(paper, "pages").replace("–", "--")))
    if _doi(paper):
        fields.append(("doi", _doi(paper)))
    if _arxiv(paper):
        fields.append(("eprint", _arxiv(paper)))
        fields.append(("archivePrefix", "arXiv"))
    body = ",\n".join(f"  {name} = {{{esc(value)}}}" for name, value in fields)
    return f"{entry_type}{{{key},\n{body}\n}}"


def _bib_author(author: Author) -> str:
    if author.cjk:
        return author.literal or author.family
    if author.given and author.family:
        return f"{author.family}, {author.given}"
    return author.family


def _format_ris(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    kind = _entry_type(paper)
    ris_kind = {
        "journal": "JOUR",
        "conference": "CPAPER",
        "preprint": "GEN",
        "thesis": "THES",
        "book": "BOOK",
        "report": "RPRT",
        "other": "GEN",
    }.get(kind, "GEN")
    lines = [f"TY  - {ris_kind}"]
    for author in parse_authors(paper):
        if author.cjk:
            lines.append(f"AU  - {author.literal or author.family}")
        else:
            lines.append(f"AU  - {author.family}, {author.given}".rstrip(", "))
    lines.append(f"TI  - {_title(paper)}")
    if kind == "thesis":
        # RIS 学位论文：PB=学校，CY=出版地，M3=学位类型（EndNote/Zotero 认这几个键）
        if _thesis_school(paper):
            lines.append(f"PB  - {_thesis_school(paper)}")
        if _thesis_place(paper):
            lines.append(f"CY  - {_thesis_place(paper)}")
        if _thesis_degree(paper):
            lines.append(f"M3  - {_thesis_degree(paper)}")
    elif _venue(paper):
        key = "PB" if kind in ("book", "report") else "JO"
        lines.append(f"{key}  - {_venue(paper)}")
    if _year(paper):
        lines.append(f"PY  - {_year(paper)}")
    if _get(paper, "volume"):
        lines.append(f"VL  - {_get(paper, 'volume')}")
    if _get(paper, "issue"):
        lines.append(f"IS  - {_get(paper, 'issue')}")
    pages = _pages(paper)
    if pages:
        start, _, end = pages.partition("-")
        lines.append(f"SP  - {start.strip()}")
        if end.strip():
            lines.append(f"EP  - {end.strip()}")
    if _doi(paper):
        lines.append(f"DO  - {_doi(paper)}")
    if _arxiv(paper):
        lines.append(f"UR  - https://arxiv.org/abs/{_arxiv(paper)}")
    lines.append("ER  - ")
    return "\n".join(lines)


def _format_custom(paper: Any, *, index: int | None = None, numbered: bool = True) -> str:
    # 没给模板时按条目类型给一个不撒谎的默认：学位论文不能套期刊的 [J] 模板
    template = (
        DEFAULT_THESIS_TEMPLATE
        if _entry_type(paper) == "thesis"
        else DEFAULT_CUSTOM_TEMPLATE
    )
    return _format_custom_with(paper, template)


DEFAULT_CUSTOM_TEMPLATE = (
    "{authors}. {title}[J]. {journal}, {year}, {volume}({issue}): {pages}. DOI: {doi}"
)
DEFAULT_THESIS_TEMPLATE = "{authors}. {title}[D]. {place}: {school}, {year}."


def _format_custom_with(paper: Any, template: str) -> str:
    authors = _gbt_authors(parse_authors(paper), chinese=bool(_CJK_RE.search(_title(paper))))
    values = {
        "authors": authors or "",
        "authors_short": authors.split(",")[0].strip() if authors else "",
        "title": _title(paper),
        "journal": _venue(paper),
        "year": _year(paper),
        "volume": _get(paper, "volume"),
        "issue": _get(paper, "issue"),
        "pages": _pages(paper),
        "doi": _doi(paper),
        "arxiv": _arxiv(paper),
        "url": _link(paper),
        "cite_key": _get(paper, "cite_key"),
        "type": _entry_type(paper),
        # 学位论文变量（迁移 0015）：{school}/{degree}/{place} 可拼学校模板
        "school": _thesis_school(paper),
        "degree": _thesis_degree(paper),
        "place": _thesis_place(paper),
    }
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return _tidy(text)


def _tidy(text: str) -> str:
    """清掉缺字段留下的空括号/重复标点（只做保守清理，不改正文）。"""
    value = text
    for pattern, repl in (
        (r"\(\s*\)", ""),
        (r"\[\s*\]", ""),
        (r"\{\s*\}", ""),
        (r"\s+", " "),
        (r"\s+([,.;:])", r"\1"),
        (r"([,;:])\s*([,;:.])", r"\2"),
        (r"\.{2,}", "."),
        (r"[,;]\s*\.", "."),
        (r"^\s*[,.;:\-–]+\s*", ""),
        (r"\s+(?:DOI|doi):\s*$", ""),
        (r"\s+(?:DOI|doi):\s*\.\s*$", ""),
    ):
        value = re.sub(pattern, repl, value)
    return value.strip()


# ---------------------------------------------------------------------------
# 风格注册表
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StyleSpec:
    key: str
    label: str
    language: str
    kind: str
    description: str


STYLE_SPECS: tuple[StyleSpec, ...] = (
    StyleSpec("gbt7714", "GB/T 7714-2015（中文期刊/学位论文）", "zh", "text",
              "顺序编码制：[n] 作者. 题名[J]. 刊名, 年, 卷(期): 页."),
    StyleSpec("apa7", "APA 第 7 版（社科/教育）", "en", "text",
              "作者. (年). 题名. 刊名, 卷(期), 页. https://doi.org/…"),
    StyleSpec("ieee", "IEEE（工程/计算机）", "en", "text",
              "[n] A. B. Author, \"Title,\" Journal, vol. X, no. Y, pp. Z, Year."),
    StyleSpec("mla9", "MLA 第 9 版（人文）", "en", "text",
              "Author. \"Title.\" Journal, vol. X, no. Y, Year, pp. Z."),
    StyleSpec("chicago", "Chicago 作者-年份（历史/社科）", "en", "text",
              "Author. Year. \"Title.\" Journal X (Y): Z."),
    StyleSpec("nature", "Nature（顶刊简短式）", "en", "text",
              "Author, A. B., Author, C. D. & Author, E. F. Title. Journal X, pages (Year)."),
    StyleSpec("science", "Science", "en", "text",
              "A. B. Author, C. D. Author & E. F. Author. Title. Journal X, pages (Year)."),
    StyleSpec("acs", "ACS（化学）", "en", "text",
              "Author, A. B.; Author, C. D. Title. Journal Year, X (Y), pages. DOI: …"),
    StyleSpec("elsevier", "Elsevier / Harvard（爱思唯尔期刊）", "en", "text",
              "Author, A. B., Author, C. D., Year. Title. Journal X (Y), pages."),
    StyleSpec("vancouver", "Vancouver（医学）", "en", "text",
              "Author AB, Author CD. Title. Journal. Year;X(Y):pages."),
    StyleSpec("bibtex", "BibTeX（LaTeX \\cite 用）", "neutral", "bibtex",
              "@article / @phdthesis / @mastersthesis / @inproceedings / @book…"),
    StyleSpec("ris", "RIS（EndNote / Zotero 导入）", "neutral", "ris",
              "TY/AU/TI/JO/PY/VL/IS/SP/EP/DO/ER 多行记录；学位论文 TY - THES"),
    StyleSpec("custom", "自定义模板", "neutral", "text",
              "用 {authors}{title}{journal}{year}{volume}{issue}{pages}{doi}{arxiv}{cite_key} 拼"),
)

_FORMATTERS = {
    "gbt7714": _format_gbt7714,
    "apa7": _format_apa7,
    "ieee": _format_ieee,
    "mla9": _format_mla9,
    "chicago": _format_chicago,
    "nature": _format_nature,
    "science": _format_science,
    "acs": _format_acs,
    "elsevier": _format_elsevier,
    "vancouver": _format_vancouver,
    "bibtex": _format_bibtex,
    "ris": _format_ris,
    "custom": _format_custom,
}


class ReferenceError(RuntimeError):
    """生成 references 时的可读错误。"""


def list_styles(sample_paper: Any | None = None) -> list[dict]:
    """给 UI 的格式清单；``sample`` 用同一份示例文献生成，方便前端做即时预览。"""
    out = []
    for spec in STYLE_SPECS:
        sample = ""
        if sample_paper is not None:
            try:
                sample = _format_paper(sample_paper, spec.key, index=1, numbered=True)
            except Exception:  # noqa: BLE001 - 示例生成失败不应影响清单
                sample = ""
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "language": spec.language,
                "kind": spec.kind,
                "description": spec.description,
                "sample": sample,
            }
        )
    return out


def _format_paper(
    paper: Any,
    style: str,
    *,
    index: int | None,
    numbered: bool,
    template: str | None = None,
) -> str:
    spec = {item.key: item for item in STYLE_SPECS}.get(style)
    if spec is None:
        raise ReferenceError(f"不支持的引用格式：{style!r}（可选：{', '.join(_FORMATTERS)}）")
    if style == "custom":
        if template:
            return _tidy(_format_custom_with(paper, template))
        return _tidy(_format_custom(paper))
    formatter = _FORMATTERS[style]
    if spec.kind == "bibtex":
        return formatter(paper, index=index, numbered=False)
    if spec.kind == "ris":
        return formatter(paper, index=index, numbered=False)
    return _tidy(formatter(paper, index=index, numbered=numbered))


def format_reference(
    paper: Any,
    style: str = "gbt7714",
    *,
    index: int | None = 1,
    numbered: bool = True,
    template: str | None = None,
) -> tuple[str, list[str]]:
    """单篇 → (条目文本, warnings)。"""
    entry = _format_paper(paper, style, index=index, numbered=numbered, template=template)
    warnings = []
    if style in ("text", "gbt7714", "apa7", "ieee", "mla9", "chicago", "nature",
                 "science", "acs", "elsevier", "vancouver", "custom"):
        # BibTeX/RIS 由字段结构保证完整性，不按「句子缺段」报警
        warnings = _warnings(paper, needs_issue=style in ("gbt7714", "ieee", "vancouver", "acs", "mla9", "chicago"))
    return entry, warnings


def format_references(
    papers: list[Any],
    style: str = "gbt7714",
    *,
    numbered: bool = True,
    template: str | None = None,
) -> dict:
    """批量 → {style, label, content, items, paper_count}。"""
    spec = {item.key: item for item in STYLE_SPECS}.get(style)
    if spec is None:
        raise ReferenceError(f"不支持的引用格式：{style!r}")
    items = []
    entries: list[str] = []
    for offset, paper in enumerate(papers):
        index = offset + 1 if numbered else None
        entry, warnings = format_reference(
            paper,
            style,
            index=index,
            numbered=numbered,
            template=template,
        )
        items.append(
            {
                "paper_id": str(getattr(paper, "id", "")),
                "cite_key": getattr(paper, "cite_key", None),
                "title": _title(paper),
                "entry": entry,
                "warnings": warnings,
            }
        )
        entries.append(entry)
    # BibTeX/RIS 是多行记录，条目之间空一行；其余每行一条
    separator = "\n\n" if spec.kind in ("bibtex", "ris") else "\n"
    return {
        "style": style,
        "label": spec.label,
        "paper_count": len(items),
        "content": separator.join(entries),
        "items": items,
    }


__all__ = [
    "DEFAULT_CUSTOM_TEMPLATE",
    "DEFAULT_THESIS_TEMPLATE",
    "ENTRY_TYPES",
    "STYLE_SPECS",
    "Author",
    "ReferenceError",
    "format_reference",
    "format_references",
    "list_styles",
    "parse_authors",
]
