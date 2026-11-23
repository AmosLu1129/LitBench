# SPDX-License-Identifier: AGPL-3.0-or-later
r"""M9.3b：同一批 PDF 上 Docling tableformer vs MinerU standard 的表格结构一致性。

M9.3a 把表格导出成 LaTeX 之后，还剩一个问题没量：两个能出结构化表格的解析器
（Docling 的 TableFormer、MinerU 的 standard 档）对同一张表的结构还原差多少？
这个脚本只做评估，不改产品路径。

两段式用法（推荐，避免 Docling 和 MinerU 同时占内存）：

    # 1) 分别抽取，结果按文件 sha 缓存，断了可续跑
    python scripts/eval_table_structure.py --engines docling
    python scripts/eval_table_structure.py --engines mineru --tier standard

    # 2) 只算指标 + 写报告（不重新解析）
    python scripts/eval_table_structure.py --report-only

一条命令也可以（内部按篇顺序跑两个引擎）：

    python scripts/eval_table_structure.py --limit 10

产物（都在 .data/tmp，不入库）：

* ``table_compare_cache/<sha16>-<engine>[-<tier>].json``：每篇每引擎的原始表结构；
* ``eval_table_structure.json``：逐表指标明细；
* ``table_structure_report.md``：给人看的报告。

口径说明见 ``litbench/table_struct.py`` 文件头：指标只说明两个解析器是否互相
一致，不等于谁更接近真值；真值要人工标注（M9.2 的 bbox 集是图框，不是表格）。

路径坑（2026-11-15 实测）：docling 的 C++ 解析层打不开含中文的路径（docs/09 §11），
当仓库路径包含中文字符时（例如 ``D:\中文目录\...``），从 ASCII junction 跑即可：

    cd C:\litbench
    .venv\Scripts\python.exe scripts\eval_table_structure.py \
        --samples C:\litbench\.data\samples --engines docling
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "server"))

# Docling 的模型缓存：仓库里已经下过 505MB（docs/09 §11），不设 HF_HOME 会去
# 用户目录重下，或撞上不可写目录。这里只 setdefault，外部显式设置优先。
os.environ.setdefault("HF_HOME", str(ROOT / ".hf-cache"))

from litbench.table_struct import (  # noqa: E402
    compare_grids,
    match_tables,
    parse_table_grid,
    table_stats,
)

CACHE_DIR = ROOT / ".data" / "tmp" / "table_compare_cache"
DEFAULT_SAMPLES = ROOT / ".data" / "samples"
DEFAULT_OUT = ROOT / ".data" / "tmp" / "eval_table_structure.json"
DEFAULT_MD = ROOT / ".data" / "tmp" / "table_structure_report.md"


# ----------------------------------------------------------------------
def _sha16(pdf: Path) -> str:
    digest = hashlib.sha256()
    with pdf.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _cache_path(pdf: Path, engine: str, tier: str) -> Path:
    suffix = f"{engine}-{tier}" if engine == "mineru" else engine
    return CACHE_DIR / f"{_sha16(pdf)}-{suffix}.json"


def _table_payload(doc, seconds: float) -> dict:  # noqa: ANN001 — ParsedDocument
    tables = []
    for table in doc.tables:
        bbox = getattr(table, "bbox", None)
        tables.append(
            {
                "page": int(getattr(table, "page", 0) or 0),
                "bbox": [float(value) for value in bbox] if bbox else None,
                "caption": str(getattr(table, "caption", "") or ""),
                "html": str(getattr(table, "html", "") or ""),
                "quality": str(getattr(table, "quality", "") or ""),
            }
        )
    return {
        "engine": doc.engine,
        "version": str(getattr(doc, "engine_version", "") or ""),
        "pages": int(getattr(doc, "page_count", 0) or 0),
        "seconds": round(seconds, 1),
        "tables": tables,
    }


async def extract(pdf: Path, engine: str, tier: str) -> dict:
    if engine == "docling":
        from litbench.adapters import DoclingParser

        parser = DoclingParser(render_images=False)
    elif engine == "mineru":
        from litbench.adapters import MineruParser

        parser = MineruParser(render_images=False, tier=tier)
    else:
        raise SystemExit(f"未知引擎：{engine}")
    started = time.perf_counter()
    doc = await parser.parse(pdf)
    return _table_payload(doc, time.perf_counter() - started)


def _load_or_extract(pdf: Path, engine: str, tier: str, *, refresh: bool) -> dict:
    path = _cache_path(pdf, engine, tier)
    if path.is_file() and not refresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # 缓存坏了就重跑
    try:
        payload = asyncio.run(extract(pdf, engine, tier))
    except Exception as exc:  # noqa: BLE001 — 单个文件失败不该中断整批
        payload = {
            "engine": engine,
            "version": "",
            "pages": 0,
            "seconds": None,
            "tables": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    payload["file"] = pdf.name
    payload["tier"] = tier if engine == "mineru" else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


# ----------------------------------------------------------------------
def _prepare_tables(payload: dict) -> list[dict]:
    """把缓存的表记录补上 has_html / has_grid / stats，供匹配与报告使用。"""
    prepared = []
    for index, table in enumerate(payload.get("tables") or []):
        html = table.get("html") or ""
        grid = None
        error = ""
        try:
            grid = parse_table_grid(html)
        except Exception as exc:  # noqa: BLE001 — 超限/畸形表只标记，不中断
            error = f"{type(exc).__name__}: {exc}"
        record = {
            "index": index,
            "page": table.get("page") or 0,
            "bbox": table.get("bbox") or None,
            "caption": table.get("caption") or "",
            "has_html": bool(html.strip()),
            "is_html_table": "<table" in html.lower(),
            "has_grid": grid is not None,
            "grid_error": error,
            "stats": table_stats(grid) if grid is not None else None,
            "caption_chars": len(table.get("caption") or ""),
        }
        prepared.append(record)
    return prepared


def analyse_document(name: str, docling: dict, mineru: dict) -> dict:
    left = _prepare_tables(docling)
    right = _prepare_tables(mineru)
    pairs, unmatched_left, unmatched_right = match_tables(
        [(item["page"], tuple(item["bbox"]) if item["bbox"] else None) for item in left],
        [(item["page"], tuple(item["bbox"]) if item["bbox"] else None) for item in right],
    )

    pair_rows = []
    for left_index, right_index, score in pairs:
        left_item = left[left_index]
        right_item = right[right_index]
        row = {
            "docling_index": left_index,
            "mineru_index": right_index,
            "page": left_item["page"],
            "match_score": score,
            "docling": left_item["stats"],
            "mineru": right_item["stats"],
            "docling_caption": left_item["caption"],
            "mineru_caption": right_item["caption"],
            "comparable": bool(left_item["has_grid"] and right_item["has_grid"]),
        }
        if row["comparable"]:
            grids_left = parse_table_grid(docling["tables"][left_index]["html"])
            grids_right = parse_table_grid(mineru["tables"][right_index]["html"])
            # Docling 的 HTML 自带 <caption>，MinerU 的表题在块字段里；统一到 Grid.caption
            # 再比，否则 MinerU 永远「没表题」，caption_f1 会被系统性低估。
            if grids_left is not None and not grids_left.caption:
                grids_left.caption = str(left_item["caption"] or "")
            if grids_right is not None and not grids_right.caption:
                grids_right.caption = str(right_item["caption"] or "")
            if grids_left is not None and grids_right is not None:
                row["metrics"] = compare_grids(grids_left, grids_right)
        pair_rows.append(row)

    return {
        "file": name,
        "pages": max(docling.get("pages") or 0, mineru.get("pages") or 0),
        "docling": {
            "version": docling.get("version"),
            "seconds": docling.get("seconds"),
            "tables": len(left),
            "html_tables": sum(1 for item in left if item["is_html_table"]),
            "grids": sum(1 for item in left if item["has_grid"]),
            "error": docling.get("error"),
        },
        "mineru": {
            "version": mineru.get("version"),
            "seconds": mineru.get("seconds"),
            "tier": mineru.get("tier"),
            "tables": len(right),
            "html_tables": sum(1 for item in right if item["is_html_table"]),
            "grids": sum(1 for item in right if item["has_grid"]),
            "error": mineru.get("error"),
        },
        "matched": len(pairs),
        "unmatched_docling": [left[index] for index in unmatched_left],
        "unmatched_mineru": [right[index] for index in unmatched_right],
        "pairs": pair_rows,
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def aggregate(documents: list[dict]) -> dict:
    metric_pairs = [
        pair
        for document in documents
        for pair in document["pairs"]
        if pair.get("comparable")
    ]
    matched = sum(document["matched"] for document in documents)
    docling_tables = sum(document["docling"]["tables"] for document in documents)
    mineru_tables = sum(document["mineru"]["tables"] for document in documents)
    errors = [
        document["file"]
        for document in documents
        if document["docling"]["error"] or document["mineru"]["error"]
    ]
    return {
        "documents": len(documents),
        "documents_with_error": errors,
        "docling_tables": docling_tables,
        "mineru_tables": mineru_tables,
        "matched_pairs": matched,
        "docling_coverage": round(matched / docling_tables, 4) if docling_tables else None,
        "mineru_coverage": round(matched / mineru_tables, 4) if mineru_tables else None,
        "comparable_pairs": len(metric_pairs),
        "same_dims_rate": _mean(
            [1.0 if pair["metrics"]["same_dims"] else 0.0 for pair in metric_pairs]
        ),
        "same_spans_rate": _mean(
            [1.0 if pair["metrics"]["same_spans"] else 0.0 for pair in metric_pairs]
        ),
        "grid_iou": _mean([pair["metrics"]["grid_iou"] for pair in metric_pairs]),
        "cell_exact": _mean([pair["metrics"]["cell_exact"] for pair in metric_pairs]),
        "text_f1": _mean([pair["metrics"]["text_f1"] for pair in metric_pairs]),
        "perfect_grids": sum(
            1
            for pair in metric_pairs
            if pair["metrics"]["grid_iou"] == 1.0 and pair["metrics"]["cell_exact"] == 1.0
        ),
    }


# ----------------------------------------------------------------------
def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_markdown(
    documents: list[dict],
    summary: dict,
    *,
    tier: str,
    sample_dir: str,
    duplicates: list[str] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# 表格结构一致性：Docling tableformer vs MinerU {tier}（M9.3b）")
    lines.append("")
    lines.append(f"> 语料：`{sample_dir}` 下 {summary['documents']} 篇 PDF（逐篇全页解析）")
    lines.append(f"> MinerU 档位：`{tier}`；Docling：TableFormer 默认档（`do_table_structure=True`）")
    lines.append("> 复现：`python scripts/eval_table_structure.py --report-only`")
    if duplicates:
        lines.append("> 已按 sha256 去重（内容相同的不同文件名）：" + "、".join(duplicates))
    lines.append("")
    lines.append("## 方法")
    lines.append("")
    lines.append(f"1. 同一篇 PDF 分别用 Docling 和 MinerU `{tier}` 解析，取所有 `table` 块；")
    lines.append("2. 按「同页 + bbox 重叠」贪心配对（IoU 或交集/较小面积 ≥ 0.3），配不上的单列；")
    lines.append("3. 对配上的表：HTML → 网格（colspan/rowspan 展开），比较")
    lines.append("   行高宽、格位占用、单元格文本、3-gram shingle F1；")
    lines.append("4. 只统计两边都拿到结构化 HTML 的表；图片表/markdown 表不计入相似度。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 文档数 | {summary['documents']} |")
    lines.append(f"| Docling 表数 | {summary['docling_tables']} |")
    lines.append(f"| MinerU 表数 | {summary['mineru_tables']} |")
    lines.append(f"| 匹配上的表对 | {summary['matched_pairs']} |")
    lines.append(f"| Docling 表被匹配覆盖率 | {_fmt(summary['docling_coverage'], 3)} |")
    lines.append(f"| MinerU 表被匹配覆盖率 | {_fmt(summary['mineru_coverage'], 3)} |")
    lines.append(f"| 可比较表对（两边都有网格） | {summary['comparable_pairs']} |")
    lines.append(f"| 行高宽完全一致率 | {_fmt(summary['same_dims_rate'], 3)} |")
    lines.append(f"| 合并结构完全一致率 | {_fmt(summary['same_spans_rate'], 3)} |")
    lines.append(f"| 格位占用 IoU 均值 | {_fmt(summary['grid_iou'], 3)} |")
    lines.append(f"| 单元格文本完全一致率 | {_fmt(summary['cell_exact'], 3)} |")
    lines.append(f"| 3-gram 文本 F1 均值 | {_fmt(summary['text_f1'], 3)} |")
    lines.append(f"| 完全一致的表对 | {summary['perfect_grids']} |")
    lines.append("")
    if summary["documents_with_error"]:
        lines.append("> 解析失败的文档：" + "、".join(summary["documents_with_error"]))
        lines.append("")
    lines.append("## 分篇")
    lines.append("")
    lines.append(
        "| 文件 | 页 | Docling 表 | MinerU 表 | 匹配 | 可比较 | 行列一致 | 文本 F1 |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for document in documents:
        pairs = [pair for pair in document["pairs"] if pair.get("comparable")]
        same_dims = _mean(
            [1.0 if pair["metrics"]["same_dims"] else 0.0 for pair in pairs]
        )
        text_f1 = _mean([pair["metrics"]["text_f1"] for pair in pairs])
        lines.append(
            f"| `{document['file']}` | {document['pages']} | "
            f"{document['docling']['tables']} | {document['mineru']['tables']} | "
            f"{document['matched']} | {len(pairs)} | {_fmt(same_dims, 3)} | "
            f"{_fmt(text_f1, 3)} |"
        )
    lines.append("")
    lines.append("## 差异最大的表对")
    lines.append("")
    disagreements = [
        (document["file"], pair)
        for document in documents
        for pair in document["pairs"]
        if pair.get("comparable")
        and (pair["metrics"]["cell_exact"] < 0.5 or pair["metrics"]["grid_iou"] < 0.6)
    ]
    if not disagreements:
        lines.append("没有 `cell_exact<0.5` 或 `grid_iou<0.6` 的表对。")
    else:
        lines.append("| 文件 | 页 | 行高宽 Docling | 行高宽 MinerU | 格位 IoU | 文本一致 | 文本 F1 |")
        lines.append("| --- | ---: | --- | --- | ---: | ---: | ---: |")
        for name, pair in disagreements[:20]:
            left = pair["docling"]
            right = pair["mineru"]
            metrics = pair["metrics"]
            lines.append(
                f"| `{name}` | {pair['page']} | {left['height']}×{left['cols']} "
                f"(span {left['colspans']}/{left['rowspans']}) | "
                f"{right['height']}×{right['cols']} "
                f"(span {right['colspans']}/{right['rowspans']}) | "
                f"{_fmt(metrics['grid_iou'], 3)} | {_fmt(metrics['cell_exact'], 3)} | "
                f"{_fmt(metrics['text_f1'], 3)} |"
            )
        lines.append("")
    lines.append("## 配不上的表")
    lines.append("")
    unmatched = [
        (document["file"], "docling", item)
        for document in documents
        for item in document["unmatched_docling"]
    ] + [
        (document["file"], "mineru", item)
        for document in documents
        for item in document["unmatched_mineru"]
    ]
    if not unmatched:
        lines.append("所有表都配上了。")
    else:
        lines.append("| 文件 | 后端 | 页 | 行高宽 | 有 HTML |")
        lines.append("| --- | --- | ---: | --- | --- |")
        for name, backend, item in unmatched[:30]:
            stats = item["stats"] or {}
            dims = (
                f"{stats.get('height')}×{stats.get('cols')}" if stats else "—"
            )
            lines.append(
                f"| `{name}` | {backend} | {item['page']} | {dims} | "
                f"{'是' if item['is_html_table'] else '否'} |"
            )
        lines.append("")
    lines.append("## 结论与边界")
    lines.append("")
    lines.append("- 本报告只衡量**两个解析器是否互相一致**，不是准确率；两边同时错会得到高分。")
    lines.append("- bbox 配对假设两个后端给的表区域大致重合；分栏/跨页表可能配不上，已单列。")
    lines.append("- 语料是 arXiv 论文（多为三线表），不是扫描件，结论不能外推到扫描教材。")
    lines.append("- Docling 的 markdown 兜底、MinerU 的图片表不计入相似度，只在表数里体现。")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default=str(DEFAULT_SAMPLES), help="PDF 目录")
    parser.add_argument("--limit", type=int, default=10, help="最多跑几篇（按文件名排序）")
    parser.add_argument("--engines", default="docling,mineru", help="逗号分隔：docling,mineru")
    parser.add_argument("--tier", default="standard", help="MinerU 档位（默认 standard）")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制重解析")
    parser.add_argument("--report-only", action="store_true", help="不解析，只用缓存算指标")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="指标 JSON 路径")
    parser.add_argument("--md-out", default=str(DEFAULT_MD), help="Markdown 报告路径")
    args = parser.parse_args()

    pdfs = sorted(Path(args.samples).glob("*.pdf"))[: args.limit]
    if not pdfs:
        print(f"没有 PDF：{args.samples}，先跑 scripts/fetch_samples.py")
        return 1
    # 样本集里有两个文件名不同、内容相同的 PDF（arXiv 交叉分类），按 sha 去重，
    # 否则同一篇会被算两遍、把均值往它的结果上偏。
    unique: list[Path] = []
    duplicates: list[str] = []
    seen_sha: set[str] = set()
    for pdf in pdfs:
        digest = _sha16(pdf)
        if digest in seen_sha:
            duplicates.append(pdf.name)
            continue
        seen_sha.add(digest)
        unique.append(pdf)
    pdfs = unique
    engines = [item.strip() for item in args.engines.split(",") if item.strip()]
    for engine in engines:
        if engine not in {"docling", "mineru"}:
            raise SystemExit(f"未知引擎：{engine}")

    cache: dict[str, dict[str, dict]] = {}
    if not args.report_only:
        for pdf in pdfs:
            cache[pdf.name] = {}
            for engine in engines:
                print(f"… {engine:<8} {pdf.name[:52]}", flush=True)
                payload = _load_or_extract(pdf, engine, args.tier, refresh=args.refresh)
                cache[pdf.name][engine] = payload
                if payload.get("error"):
                    print(f"  ERROR {payload['error'][:100]}", flush=True)
                else:
                    print(
                        f"  {payload['seconds']}s, {len(payload.get('tables') or [])} tables",
                        flush=True,
                    )
                gc.collect()
        # 统一落一份，方便只跑了一个引擎时分两次合并
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "tier": args.tier,
                    "documents": {
                        name: {
                            engine: payload
                            for engine, payload in per_engine.items()
                        }
                        for name, per_engine in cache.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # 读缓存（全部或单引擎）
    cache = {}
    for pdf in pdfs:
        entry: dict[str, dict] = {}
        for engine in ("docling", "mineru"):
            path = _cache_path(pdf, engine, args.tier)
            if path.is_file():
                entry[engine] = json.loads(path.read_text(encoding="utf-8"))
        cache[pdf.name] = entry

    documents = []
    for pdf in pdfs:
        entry = cache.get(pdf.name) or {}
        if "docling" not in entry or "mineru" not in entry:
            missing = [engine for engine in ("docling", "mineru") if engine not in entry]
            print(f"跳过 {pdf.name}：缺 {', '.join(missing)} 的缓存")
            continue
        documents.append(analyse_document(pdf.name, entry["docling"], entry["mineru"]))

    if not documents:
        print("没有可分析的文档：先分别跑 --engines docling 和 --engines mineru。")
        return 1

    summary = aggregate(documents)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "tier": args.tier,
                "duplicates": duplicates,
                "summary": summary,
                "documents": documents,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report = render_markdown(
        documents,
        summary,
        tier=args.tier,
        sample_dir=str(Path(args.samples)),
        duplicates=duplicates,
    )
    md_path = Path(args.md_out)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report, encoding="utf-8")

    print()
    print(
        f"{'file':<44}{'doc':>5}{'min':>5}{'pair':>6}{'cmp':>5}"
        f"{'dims':>7}{'iou':>7}{'exact':>7}{'f1':>7}"
    )
    for document in documents:
        comparable = [pair for pair in document["pairs"] if pair.get("comparable")]
        dims = _mean([1.0 if pair["metrics"]["same_dims"] else 0.0 for pair in comparable])
        iou = _mean([pair["metrics"]["grid_iou"] for pair in comparable])
        exact = _mean([pair["metrics"]["cell_exact"] for pair in comparable])
        f1 = _mean([pair["metrics"]["text_f1"] for pair in comparable])
        print(
            f"{document['file'][:42]:<44}{document['docling']['tables']:>5}"
            f"{document['mineru']['tables']:>5}{document['matched']:>6}"
            f"{len(comparable):>5}{_fmt(dims):>7}{_fmt(iou):>7}"
            f"{_fmt(exact):>7}{_fmt(f1):>7}"
        )
    print()
    print(f"匹配 {summary['matched_pairs']} 对，可比较 {summary['comparable_pairs']} 对")
    print(
        f"行列一致率 {_fmt(summary['same_dims_rate'])}，"
        f"格位 IoU {_fmt(summary['grid_iou'])}，"
        f"单元格一致 {_fmt(summary['cell_exact'])}，"
        f"文本 F1 {_fmt(summary['text_f1'])}"
    )
    print(f"明细 -> {out_path}")
    print(f"报告 -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
