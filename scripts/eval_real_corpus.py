# SPDX-License-Identifier: AGPL-3.0-or-later
r"""真实课本语料的可信基线（每次改解析器都跑这个）。

    C:\litbench\.venv\Scripts\python.exe scripts\eval_real_corpus.py \
        --out docs\13-真实语料基线.md

为什么要有它：arXiv 那 10 篇是「最规范版面」，课本才是目标场景。`docs/05` 的图号可定位率
只在 arXiv 上成立；本脚本对 `<LITBENCH_CORPUS_DIR>` 的 5 份真实课本切片跑完整解析链路
（pdfium → 判弱升级 Docling / 无文字层转 MinerU OCR），输出每份的 blocks / figures /
图注-图配对率 / 耗时，写进 `docs/13-真实语料基线.md`。

口径说清楚（否则数字会骗人）：

* ``figures``：pdfium 是「矢量绘图基元聚类」，Docling/MinerU 是「版面模型判定的图块」，
  **跨后端不能直接比精度**，只能和「同一后端的上一次基线」比。
* ``图注-图配对率``：图注文本里出现的图号里，有多少比例真的定位到了 bbox
  （``labels.py`` 的归一化口径）。无人工真值，所以只作相对回归，不作准确率。
* 两本有文字层的书（01/02）跑 pdfium 后会按 `escalate.py` 的判据自动升级到 Docling；
  三本无文字层的（03/04/05）走 MinerU flash，和 worker 的产品行为一致。
* 语料是**只读**的。Docling / MinerU 的解析层打不开非 ASCII 路径，所以先复制到
  ASCII 工作目录（``--workdir``）再解析。

退出码：0 = 全部书跑完；1 = 有书失败（失败原因写进报告，不静默）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

# 本机 Docling 的模型缓存在 ASCII 目录（见 docs/09 §11）。不设 HF_HOME 时
# transformers 会去连 HuggingFace 找 snapshot，离线/超时就直接失败。
_HF_CACHE = Path(r"C:\litbench\.hf-cache")
if _HF_CACHE.is_dir():
    os.environ.setdefault("HF_HOME", str(_HF_CACHE))
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from litbench.adapters import get_parser  # noqa: E402
from litbench.escalate import can_escalate, find_weakness  # noqa: E402
from litbench.labels import normalize_label  # noqa: E402

DEFAULT_CORPUS = Path(os.environ.get("LITBENCH_CORPUS_DIR", str(ROOT / ".data" / "corpus")))
DEFAULT_WORKDIR = Path(r"C:\litbench\.data\real-corpus")
# 复制到 ASCII 目录时用短名（语料文件名本身带中文）
ASCII_NAMES = {
    "01": "01-md.pdf",
    "02": "02-trans.pdf",
    "03": "03-act.pdf",
    "04": "04-comb.pdf",
    "05": "05-eet.pdf",
}


def stage(src: Path, workdir: Path, book: str) -> Path:
    """复制到 ASCII 工作目录（只读语料本身不动）。"""
    dst = workdir / "input" / ASCII_NAMES.get(book, f"{book}.pdf")
    if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return dst


def fig_labels(doc) -> set[str]:
    """图注文本里出现过的图号（含 figure 自带 caption 和独立 caption 块）。"""
    found: set[str] = set()
    for block in doc.blocks:
        if block.type != "caption":
            continue
        norm = normalize_label(block.text or "")
        if norm and norm.startswith("fig"):
            found.add(norm)
    for fig in doc.figures:
        norm = normalize_label(fig.caption or "")
        if norm and norm.startswith("fig"):
            found.add(norm)
    return found


def anchored_labels(doc) -> set[str]:
    found: set[str] = set()
    for fig in doc.figures:
        norm = normalize_label(fig.label_raw or "")
        if norm and norm.startswith("fig"):
            found.add(norm)
    return found


def summarize(doc, *, chain: str, seconds: float) -> dict:
    cap_labels = fig_labels(doc)
    anchored = anchored_labels(doc)
    paired = len(cap_labels & anchored) / len(cap_labels) if cap_labels else None
    return {
        "chain": chain,
        "seconds": round(seconds, 1),
        "pages": doc.page_count,
        "text_layer": bool(doc.text_layer),
        "blocks": len(doc.blocks),
        "caption_blocks": sum(1 for b in doc.blocks if b.type == "caption"),
        "figures": len(doc.figures),
        "figures_with_caption": sum(1 for f in doc.figures if f.caption),
        "figures_with_label": sum(1 for f in doc.figures if f.label_raw),
        "distinct_labels": len(anchored),
        "caption_labels": len(cap_labels),
        "caption_pair_rate": round(paired, 3) if paired is not None else None,
        "tables": len(doc.tables),
    }


async def eval_book(src: Path, workdir: Path, book: str) -> dict:
    """跑产品同款链路：pdfium → 判弱升级 Docling / 无文字层转 MinerU。"""
    t0 = time.time()
    staged = stage(src, workdir, book)
    chain = ["pdfium"]
    doc = await get_parser("pdfium", render_images=True).parse(staged)
    if not doc.text_layer:
        chain.append("mineru(ocr)")
        doc = await get_parser("mineru", render_images=True, ocr=True).parse(staged)
    else:
        weakness = find_weakness(doc)
        ok, _why = can_escalate("docling", "pdfium")
        if weakness is not None and ok:
            chain.append(f"docling({weakness.code})")
            doc = await get_parser("docling", render_images=True).parse(staged)
    return summarize(doc, chain=" → ".join(chain), seconds=time.time() - t0)


def fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def write_markdown(rows: list[dict], out: Path, *, corpus: Path) -> None:
    ok_rows = [r for r in rows if r.get("metrics")]
    lines = [
        "# 真实课本语料基线（自动生成）",
        "",
        "> 由 `scripts/eval_real_corpus.py` 生成，**每次改解析器都要重跑**，和上一版基线对比。",
        f"> 语料：`{corpus}`（只读，5 份真实课本切片 / 合计 200 页）。",
        "> 链路：pdfium 起步 → 判弱自动升级 Docling / 无文字层转 MinerU flash OCR（与 worker 产品行为一致）。",
        "",
        "## 结果",
        "",
        "| 书 | 页 | 链路 | blocks | 图注块 | **图** | 有图注 | 有图号 | 不同图号 | 图注-图配对率 | 表 | 耗时 |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['book']} | — | 失败 | — | — | — | — | — | — | — | — | — |")
            continue
        m = r["metrics"]
        lines.append(
            "| {book} | {pages} | {chain} | {blocks} | {caption_blocks} | **{figures}** | "
            "{figures_with_caption} | {figures_with_label} | {distinct_labels} | {pair} | {tables} | {seconds} s |".format(
                book=r["book"],
                pages=m["pages"],
                chain=m["chain"],
                blocks=m["blocks"],
                caption_blocks=m["caption_blocks"],
                figures=m["figures"],
                figures_with_caption=m["figures_with_caption"],
                figures_with_label=m["figures_with_label"],
                distinct_labels=m["distinct_labels"],
                pair=fmt_rate(m["caption_pair_rate"]),
                tables=m["tables"],
                seconds=m["seconds"],
            )
        )
    fails = [r for r in rows if r.get("error")]
    if fails:
        lines += ["", "## 失败", ""]
        lines += [f"- {r['book']}：`{r['error']}`" for r in fails]
    total = sum(r.get("metrics", {}).get("seconds", 0) for r in ok_rows)
    lines += [
        "",
        "## 口径与读法（别把数字当准确率）",
        "",
        "1. `figures` 在不同后端不是同一种东西：pdfium 是矢量绘图基元聚类，Docling/MinerU 是版面模型",
        "   判定的图块。**只和同一后端的上一版基线比**，不要跨后端比大小。",
        "2. `图注-图配对率` = 图注文本里出现的图号中，真的定位到 bbox 的比例（无人工真值，只作回归）。",
        "3. `有图注 / 有图号` 是 figure 行上的字段；一图多子图会被拆成多行，同号多行是正常的。",
        "4. 无文字层 → MinerU flash：图注在 image/chart 块的 `*_caption` 子块里，适配器已取出；",
        "   flash 的表格只有 markdown 文本、无结构化 HTML。",
        "",
        "## 复现",
        "",
        "```powershell",
        "# 必须用 ASCII venv + ASCII 工作目录（Docling 限制）",
        "Set-Location C:\\litbench",
        "C:\\litbench\\.venv\\Scripts\\python.exe C:\\litbench\\scripts\\eval_real_corpus.py",
        "```",
        "",
        f"> 总耗时：**{total:.1f} s**（{len(ok_rows)} 本逐本相加）。",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> int:
    ap = argparse.ArgumentParser(description="真实课本语料基线（pdfium → Docling/MinerU 链路）")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    ap.add_argument("--books", default="", help="逗号分隔的编号，默认全部（01,02,03,04,05）")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "13-真实语料基线.md")
    ap.add_argument("--json", type=Path, default=None, help="另存机器可读结果")
    args = ap.parse_args()

    books = [b.strip() for b in args.books.split(",") if b.strip()] or sorted(ASCII_NAMES)
    rows: list[dict] = []
    for book in books:
        matches = sorted(args.corpus.glob(f"{book}-*.pdf"))
        if not matches:
            rows.append({"book": book, "error": f"语料里没有 {book}-*.pdf"})
            print(f"[{book}] 缺语料", flush=True)
            continue
        try:
            metrics = await eval_book(matches[0], args.workdir, book)
            rows.append({"book": matches[0].stem, "metrics": metrics})
            m = metrics
            print(
                f"[{book}] {m['chain']:<24} blocks={m['blocks']:<5} figs={m['figures']:<4} "
                f"label={m['figures_with_label']:<4} pair={fmt_rate(m['caption_pair_rate']):<4} {m['seconds']}s",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 — 单本失败不能吞掉整份报告
            rows.append({"book": book, "error": f"{type(exc).__name__}: {exc}"})
            print(f"[{book}] 失败：{type(exc).__name__}: {exc}", flush=True)

    write_markdown(rows, args.out, corpus=args.corpus)
    payload = {"corpus": str(args.corpus), "books": rows}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写好了：{args.out}")
    return 0 if not any(r.get("error") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
