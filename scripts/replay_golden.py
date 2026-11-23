# SPDX-License-Identifier: AGPL-3.0-or-later
"""按 golden set 重放解析器，算回归指标。

    python scripts/replay_golden.py .data/golden.json
    python scripts/replay_golden.py .data/golden.json --backend pdfium   # 用基线档重放
    python scripts/replay_golden.py .data/golden.json --iou 0.4 -o .data/report.json

指标怎么看：

* ``recall``       —— 人工说「定位正确」的图，重放后还有多少比例仍然锚定且 IoU ≥ 阈值。
                      **解析器改动前后比这个数，而不是比一次性的 68%。**
* ``wrong_still``  —— 人工说「定位错了」的图，重放后是否还落在同一个错框上。越少越好。
* ``wrong_fixed``  —— 已经不再落回错框（换框或干脆没有）。
* ``errors``       —— 样本 PDF 缺失/解析失败。它们不算分，但必须看得见。

``--backend`` 是这次最有用的开关：golden 由现役后端（如 docling）产出，
用 ``--backend pdfium`` 重放，就得到「不升级的代价」——这就是按需升级的收益证明。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("STORAGE_DIR", str(ROOT / ".data"))
sys.path.insert(0, str(ROOT / "server"))

from litbench.golden import replay  # noqa: E402

VERDICT_LABEL = {
    "pass": "通过",
    "regression": "回退",
    "no_box": "未锚定",
    "fixed": "已修正",
    "still_wrong": "仍是错框",
    "error": "错误",
}


async def main() -> int:
    ap = argparse.ArgumentParser(description="回放 golden set")
    ap.add_argument("golden", type=Path, help="export_golden.py 产出的 JSON")
    ap.add_argument("--backend", help="强制用某个后端重放（默认用每条记录的 parser）")
    ap.add_argument("--iou", type=float, default=0.5, help="普通条目的 bbox IoU 阈值，默认 0.5")
    ap.add_argument(
        "--corrected-iou",
        type=float,
        default=0.9,
        help="人工给过修正框的条目阈值，默认 0.9（人工精确到框，不能用 0.5 放水）",
    )
    ap.add_argument("-o", "--out", type=Path, help="把完整报告写成 JSON")
    ap.add_argument("--limit", type=int, help="只回放前 N 条")
    args = ap.parse_args()

    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    if golden.get("item_count", 0) == 0:
        print("golden 是空的：先复核几张图再导出。", file=sys.stderr)
        return 1

    report = await replay(
        golden,
        backend=args.backend,
        iou_threshold=args.iou,
        corrected_threshold=args.corrected_iou,
        limit=args.limit,
    )
    summary = report["summary"]

    print(
        f"backend = {report['backend']}    IoU ≥ {report['iou_threshold']}"
        f"（修正框条目 ≥ {report['corrected_threshold']}）"
    )
    print(
        f"条目 {summary['total']}（期望有框={summary['positive_total']}"
        f" / 期望没框={summary['negative_total']} / 错误={summary['errors']}）"
    )
    print(
        f"recall = {summary['recall'] if summary['recall'] is not None else '-'}"
        f"   （{summary['positive_passed']}/{summary['positive_total']}"
        f" 条「人工说这里该有这张图」仍然对得上）"
    )
    print(
        f"期望没框: 仍是错框 {summary['negative_still_wrong']}"
        f" / 已修正 {summary['negative_fixed']} / 共 {summary['negative_total']}"
    )
    if summary["mean_iou_positive"] is not None:
        print(f"正样本平均 IoU = {summary['mean_iou_positive']}")
    if summary["errors"]:
        print(f"!! {summary['errors']} 条样本解析失败（不计分，看一眼原因）")
        if any("filename does not exists" in str(r.get("error", "")) for r in report["results"]):
            print(
                "提示：Docling 打不开含非 ASCII 的路径（见 docs/09-现状交接.md §11）。"
                "从目录联接启动本脚本再试：Set-Location C:\\litbench",
                file=sys.stderr,
            )

    bad = [r for r in report["results"] if r["verdict"] in ("regression", "still_wrong", "error")]
    if bad:
        print("\n需要看的条目：")
        for r in bad[:15]:
            mark = VERDICT_LABEL.get(r["verdict"], r["verdict"])
            extra = f"  {r['error']}" if r.get("error") else f"  IoU={r['iou']}"
            print(f"  [{mark}] {r.get('cite_key')} · {r.get('label_norm')} · {r.get('paper_title')}{extra}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整报告：{args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
