# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSL 参考文献渲染（M8，2026-11-16）。

**引擎选择：pandoc --citeproc**（独立子进程），不是把 citeproc-py 链接进主程序。

为什么不用 citeproc-py（实测对比，见 docs/15 §12.42）：

* 官方 GB/T 7714-2015 样式里「文献类型标识」和载体标识在同一个 ``cs:group``，
  citeproc-py 按「组内变量全空就抑制整组」的静态规则处理，没有 DOI/URL 的条目会
  连 ``[J]`` / ``[D]`` / ``[C]`` 一起吞掉；citeproc-js（Zotero/Word 用的引擎）不会；
* IEEE 样式里「引号内标点」的位置与 citeproc-js 不同；
* APA 的参考文献表在 citeproc-py 里不按样式规则排序。

pandoc 的 citeproc 与 Zotero 输出一致，而且下一步的 docx/pdf 导出本来就要用它，
所以这里统一走 pandoc。pandoc 是 GPL-2.0 **子进程**，不链接进主程序（THIRD_PARTY.md）。

边界（先说清楚）：

* 只渲染**参考文献表**（bibliography），不做正文 in-text citation 排版；
* 只搬运库里已有字段，缺什么让 CSL 样式按自己的规则省略，不在本模块里猜；
* 输出**纯文本**（斜体/小型大写等格式被剥掉），与手写模板引擎的行为一致；
* pandoc 缺失 → :class:`CslUnavailable`（API 转 503）；渲染失败/超时 →
  :class:`CslError`（API 转 502）；模板引擎不受影响。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from . import references as refs
from .config import settings
from .toolchain import (
    MIN_PANDOC_VERSION,
    executable_name,
    find_tool,
    tool_version,
)


# ---------------------------------------------------------------------------
# 样式注册表（文件随仓库分发，来源与 SHA-256 见 csl_styles/README.md）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CslStyle:
    key: str
    filename: str


CSL_STYLES: tuple[CslStyle, ...] = (
    CslStyle(
        "gbt7714",
        "china-national-standard-gb-t-7714-2015-numeric.csl",
    ),
    CslStyle("apa7", "apa.csl"),
    CslStyle("ieee", "ieee.csl"),
)

_CSL_BY_KEY = {style.key: style for style in CSL_STYLES}
_STYLE_DIR = Path(__file__).with_name("csl_styles")

# 手写模板的条目类型 → CSL item type。preprint 用 article（CSL 1.0 没有 preprint），
# 其余按 CSL 标准类型映射；认不出的 other 用 article 兜底，与参考文献模板的 Z 对应。
_CSL_TYPES = {
    "journal": "article-journal",
    "conference": "paper-conference",
    "preprint": "article",
    "thesis": "thesis",
    "book": "book",
    "report": "report",
    "other": "article",
}

_WHITESPACE_RE = re.compile(r"\s+")
_ENTRY_ID_PREFIX = "ref-"

# 用 nocite 让 pandoc 输出「全部条目」，不依赖正文里有没有 \cite。
_PANDOC_INPUT = '---\nnocite: "@*"\n---\n'


class CslError(RuntimeError):
    """CSL 渲染失败（pandoc 非零退出 / 超时 / 输出不可解析）。"""


class CslUnavailable(CslError):
    """环境里没有可用的 pandoc；CSL 引擎不可用，但模板引擎照常。"""


# ---------------------------------------------------------------------------
# pandoc 定位
# ---------------------------------------------------------------------------
def find_pandoc() -> str | None:
    """按「配置 → 项目内钉版本 → PATH」找 pandoc，找不到返回 None。

    项目内钉版本（``scripts/get_pandoc.py`` 装到 ``.data/tools/pandoc/<版本>/``）
    优先于 PATH：系统里可能有一个旧版 pandoc（如 apt 的 2.x），而 CSL/导出依赖
    3.x 的行为和 ``--sandbox``；装了便携版就用便携版，避免「看起来装了却报错」。
    版本门槛由 ``pandoc_version`` / ``csl_engine_status`` 判定。
    """
    return find_tool("pandoc", settings.pandoc_path, "pandoc", executable_name("pandoc"))


def pandoc_version(path: str | None = None) -> tuple[int, int] | None:
    """可执行 pandoc 的主/次版本号；找不到或读不出来返回 None。"""
    target = path or find_pandoc()
    if target is None:
        return None
    return tool_version(target)


def pandoc_is_supported(path: str | None = None) -> bool:
    """pandoc 是否满足 CSL/导出要求（≥3.0，``--sandbox`` 与现行 CLI 行为）。"""
    version = pandoc_version(path)
    return version is not None and version >= MIN_PANDOC_VERSION


def _pandoc_version_note(version: tuple[int, int] | None) -> str:
    if version is None:
        return "无法读取 pandoc 版本（--version 失败）"
    return f"pandoc 版本过低：{version[0]}.{version[1]}（需要 ≥3.0）"


def csl_engine_status() -> tuple[bool, str]:
    """(CSL 引擎是否可用, 给界面看的说明)。"""
    if not settings.csl_enabled:
        return False, "CSL 引擎已被配置关闭（CSL_ENABLED=0）；手写模板不受影响。"
    pandoc = find_pandoc()
    if pandoc is None:
        return (
            False,
            (
                "CSL 引擎不可用：没找到 pandoc。装 pandoc 或设 PANDOC_PATH 后重启 API；"
                "手写模板不受影响。"
            ),
        )
    version = pandoc_version(pandoc)
    if version is None or version < MIN_PANDOC_VERSION:
        return (
            False,
            "CSL 引擎不可用："
            + _pandoc_version_note(version)
            + "；用 scripts/get_pandoc.py 或设 PANDOC_PATH 指向 3.x。手写模板不受影响。",
        )
    return True, "CSL 引擎可用（pandoc --citeproc，官方 CSL 样式；输出纯文本）。"


def csl_style_keys() -> list[str]:
    return [style.key for style in CSL_STYLES]


def has_csl_style(style: str) -> bool:
    return style in _CSL_BY_KEY


def style_file(style: str) -> Path | None:
    """样式 key → 内置 .csl 文件路径；未知 key 返回 None。"""
    spec = _CSL_BY_KEY.get(style)
    if spec is None:
        return None
    return _STYLE_DIR / spec.filename


def _style_label(style: str) -> str:
    for spec in refs.STYLE_SPECS:
        if spec.key == style:
            return spec.label
    return style


# ---------------------------------------------------------------------------
# Paper → CSL JSON
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _text(paper: Any, name: str) -> str:
    return _clean(getattr(paper, name, None))


def _csl_names(paper: Any) -> list[dict[str, str]]:
    """作者 → CSL name 对象。

    与 ``references.parse_authors`` 的区别：这里直接读原始 ``authors_json``，
    Zotero 的机构作者（只有 ``name`` 字段）保持 ``literal``，不再拆成姓名。
    """
    names: list[dict[str, str]] = []
    for raw in getattr(paper, "authors_json", None) or []:
        if isinstance(raw, dict):
            family = _clean(raw.get("lastName"))
            given = _clean(raw.get("firstName"))
            literal = _clean(raw.get("name"))
            if family:
                name = {"family": family}
                if given:
                    name["given"] = given
                names.append(name)
            elif literal:
                names.append({"literal": literal})
            continue
        text = _clean(raw)
        if not text:
            continue
        if refs._CJK_RE.search(text):
            # 中文姓名不拆姓/名：CSL 的 family/given 拆法对「张三」没有意义，
            # 用 literal 原样输出（GB/T 姓前名后、APA 全名都正确）。
            names.append({"literal": text})
            continue
        family, given = refs._split_name(text)
        if family:
            name = {"family": family}
            if given:
                name["given"] = given
            names.append(name)
    return names


def build_csl_item(paper: Any) -> dict[str, Any]:
    """一篇 Paper → CSL JSON item（只搬运已有字段，不补不猜）。"""
    kind = refs._entry_type(paper)
    item: dict[str, Any] = {
        "id": str(getattr(paper, "id", "") or ""),
        "type": _CSL_TYPES.get(kind, "article"),
    }
    title = _text(paper, "title")
    if title:
        item["title"] = title
    names = _csl_names(paper)
    if names:
        item["author"] = names

    year = refs._year(paper)
    if year:
        item["issued"] = {"date-parts": [[int(year)]]}

    venue = _text(paper, "venue")
    doi = refs._doi(paper)
    pages = refs._pages(paper)
    if doi:
        item["DOI"] = doi
    # GB/T 等样式对中文条目用「等/卷/期」、英文条目用 et al.；语言按题名/刊名判定。
    if refs._CJK_RE.search(title) or refs._CJK_RE.search(venue):
        item["language"] = "zh-CN"
    else:
        item["language"] = "en-US"

    if kind in ("journal", "conference"):
        if venue:
            item["container-title"] = venue
        volume = _text(paper, "volume")
        issue = _text(paper, "issue")
        if volume:
            item["volume"] = volume
        if issue:
            item["issue"] = issue
        if pages:
            item["page"] = pages
    elif kind == "thesis":
        school = refs._thesis_school(paper)
        place = refs._thesis_place(paper)
        degree = refs._thesis_degree(paper)
        if school:
            item["publisher"] = school
        if place:
            item["publisher-place"] = place
        if degree:
            item["genre"] = degree
    elif kind in ("book", "report"):
        # 手写模板对 [M]/[R] 把 venue 当出版者；CSL 的 publisher 语义相同。
        if venue:
            item["publisher"] = venue
        if pages:
            item["page"] = pages
    else:  # preprint / other
        arxiv = refs._arxiv(paper)
        if kind == "preprint":
            # CSL 1.0 没有 preprint 类型，Zotero 也用 article + genre=Preprint；
            # 仓储名（arXiv / SSRN…）放 publisher，编号放 number。
            item["genre"] = "Preprint"
            item["publisher"] = venue or "arXiv"
            if arxiv:
                item["number"] = arxiv
        elif venue:
            item["publisher"] = venue
    link = refs._link(paper)
    if link and not doi:
        item["URL"] = link
    return item


def export_csl_json(papers: list[Any]) -> list[dict[str, Any]]:
    """给 pandoc / Zotero / 后续 docx 导出用的 CSL JSON 列表。"""
    return [build_csl_item(paper) for paper in papers]


# ---------------------------------------------------------------------------
# pandoc 调用与 HTML 解析
# ---------------------------------------------------------------------------
def _run_pandoc(pandoc: str, style_file: Path, items: list[dict[str, Any]]) -> str:
    with tempfile.TemporaryDirectory(prefix="litbench-csl-") as tmp:
        workdir = Path(tmp)
        (workdir / "refs.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
        (workdir / "input.md").write_text(_PANDOC_INPUT, encoding="utf-8")
        command = [
            pandoc,
            str(workdir / "input.md"),
            "--citeproc",
            f"--bibliography={workdir / 'refs.json'}",
            f"--csl={style_file}",
            # 文献元数据也是用户可控文本；sandbox 禁止 pandoc 去读正文/元数据里引用的资源
            "--sandbox",
            "-t",
            "html",
            "--wrap=none",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.csl_timeout_s,
                stdin=subprocess.DEVNULL,
                cwd=workdir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CslError(
                f"pandoc 渲染超时（>{settings.csl_timeout_s:g}s）"
            ) from exc
        except OSError as exc:
            raise CslUnavailable(f"无法启动 pandoc：{exc}") from exc
        if result.returncode != 0:
            stderr_lines = [
                line.strip()
                for line in (result.stderr or "").splitlines()
                if line.strip()
            ]
            detail = stderr_lines[-1] if stderr_lines else "无错误输出"
            raise CslError(f"pandoc 渲染失败（退出码 {result.returncode}）：{detail}")
        return result.stdout or ""


class _CslHtmlParser(HTMLParser):
    """从 pandoc 的 HTML 参考文献表里取出 (paper id, 纯文本条目)。

    pandoc 输出的结构是::

        <div id="refs" class="references csl-bib-body">
          <div id="ref-<paper id>" class="csl-entry">
            <div class="csl-left-margin">[1] </div>
            <div class="csl-right-inline">…</div>
          </div>
        </div>

    ``csl-left-margin`` 和 ``csl-right-inline`` 直接拼接即可（前者自带尾空格）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, str]] = []
        self._buffer: list[str] | None = None
        self._entry_id = ""
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "div":
            return
        attributes = {name: (value or "") for name, value in attrs}
        classes = attributes.get("class", "").split()
        if self._buffer is None:
            if "csl-entry" in classes:
                self._buffer = []
                self._entry_id = (
                    attributes.get("data-csl-entry-id") or attributes.get("id", "")
                )
                self._depth = 1
        else:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag != "div" or self._buffer is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = _WHITESPACE_RE.sub(" ", "".join(self._buffer)).strip()
        if text:
            self.entries.append((self._entry_id, text))
        self._buffer = None

    def handle_data(self, data: str) -> None:
        if self._buffer is not None:
            self._buffer.append(data)


def _paper_id_from_entry(raw: str) -> str:
    if raw.startswith(_ENTRY_ID_PREFIX):
        return raw[len(_ENTRY_ID_PREFIX) :]
    return raw


# ---------------------------------------------------------------------------
# 渲染入口
# ---------------------------------------------------------------------------
def render_bibliography(papers: list[Any], style: str) -> dict[str, Any]:
    """按 CSL 样式渲染参考文献表 → 与 ``references.format_references`` 同形的 dict。"""
    spec = _CSL_BY_KEY.get(style)
    if spec is None:
        raise CslError(
            f"格式 {style!r} 没有 CSL 样式；可用：{', '.join(csl_style_keys())}"
        )
    if not settings.csl_enabled:
        raise CslUnavailable("CSL 引擎已被配置关闭（CSL_ENABLED=0）")
    pandoc = find_pandoc()
    if pandoc is None:
        raise CslUnavailable(
            "CSL 引擎需要 pandoc：没找到可执行的 pandoc"
            "（安装 pandoc 或设 PANDOC_PATH；手写模板不受影响）"
        )
    version = pandoc_version(pandoc)
    if version is None or version < MIN_PANDOC_VERSION:
        raise CslUnavailable(
            "CSL 引擎需要 pandoc ≥3.0：" + _pandoc_version_note(version)
        )
    style_file = _STYLE_DIR / spec.filename
    if not style_file.is_file():
        raise CslError(f"CSL 样式文件缺失：{spec.filename}")

    items = export_csl_json(papers)
    html = _run_pandoc(pandoc, style_file, items)
    parser = _CslHtmlParser()
    parser.feed(html)
    parser.close()

    by_id = {str(getattr(paper, "id", "")): paper for paper in papers}
    out_items: list[dict[str, Any]] = []
    for raw_id, entry in parser.entries:
        paper_id = _paper_id_from_entry(raw_id)
        paper = by_id.get(paper_id)
        if paper is None:
            out_items.append(
                {
                    "paper_id": "",
                    "cite_key": None,
                    "title": "",
                    "entry": entry,
                    "warnings": ["pandoc 返回的条目没能对应到所选文献（id 不匹配）"],
                }
            )
            continue
        out_items.append(
            {
                "paper_id": paper_id,
                "cite_key": getattr(paper, "cite_key", None),
                "title": _text(paper, "title"),
                "entry": entry,
                "warnings": [],
            }
        )

    # 极端情况下 CSL 样式可能整条不输出（比如条目没有任何可渲染字段）：
    # 不编造条目，留空并在 warnings 里点名。
    rendered_ids = {item["paper_id"] for item in out_items if item["paper_id"]}
    for paper in papers:
        paper_id = str(getattr(paper, "id", ""))
        if paper_id in rendered_ids:
            continue
        out_items.append(
            {
                "paper_id": paper_id,
                "cite_key": getattr(paper, "cite_key", None),
                "title": _text(paper, "title"),
                "entry": "",
                "warnings": ["CSL 样式没有输出这条（字段不足或 id 冲突）"],
            }
        )

    entries = [item["entry"] for item in out_items if item["entry"]]
    return {
        "style": style,
        "label": _style_label(style),
        "paper_count": len(papers),
        "content": "\n".join(entries),
        "items": out_items,
        "engine": "csl",
        "note": (
            "CSL 引擎（pandoc --citeproc）：条目顺序/编号由样式规则决定，"
            "「带序号」开关对 CSL 引擎无效；输出为纯文本。"
        ),
    }


__all__ = [
    "CSL_STYLES",
    "CslError",
    "CslUnavailable",
    "build_csl_item",
    "csl_engine_status",
    "csl_style_keys",
    "export_csl_json",
    "find_pandoc",
    "has_csl_style",
    "pandoc_is_supported",
    "pandoc_version",
    "render_bibliography",
    "style_file",
]
