# SPDX-License-Identifier: AGPL-3.0-or-later
"""表格 HTML → LaTeX tabular（M9.3a，2026-11-15）。

为什么需要它：表格在前端已经能看（M9.3 之前的「表格进界面」），但写论文时
用户还得手抄；Docling 抽出的 HTML 带 ``colspan/rowspan``，直接复制到 LaTeX
会丢合并单元格。这里用标准库 ``HTMLParser`` 做一个保守转换：

* 只认 ``table/caption/thead/tbody/tfoot/tr/td/th``；其它标签当文本容器；
* ``colspan`` → ``\\multicolumn``；``rowspan`` → ``\\multirow``（需要 multirow 宏包）；
* 单元格文本做 LaTeX 转义（``& % # _ { } ~ ^ \\``）；
* 表题优先用库里的 ``TableBlock.caption``；库里没有表号时从 caption 的
  ``Table 3`` 前缀推导显示用 label，不改数据库；
* 输出同时给 ``tabular`` 片段和可直接粘贴的 ``table`` 浮动体，缺字段/需要宏包
  只进 warnings，不瞎编。

不做：不生成学校模板、不处理图片表（没有 HTML 时明确报错）、不做列宽自动测量。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .labels import normalize_label

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_CAPTION_LABEL = re.compile(
    r"^(?:table|tab\.?|表)\s*([0-9]{1,3}(?:[-.．–—]\s*[0-9]{1,3})*)",
    re.IGNORECASE,
)
_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_UNSAFE_LABEL = re.compile(r"[^A-Za-z0-9:._-]")
# Docling 抽出的表格常带 Unicode 数学字符（ℓ、𝜃、β…）；直接写进 LaTeX 在
# 默认字体下缺字形。先 NFKC（数学斜体字母→普通字母、全角→半角），再把
# 希腊字母/常用符号换成数学模式命令。NFKC 必须放在转义之前，否则全角 ％
# 会变成未转义的 %。
_GREEK_COMMANDS = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho",
    "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\varphi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Φ": r"\Phi",
    "Ψ": r"\Psi", "Ω": r"\Omega",
}
_MATH_SYMBOLS = {
    "ℓ": r"\ell", "ℏ": r"\hbar", "±": r"\pm", "×": r"\times",
    "÷": r"\div", "≤": r"\le", "≥": r"\ge", "≠": r"\ne",
    "≈": r"\approx", "∞": r"\infty", "°": r"^\circ", "→": r"\to",
    "−": "-", "·": r"\cdot",
}
_MAX_COLUMNS = 40
MAX_COLUMNS = _MAX_COLUMNS
MAX_CELLS = 2000

# M9.3c：宽表自动换行。LaTeX 的 l 列不换行，长单元格会把表撑出页宽；这里按
# 内容比例改成 p{…\linewidth} 列。阈值是「半角字符宽度」的估算（中文按 2 计），
# 不是精确排版测量：宁可早一点换行，也不要等用户编译出 Overfull 再手改。
WRAP_TOTAL_UNITS = 78       # 一行大约能放 78 个半角字符
WRAP_SINGLE_UNITS = 44      # 单个单元格超过这个长度就认为必须换行
MIN_COLUMN_FRACTION = 0.045  # 单列最窄 4.5% 页宽，避免数字列被压成竖排
COLUMN_PADDING_FRACTION = 0.0264  # 列间距折算成页宽：2×\tabcolsep(6pt) / 455pt
CHAR_HALF_PT = 5.3          # 10pt 字号下半角字符的粗略宽度
COLUMN_PADDING_PT = 12.0    # 一列左右 \tabcolsep 合计约 12pt
PAGE_TEXT_PT = 455.0        # A4 + 2.5cm 页边距的正文宽（只用于「放不下」提示）


class TableLatexError(RuntimeError):
    """表格无法转 LaTeX（没有结构化 HTML / HTML 里没有表格）。"""


@dataclass
class _Cell:
    text: str = ""
    colspan: int = 1
    rowspan: int = 1
    header: bool = False


@dataclass
class TableLatex:
    tabular: str
    code: str
    caption: str = ""
    label: str = ""
    label_key: str = ""
    columns: int = 0
    rows: int = 0
    warnings: list[str] = field(default_factory=list)
    # M9.3c：是否因宽表改成了 p 列自动换行，以及实际使用的列格式串
    wrapped: bool = False
    column_spec: str = ""


class _TableParser(HTMLParser):
    """只抽第一张 table 的结构；畸形 HTML 尽量容错，不抛异常。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self.caption_parts: list[str] = []
        self.warnings: list[str] = []
        self._table_seen = False
        self._done = False
        self._depth = 0
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._in_caption = False
        self._cell_count = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._done:
            return
        if tag == "table":
            if self._table_seen:
                self.warnings.append("HTML 里有多张表，只导出第一张")
                self._done = True
                return
            self._table_seen = True
            self._depth = 1
            return
        if not self._table_seen:
            return
        if tag == "caption":
            self._in_caption = True
            return
        if tag == "tr":
            self._finish_row()
            self._row = []
            return
        if tag in ("td", "th"):
            self._finish_cell()
            if self._row is None:
                self._row = []
            self._cell_count += 1
            if self._cell_count > MAX_CELLS:
                raise TableLatexError("表格单元格过多（>2000），拒绝导出")
            attr = dict(attrs)
            colspan = self._span(attr.get("colspan"))
            rowspan = self._span(attr.get("rowspan"))
            self._cell = _Cell(colspan=colspan, rowspan=rowspan, header=(tag == "th"))
            return
        if tag == "br" and self._cell is not None:
            self._cell.text += " "

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._done:
            return
        if tag == "caption":
            self._in_caption = False
            return
        if tag in ("td", "th"):
            self._finish_cell()
            return
        if tag == "tr":
            self._finish_row()
            return
        if tag == "table":
            self._finish_row()
            self._done = True

    def handle_data(self, data: str) -> None:
        if not self._table_seen or self._done:
            return
        if self._in_caption:
            self.caption_parts.append(data)
        elif self._cell is not None:
            self._cell.text += data

    def close(self) -> None:
        super().close()
        self._finish_row()
        self._finish_cell()

    def _finish_cell(self) -> None:
        if self._cell is None:
            return
        if self._row is None:
            self._row = []
        text = _SPACE.sub(" ", self._cell.text).strip()
        self._row.append(
            _Cell(
                text=text,
                colspan=self._cell.colspan,
                rowspan=self._cell.rowspan,
                header=self._cell.header,
            )
        )
        self._cell = None

    def _finish_row(self) -> None:
        self._finish_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    @staticmethod
    def _span(raw: str | None) -> int:
        try:
            value = int(str(raw or "1").strip())
        except ValueError:
            return 1
        return max(1, min(value, 50))


def _escape(text: str) -> str:
    return "".join(_LATEX_ESCAPES.get(char, char) for char in text)


def _mathify(text: str) -> str:
    """NFKC + 转义 + 希腊/常用符号换数学命令；顺序见文件头注释。"""
    # ℓ/ℏ 会被 NFKC 拍成普通字母（ℓ→l），先换成私用区占位符保住语义
    text = (text or "").replace("ℓ", "\ue000").replace("ℏ", "\ue001")
    normalized = unicodedata.normalize("NFKC", text)
    escaped = _escape(normalized)
    out: list[str] = []
    for char in escaped:
        command = _GREEK_COMMANDS.get(char) or _MATH_SYMBOLS.get(char)
        if char == "\ue000":
            command = r"\ell"
        elif char == "\ue001":
            command = r"\hbar"
        if command:
            out.append(f"${command}$")
        else:
            out.append(char)
    return "".join(out)


def _strip_tags(text: str) -> str:
    return _SPACE.sub(" ", _TAG.sub(" ", text or "")).strip()


def _caption_and_label(
    caption_raw: str,
    label_raw: str | None,
    label_norm: str | None,
    table_id: str,
) -> tuple[str, str, str]:
    """返回 (caption 文本, 显示 label, LaTeX label key)。"""
    caption = _strip_tags(caption_raw)
    display_label = (label_raw or "").strip()
    norm = (label_norm or "").strip()
    if not norm:
        match = _CAPTION_LABEL.match(caption)
        if match:
            display_label = display_label or match.group(0).strip()
            norm = normalize_label(display_label) or ""
            if norm:
                caption = caption[match.end() :].lstrip(" .:：-–—")
    elif display_label and caption.lower().startswith(display_label.lower()):
        caption = caption[len(display_label) :].lstrip(" .:：-–—")
    # label key 只保留 LaTeX 安全字符；中文表号会被替换成短横线
    if norm and norm.startswith("tab:"):
        key = norm
    elif norm:
        key = f"tab:{norm}"
    else:
        key = f"tab:{table_id[:8]}"
    key = _UNSAFE_LABEL.sub("-", key)
    return caption, display_label, key


def _build_grid(rows: list[list[_Cell]]) -> tuple[list[list[tuple[int, int, int, _Cell]]], list[tuple[int, int, int, int, _Cell]], int]:
    """把带 rowspan 的行格子摊平成 (行内放置, 跨行区间, 最大列数)。"""
    placed_rows: list[list[tuple[int, int, int, _Cell]]] = []
    spans: list[tuple[int, int, int, int, _Cell]] = []
    active: dict[int, int] = {}
    max_columns = 0
    for row_index, cells in enumerate(rows):
        placements: list[tuple[int, int, int, _Cell]] = []
        column = 0
        for cell in cells:
            while any((column + offset) in active for offset in range(cell.colspan)):
                column += 1
            placements.append((column, cell.colspan, cell.rowspan, cell))
            if cell.rowspan > 1:
                for offset in range(cell.colspan):
                    active[column + offset] = cell.rowspan
                spans.append((row_index, column, cell.colspan, cell.rowspan, cell))
            column += cell.colspan
            max_columns = max(max_columns, column)
            if max_columns > MAX_COLUMNS:
                raise TableLatexError("表格列数过多（>40），拒绝导出")
        placed_rows.append(placements)
        for key in list(active):
            active[key] -= 1
            if active[key] <= 0:
                del active[key]
    return placed_rows, spans, max(1, max_columns)


def _display_units(text: str) -> int:
    """估算文本的半角字符宽度：CJK 全角按 2，其余按 1。"""
    total = 0
    for char in _SPACE.sub(" ", text or ""):
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _column_weights(placed_rows, max_columns: int) -> list[float]:  # noqa: ANN001
    """每列的内容宽度（半角单位）；跨列单元格按 colspan 均摊。"""
    weights = [1.0] * max(1, max_columns)
    for placements in placed_rows:
        for column, colspan, _rowspan, cell in placements:
            share = _display_units(cell.text) / max(1, colspan)
            for offset in range(colspan):
                index = column + offset
                if index < len(weights):
                    weights[index] = max(weights[index], share)
    return weights


def _needs_wrap(weights: list[float], longest_cell: int) -> bool:
    return sum(weights) > WRAP_TOTAL_UNITS or longest_cell > WRAP_SINGLE_UNITS


def _longest_token_units(text: str) -> int:
    """列里最长不可断词的宽度（半角单位）。

    拉丁词按空白切开就是一个不可断词；中文可以在任意字之间换行，所以按单字宽（2）
    估算，不把整段中文当不可断词，否则会误判「放不下」。
    """
    longest = 0
    for token in (text or "").split():
        units = _display_units(token)
        has_cjk = any(
            unicodedata.east_asian_width(char) in ("W", "F") for char in token
        )
        longest = max(longest, 2 if has_cjk else units)
    return longest


def _table_may_overflow(
    placed_rows, max_columns: int  # noqa: ANN001
) -> bool:
    """按「最长不可断词 + 列间距」估算；放不下时 p 列也救不了，需要横排/拆表。"""
    min_units = [0.0] * max(1, max_columns)
    for placements in placed_rows:
        for column, colspan, _rowspan, cell in placements:
            share = _longest_token_units(cell.text) / max(1, colspan)
            for offset in range(colspan):
                index = column + offset
                if index < len(min_units):
                    min_units[index] = max(min_units[index], share)
    required = sum(units * CHAR_HALF_PT + COLUMN_PADDING_PT for units in min_units)
    return required > PAGE_TEXT_PT * 0.98


def _target_fraction(columns: int) -> float:
    """p 列总宽占 ``\\linewidth`` 的比例：列越多，给 ``\\tabcolsep`` 留的越多。"""
    return max(0.55, 0.94 - COLUMN_PADDING_FRACTION * (columns - 1))


def _column_min_fractions(placed_rows, max_columns: int) -> list[float]:  # noqa: ANN001
    """每列至少要留的宽度：装得下那列最长的不可断词，否则 p 列也会 Overfull。"""
    minimum = [MIN_COLUMN_FRACTION] * max(1, max_columns)
    for placements in placed_rows:
        for column, colspan, _rowspan, cell in placements:
            units = _longest_token_units(cell.text) / max(1, colspan)
            needed = (units * CHAR_HALF_PT + 6.0) / PAGE_TEXT_PT
            for offset in range(colspan):
                index = column + offset
                if index < len(minimum):
                    minimum[index] = max(minimum[index], needed)
    return minimum


def _column_fractions(weights: list[float], minimum: list[float] | None = None) -> list[float]:
    columns = len(weights)
    target = _target_fraction(columns)
    floor = list(minimum) if minimum is not None else [MIN_COLUMN_FRACTION] * columns
    smoothed = [weight + 1.5 for weight in weights]
    total = sum(smoothed) or 1.0
    fractions = [target * weight / total for weight in smoothed]
    for _ in range(3):
        fractions = [max(floor[index], value) for index, value in enumerate(fractions)]
        scale = target / sum(fractions) if sum(fractions) > target else 1.0
        fractions = [value * scale for value in fractions]
    return fractions


def _fraction_latex(fraction: float) -> str:
    return rf"{fraction:.3f}\linewidth"


def _span_fraction(fractions: list[float], start: int, span: int) -> float:
    """合并单元格的 p 宽：覆盖列的宽度和 + 被吃掉的列间距。"""
    value = sum(fractions[start : start + span]) + COLUMN_PADDING_FRACTION * (span - 1)
    return min(0.98, max(MIN_COLUMN_FRACTION, value))


def _column_spec(columns: int, fractions: list[float] | None) -> str:
    if fractions is None:
        return "l" * max(1, columns)
    body = "".join(rf"p{{{_fraction_latex(value)}}}" for value in fractions)
    return "@{}" + body + "@{}"


def _row_latex(
    placements: list[tuple[int, int, int, _Cell]],
    covered: set[int],
    max_columns: int,
    fractions: list[float] | None = None,
) -> str:
    by_column = {column: (colspan, rowspan, cell) for column, colspan, rowspan, cell in placements}
    parts: list[str] = []
    column = 0
    while column < max_columns:
        if column in covered:
            # 列被上方 rowspan 占着：本行仍要留出空单元格，否则后面的 & 会错位
            parts.append("")
            column += 1
            continue
        found = by_column.get(column)
        if found is None:
            parts.append("")
            column += 1
            continue
        colspan, rowspan, cell = found
        text = _mathify(cell.text)
        if fractions is not None:
            # 宽表：合并列用 p 宽（否则 multicolumn{l} 仍会把表撑出去）；合并行用
            # \parbox{\linewidth}——p 单元格里的 \linewidth 已经是该列宽，能正常换行。
            if colspan > 1:
                width = _fraction_latex(_span_fraction(fractions, column, colspan))
                if rowspan > 1:
                    text = (
                        rf"\multicolumn{{{colspan}}}{{p{{{width}}}}}"
                        rf"{{\multirow{{{rowspan}}}{{*}}{{\parbox{{\linewidth}}{{{text}}}}}}}"
                    )
                else:
                    text = rf"\multicolumn{{{colspan}}}{{p{{{width}}}}}{{{text}}}"
            elif rowspan > 1:
                text = rf"\multirow{{{rowspan}}}{{*}}{{\parbox{{\linewidth}}{{{text}}}}}"
        else:
            if colspan > 1 and rowspan > 1:
                text = rf"\multicolumn{{{colspan}}}{{l}}{{\multirow{{{rowspan}}}{{*}}{{{text}}}}}"
            elif colspan > 1:
                text = rf"\multicolumn{{{colspan}}}{{l}}{{{text}}}"
            elif rowspan > 1:
                text = rf"\multirow{{{rowspan}}}{{*}}{{{text}}}"
        parts.append(text)
        column += colspan
    return " & ".join(parts) + r" \\"


def table_html_to_latex(
    html: str | None,
    *,
    caption: str = "",
    label_raw: str | None = None,
    label_norm: str | None = None,
    table_id: str = "table",
    width: str = "auto",
) -> TableLatex:
    """把一张表的结构化 HTML 转成 LaTeX 片段。

    ``width="auto"``（默认）会在表格偏宽/单元格偏长时把 ``l`` 列换成按内容比例的
    ``p{…\\linewidth}`` 列，让长文本自动换行；``width="plain"`` 强制旧行为（全 ``l``）。
    """
    if not html or not html.strip():
        raise TableLatexError("这张表没有结构化 HTML（可能是图片表），无法导出 LaTeX")
    if width not in ("auto", "plain"):
        raise TableLatexError("width 只支持 auto 或 plain")
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    if not parser.rows:
        raise TableLatexError("表格 HTML 里没有可识别的行/单元格")
    rows = parser.rows
    placed_rows, spans, max_columns = _build_grid(rows)

    # M9.3c：先按内容估宽度，再决定用 l 列还是 p 列（宽表自动换行）。
    weights = _column_weights(placed_rows, max_columns)
    longest_cell = max((_display_units(cell.text) for row in rows for cell in row), default=0)
    longest_token = max(
        (len(part) for row in rows for cell in row for part in (cell.text or "").split()),
        default=0,
    )
    wrapped = width == "auto" and _needs_wrap(weights, longest_cell)
    fractions = (
        _column_fractions(weights, _column_min_fractions(placed_rows, max_columns))
        if wrapped
        else None
    )
    column_spec = _column_spec(max_columns, fractions)
    covered_by_row: list[set[int]] = []
    for row_index in range(len(rows)):
        covered: set[int] = set()
        for start_row, column, colspan, rowspan, _cell in spans:
            if start_row < row_index < start_row + rowspan:
                covered.update(range(column, column + colspan))
        covered_by_row.append(covered)

    # 表头行：第一行里 th 占多数就把它当表头，加一条 \hline
    header_rows = 0
    for index, cells in enumerate(rows):
        if cells and sum(1 for cell in cells if cell.header) > len(cells) / 2:
            header_rows = index + 1
        else:
            break
    if header_rows == 0 and rows and all(cell.header for cell in rows[0]):
        header_rows = 1

    caption_text, display_label, label_key = _caption_and_label(
        caption or "".join(parser.caption_parts), label_raw, label_norm, table_id
    )
    warnings = list(parser.warnings)
    if not caption_text:
        warnings.append("原表没有表题，未生成 \\caption（可在库里补 caption 后重新导出）")
    if any(cell.rowspan > 1 for row in rows for cell in row):
        warnings.append("含合并行，编译时需要 \\usepackage{multirow}")
    if any(cell.colspan > 1 for row in rows for cell in row):
        warnings.append("含合并列，已用 \\multicolumn 表示")
    if wrapped:
        warnings.append(
            "表格较宽，已按内容比例改成 p 列自动换行（表宽按 \\linewidth 计）；"
            "如需固定版式可显式传 width=plain"
        )
        if _table_may_overflow(placed_rows, max_columns):
            warnings.append(
                "估算该表在当前字号下仍可能放不下（短列/列数太多，p 列无法再压缩）；"
                "建议改用横排（sidewaystable）、缩小字号或拆表"
            )
        if longest_token > 30:
            warnings.append(
                f"存在超长英文串（{longest_token} 字符），p 列不会自动断词，可能仍会溢出；"
                "可手动改用 \\url 或 \\seqsplit"
            )

    lines = [rf"\begin{{tabular}}{{{column_spec}}}", r"\hline"]
    for index, placements in enumerate(placed_rows):
        lines.append(_row_latex(placements, covered_by_row[index], max_columns, fractions))
        if index + 1 == header_rows:
            lines.append(r"\hline")
    lines.extend([r"\hline", r"\end{tabular}"])
    tabular = "\n".join(lines)

    code_lines = [
        "% 由 LitBench 从结构化表导出（M9.3a/M9.3c）；表题/表号可在库里补全",
    ]
    if warnings:
        code_lines.append("% 注意：" + "；".join(warnings))
    code_lines.extend([r"\begin{table}[htbp]", r"\centering"])
    if caption_text:
        code_lines.append(rf"\caption{{{_mathify(caption_text)}}}")
    code_lines.extend([rf"\label{{{label_key}}}", tabular, r"\end{table}"])
    return TableLatex(
        tabular=tabular,
        code="\n".join(code_lines),
        caption=caption_text,
        label=display_label,
        label_key=label_key,
        columns=max_columns,
        rows=len(rows),
        warnings=warnings,
        wrapped=wrapped,
        column_spec=column_spec,
    )


__all__ = ["TableLatex", "TableLatexError", "table_html_to_latex"]
