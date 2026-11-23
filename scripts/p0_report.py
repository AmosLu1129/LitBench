# SPDX-License-Identifier: AGPL-3.0-or-later
"""P0 验证：在样本集上跑解析，输出「图号可定位率」等指标。

    python scripts/p0_report.py

指标口径（先在脚本里说清楚，免得自己感动自己）：

* ``caps``    —— 以「Fig. 3 / 图3 / Table 2」开头的 block 数
* ``dist``    —— 这些 caption 归一化后的**不同**图号数（≈ 论文里带编号的图有几张）
* ``figs``    —— 成功定位到 bbox 的图数（以 caption 为锚点长出来）
* ``cov``     —— figs / dist，**论文里的 Fig. N，我们能不能找到那张图**

cov 是唯一对用户有意义的数：用户按「Fig. 3」检索时，要么命中那张图，要么就是没有。
表题（Table N）不计入——表结构识别留给增强档。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from litbench.adapters.pdfium_parser import PdfiumParser  # noqa: E402
from litbench.labels import normalize_label  # noqa: E402


async def main() -> int:
    samples = ROOT / ".data" / "samples"
    pdfs = sorted(samples.glob("*.pdf"))
    if not pdfs:
        print("没有样本。先跑：python scripts/fetch_samples.py")
        return 1

    parser = PdfiumParser(render_images=False)
    rows: list[dict] = []

    for pdf in pdfs:
        try:
            doc = await parser.parse(pdf)
        except Exception as exc:  # noqa: BLE001
            print(f"!! {pdf.name}: {type(exc).__name__}: {exc}")
            rows.append({"file": pdf.name, "error": f"{type(exc).__name__}: {exc}"})
            continue

        fig_caps = [b for b in doc.blocks if b.type == "caption"]
        labels = {normalize_label(b.text) for b in fig_caps}
        distinct = {label for label in labels if label and label.startswith("fig")}
        pages = max(doc.page_count, 1)
        figs = len(doc.figures)

        rows.append(
            {
                "file": pdf.name,
                "pages": doc.page_count,
                "blocks": len(doc.blocks),
                "caps": len(fig_caps),
                "dist": len(distinct),
                "figs": figs,
                "cov": round(figs / len(distinct), 3) if distinct else 0.0,
                "figs_per_page": round(figs / pages, 2),
                "text_layer": doc.text_layer,
            }
        )

    ok = [r for r in rows if "error" not in r]
    header = f"{'file':<46}{'pg':>4}{'blk':>7}{'cap':>5}{'dist':>5}{'figs':>5}{'cov':>7}{'f/pg':>6}"
    print(header)
    print("-" * len(header))
    for r in ok:
        print(
            f"{r['file'][:44]:<46}{r['pages']:>4}{r['blocks']:>7}{r['caps']:>5}"
            f"{r['dist']:>5}{r['figs']:>5}{r['cov'] * 100:>6.0f}%{r['figs_per_page']:>6.2f}"
        )

    if ok:
        dist = sum(r["dist"] for r in ok)
        figs = sum(r["figs"] for r in ok)
        print("-" * len(header))
        print(
            f"{'TOTAL':<46}{sum(r['pages'] for r in ok):>4}{sum(r['blocks'] for r in ok):>7}"
            f"{sum(r['caps'] for r in ok):>5}{dist:>5}{figs:>5}"
            f"{figs / dist * 100 if dist else 0:>6.0f}%"
            f"{figs / max(sum(r['pages'] for r in ok), 1):>6.2f}"
        )

    out = samples / "p0_report.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
