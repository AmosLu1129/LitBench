# SPDX-License-Identifier: AGPL-3.0-or-later
"""Docling 表格导出单测（不需要装 docling）。

2026-11-13 真实语料暴露的回归：docling 2.150 的 ``TableItem.export_to_html()``
在**不传 doc** 时不再抛异常，而是返回空串。旧实现只 catch 异常，
于是所有结构化表都以 ``html=""`` 入库（19/19 张空表）。
这里用假 item 锁定「必须传 doc」「空串要兜底到 markdown」这两条行为。
"""

from __future__ import annotations

from litbench.adapters.docling_adapter import _table_html


class FakeTable:
    """模拟 docling TableItem 的两个导出方法。"""

    def __init__(self, html: str = "", markdown: str = "", raise_html: bool = False) -> None:
        self.html = html
        self.markdown = markdown
        self.raise_html = raise_html
        self.calls: list[tuple[str, object]] = []

    def export_to_html(self, doc=None, add_caption: bool = True) -> str:
        self.calls.append(("html", doc))
        if self.raise_html:
            raise RuntimeError("boom")
        # 关键回归点：不传 doc 时返回空串（而不是抛异常）
        return self.html if doc is not None else ""

    def export_to_markdown(self, doc=None) -> str:
        self.calls.append(("markdown", doc))
        return self.markdown if doc is not None else ""


def test_table_html_passes_doc_and_returns_html() -> None:
    doc = object()
    item = FakeTable(html="<table><tr><td>42</td></tr></table>", markdown="| 42 |")
    assert _table_html(item, doc) == "<table><tr><td>42</td></tr></table>"
    assert item.calls == [("html", doc)]


def test_table_html_falls_back_to_markdown_when_html_empty() -> None:
    doc = object()
    item = FakeTable(html="", markdown="| inlet | outlet |")
    assert _table_html(item, doc) == "| inlet | outlet |"


def test_table_html_falls_back_when_html_raises() -> None:
    doc = object()
    item = FakeTable(html="<table/>", markdown="| a |", raise_html=True)
    assert _table_html(item, doc) == "| a |"


def test_table_html_returns_none_when_everything_empty() -> None:
    doc = object()
    item = FakeTable(html="", markdown="")
    assert _table_html(item, doc) is None
