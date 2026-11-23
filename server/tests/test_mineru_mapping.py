# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU 适配器的映射测试（不跑 CLI）。

MinerU 是 AGPL，只能走独立子进程；所以这里测的是「拿到 CLI 产出的 middle_json
之后怎么映射成本项目结构」。用合成的 docvortex.middle JSON，字段按官方输出协议
（`schema=docvortex.middle` v2，`page_idx` 从 0 开始）。
"""

from __future__ import annotations

import pytest

from litbench.adapters.base import ParserError
from litbench.adapters.mineru_adapter import to_parsed_document


def build_middle() -> dict:
    return {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {"file_suffix": "pdf", "producer": {"name": "mineru", "version": "4.2.0"}},
        "extensions": {
            "docvortex_layout": {
                "version": 1,
                "pages": [{"page_idx": 0, "width_pt": 469.0, "height_pt": 649.0}],
            }
        },
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "title",
                        "index": 0,
                        "bbox": [40.0, 30.0, 420.0, 60.0],
                        "content": [{"type": "text", "content": "第 4 章 控制系统的频域分析"}],
                    },
                    {
                        "type": "text",
                        "index": 1,
                        "bbox": [40.0, 80.0, 420.0, 200.0],
                        "content": [
                            {"type": "text", "content": "频率特性是系统对正弦输入的稳态响应。" * 6},
                        ],
                    },
                    {
                        "type": "image",
                        "index": 2,
                        "bbox": [60.0, 220.0, 400.0, 380.0],
                        "content": [{"type": "text", "content": ""}],
                    },
                    {
                        "type": "caption",
                        "index": 3,
                        "bbox": [120.0, 385.0, 340.0, 405.0],
                        "content": [{"type": "text", "content": "图 4-2 频率特性的几何表示"}],
                    },
                    {
                        "type": "table",
                        "index": 4,
                        "bbox": [60.0, 430.0, 400.0, 520.0],
                        "html": "<table><tr><td>ω</td><td>1</td></tr></table>",
                        "content": [{"type": "text", "content": "表 4-1 频率特性数据"}],
                    },
                ],
            }
        ],
    }


def test_maps_blocks_figures_tables() -> None:
    doc = to_parsed_document(build_middle(), render_images=False)

    assert doc.engine == "mineru"
    assert doc.page_count == 1
    assert doc.text_layer is True

    types = [b.type for b in doc.blocks]
    assert "title" in types and "text" in types and "caption" in types

    assert len(doc.figures) == 1
    fig = doc.figures[0]
    assert fig.page == 1
    assert fig.bbox == [60.0, 220.0, 400.0, 380.0]
    # 图注按几何绑上，图号从图注里抽出来
    assert fig.caption.startswith("图 4-2")
    assert fig.label_raw == "图 4-2"

    assert len(doc.tables) == 1
    assert doc.tables[0].page == 1
    assert "table" in (doc.tables[0].html or "")


def test_nested_figure_caption_is_mapped() -> None:
    """真实 MinerU 把图注挂在图块自己的 content 里（image_caption 子块）。

    不读子块 = 整本书的图注全丢（整章 104 张图只有 6 张能对上号就是这么来的）。
    """
    data = build_middle()
    image = next(b for b in data["pages"][0]["blocks"] if b["type"] == "image")
    image["content"] = [
        {"type": "image_body", "index": 2, "bbox": [60.0, 220.0, 400.0, 380.0], "content": ""},
        {
            "type": "image_caption",
            "index": 3,
            "bbox": [120.0, 385.0, 340.0, 405.0],
            "content": [{"type": "text", "content": "图 4.2.1 G(jω) 的极坐标图"}],
        },
    ]
    doc = to_parsed_document(data, render_images=False)
    fig = doc.figures[0]
    assert fig.caption == "图 4.2.1 G(jω) 的极坐标图"
    assert fig.label_raw == "图 4.2.1"


def test_nested_table_caption_and_body() -> None:
    """表题/表体也是嵌在 table 块里的子块，不能拿整段 flatten 文本当 caption。"""
    data = build_middle()
    table = next(b for b in data["pages"][0]["blocks"] if b["type"] == "table")
    table.pop("html")
    table["content"] = [
        {
            "type": "table_caption",
            "index": 4,
            "bbox": [60.0, 420.0, 400.0, 440.0],
            "content": [{"type": "text", "content": "表 4.4.1 二阶系统阻尼比对照表"}],
        },
        {
            "type": "table_body",
            "index": 5,
            "bbox": [60.0, 440.0, 400.0, 520.0],
            "content": "ζ  0.1  0.2\nσ  10  20",
        },
    ]
    doc = to_parsed_document(data, render_images=False)
    item = doc.tables[0]
    assert item.caption == "表 4.4.1 二阶系统阻尼比对照表"
    assert item.quality == "structured"


def test_short_text_means_no_text_layer() -> None:
    data = build_middle()
    for block in data["pages"][0]["blocks"]:
        block["content"] = [{"type": "text", "content": "短"}]
    doc = to_parsed_document(data, render_images=False)
    assert doc.text_layer is False


def test_rejects_unexpected_payload() -> None:
    with pytest.raises(ParserError):
        to_parsed_document({"foo": "bar"}, render_images=False)


def test_bbox_tolerates_alternative_shapes() -> None:
    """不同版本的字段名不一样：bbox / geom.bbox / position.bbox 都要能读。"""
    data = build_middle()
    blocks = data["pages"][0]["blocks"]
    blocks[0].pop("bbox")
    blocks[0]["geom"] = {"bbox": [1.0, 2.0, 3.0, 4.0]}
    blocks[1].pop("bbox")
    blocks[1]["position"] = {"bbox": [5.0, 6.0, 7.0, 8.0]}
    doc = to_parsed_document(data, render_images=False)
    by_text = {b.text: b.bbox for b in doc.blocks}
    title_key = next(k for k in by_text if k.startswith("第 4 章"))
    body_key = next(k for k in by_text if k.startswith("频率特性是系统"))
    assert by_text[title_key] == [1.0, 2.0, 3.0, 4.0]
    assert by_text[body_key] == [5.0, 6.0, 7.0, 8.0]

def test_parser_tier_override() -> None:
    """M9.3b 评估要显式跑 standard 档；默认仍跟配置（生产是 flash）。"""
    from litbench.adapters.mineru_adapter import MineruParser
    from litbench.config import settings

    assert MineruParser(render_images=False).tier == settings.mineru_tier
    assert MineruParser(render_images=False, tier="standard").tier == "standard"
