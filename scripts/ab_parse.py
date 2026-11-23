# SPDX-License-Identifier: AGPL-3.0-or-later
"""解析后端 A/B：同一批 PDF 分别用 pdfium（轻量档）和 Docling（增强档）跑。

    python scripts/ab_parse.py --limit 3
    python scripts/ab_parse.py --engines docling --limit 1

指标口径与 P0 报告一致：
``coverage = 能定位到 bbox 的图号数 / 论文里出现的不同图号数``。
额外记录耗时与「caption 长度」，因为增强档的代价主要在时间上。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from litbench.labels import normalize_label  # noqa: E402


def _summarise(doc) -> dict:
    captions = [b for b in doc.blocks if b.type == "caption"]
    labels = {normalize_label(b.text) for b in captions}
    fig_labels = {label for label in labels if label and label.startswith("fig")}
    with_label = [f for f in doc.figures if f.label_raw]
    caption_chars = sum(len(f.caption) for f in doc.figures) / max(len(doc.figures), 1)
    return {
        "pages": doc.page_count,
        "blocks": len(doc.blocks),
        "tables": len(doc.tables),
        "figures": len(doc.figures),
        "captions": len(captions),
        "distinct": len(fig_labels),
        "with_label": len(with_label),
        "coverage": round(len(with_label) / len(fig_labels), 3) if fig_labels else 0.0,
        "caption_chars": round(caption_chars),
        "text_layer": doc.text_layer,
    }


async def run_engine(name: str, pdf: Path) -> dict:
    if name == "pdfium":
        from litbench.adapters import PdfiumParser

        parser = PdfiumParser(render_images=False)
    elif name == "docling":
        from litbench.adapters import DoclingParser

        parser = DoclingParser()
    else:
        raise SystemExit(f"unknown engine: {name}")

    started = time.time()
    try:
        doc = await parser.parse(pdf)
    except Exception as exc:  # noqa: BLE001
        return {"engine": name, "file": pdf.name, "error": f"{type(exc).__name__}: {exc}"}
    elapsed = time.time() - started
    row = {"engine": name, "file": pdf.name, "seconds": round(elapsed, 1)}
    row.update(_summarise(doc))
    return row


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3, help="跑前 N 篇")
    ap.add_argument("--engines", default="pdfium,docling")
    ap.add_argument("--samples", default=str(ROOT / ".data" / "samples"))
    args = ap.parse_args()

    pdfs = sorted(Path(args.samples).glob("*.pdf"))[: args.limit]
    if not pdfs:
        print("没有样本，先跑 scripts/fetch_samples.py")
        return 1

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    rows: list[dict] = []
    for pdf in pdfs:
        for engine in engines:
            print(f"… {engine:<8} {pdf.name[:46]}", flush=True)
            rows.append(await run_engine(engine, pdf))

    header = f"{'engine':<8}{'file':<44}{'pg':>4}{'blk':>6}{'tab':>4}{'fig':>4}{'cap':>5}{'dist':>5}{'cov':>6}{'capch':>6}{'sec':>7}"
    print("\n" + header)
    print("-" * len(header))
    for r in rows:
        if "error" in r:
            print(f"{r['engine']:<8}{r['file'][:42]:<44}  ERROR {r['error'][:60]}")
            continue
        print(
            f"{r['engine']:<8}{r['file'][:42]:<44}{r['pages']:>4}{r['blocks']:>6}"
            f"{r['tables']:>4}{r['figures']:>4}{r['captions']:>5}{r['distinct']:>5}"
            f"{r['coverage'] * 100:>5.0f}%{r['caption_chars']:>6}{r['seconds']:>7.1f}"
        )

    out = ROOT / ".data" / "ab_parse.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
