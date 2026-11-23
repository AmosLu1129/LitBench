# SPDX-License-Identifier: AGPL-3.0-or-later
"""提案阈值校准（M3 收尾）：用「人已经判过」的提案反推自动接受阈值。

为什么要它：docs/15 §12.18.5 / §12.19.2 留了两笔账——

* figure.group 之外，图号/图注/框三类提案的自动接受阈值目前是拍的（0.95）；
* M3c 的验收口径「自动建议与人判一致率 ≥ 90% / 误改率 < 2%」需要 50 张人工复核框，
  当前语料没有人工 bbox 标注，一直量不出来。

这个脚本不 pretend 有数据：它把库里 **已由人决策** 的提案当 ground truth，
按置信度阈值模拟「如果当时开自动接受会怎样」，输出 precision / coverage，
并在样本不足时明确说不足。每次人审后重跑，阈值就会越来越有依据。

用法::

    python scripts/calibrate_bbox.py                     # 默认 figure_bbox
    python scripts/calibrate_bbox.py --kind figure_review
    python scripts/calibrate_bbox.py --export-golden .data/golden/bbox-proposals.json

退出码：0（有结论或数据不足都算正常跑完）；2（参数/库结构错误）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 自动接受候选阈值（与 settings.auto_accept_threshold 同区间；0.90 是 M2b 校准值）
THRESHOLDS = (0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98)
# 「人判通过」的提案状态：accepted；rolled_back 说明人后来反悔 → 算误改
POSITIVE = {"accepted"}
NEGATIVE = {"rejected", "rolled_back"}


def _rows(db: Path, kind: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT p.id, p.kind, p.status, p.confidence, p.decided_by, p.auto_accepted,
                   p.proposed_json, p.before_json, p.figure_id, p.paper_version_id,
                   f.label_raw, f.label_norm, f.page,
                   pv.paper_id
            FROM figure_proposal p
            LEFT JOIN figure f ON f.id = p.figure_id
            LEFT JOIN paper_version pv ON pv.id = p.paper_version_id
            WHERE p.kind = ?
            ORDER BY p.created_at DESC
            """,
            (kind,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _bucket(confidence: float) -> str:
    if confidence >= 0.97:
        return "≥0.97"
    if confidence >= 0.95:
        return "0.95–0.96"
    if confidence >= 0.92:
        return "0.92–0.94"
    return "<0.92"


def _simulate(decided: list[dict]) -> list[dict]:
    """按阈值模拟：conf≥T 自动接受；人后来回滚（rolled_back）算误改。"""
    out = []
    for threshold in THRESHOLDS:
        predicted = [row for row in decided if float(row["confidence"] or 0) >= threshold]
        if not predicted:
            out.append(
                {
                    "threshold": threshold,
                    "predicted": 0,
                    "correct": 0,
                    "wrong": 0,
                    "precision": None,
                    "coverage": 0.0,
                }
            )
            continue
        correct = sum(1 for row in predicted if row["status"] in POSITIVE)
        wrong = len(predicted) - correct
        out.append(
            {
                "threshold": threshold,
                "predicted": len(predicted),
                "correct": correct,
                "wrong": wrong,
                "precision": round(correct / len(predicted), 4),
                "coverage": round(len(predicted) / max(len(decided), 1), 4),
            }
        )
    return out


def _recommend(simulated: list[dict]) -> dict:
    """推荐阈值：precision ≥ 0.95 前提下覆盖最多；都不满足就明说样本不足。"""
    eligible = [
        item
        for item in simulated
        if item["precision"] is not None and item["precision"] >= 0.95 and item["predicted"] >= 3
    ]
    if not eligible:
        return {
            "threshold": None,
            "reason": "样本不足或没有任何阈值达到 precision≥0.95 且覆盖≥3 条；保持默认 0.95，继续攒人工决策",
        }
    best = max(eligible, key=lambda item: (item["coverage"], item["threshold"]))
    return {
        "threshold": best["threshold"],
        "reason": f"在 precision≥0.95 的阈值里覆盖率最高（{best['coverage']:.0%}，{best['predicted']} 条）",
    }


def _export_golden(path: Path, kind: str, rows: list[dict]) -> int:
    decided = [row for row in rows if row["status"] in POSITIVE | NEGATIVE]
    payload = {
        "schema": "litbench.proposal-golden/v1",
        "kind": kind,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "item_count": len(decided),
        "note": "人已决策的提案；proposed/before 保留原始 JSON，后续可作阈值与模型回归的 ground truth。",
        "items": [
            {
                "proposal_id": row["id"],
                "figure_id": row["figure_id"],
                "paper_id": row["paper_id"],
                "page": row["page"],
                "current_label": row["label_raw"],
                "status": row["status"],
                "human_verdict": "accept" if row["status"] in POSITIVE else "reject",
                "confidence": row["confidence"],
                "decided_by": row["decided_by"],
                "auto_accepted": bool(row["auto_accepted"]),
                "proposed": json.loads(row["proposed_json"] or "{}"),
                "before": json.loads(row["before_json"] or "null"),
            }
            for row in decided
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(decided)


def main() -> int:
    parser = argparse.ArgumentParser(description="用人工决策校准提案自动接受阈值")
    parser.add_argument("--db", default=".data/dev.db", help="SQLite 开发库路径")
    parser.add_argument(
        "--kind",
        default="figure_bbox",
        choices=["figure_bbox", "figure_review", "figure.group"],
        help="提案类型（figure.group 尚未接入自动接受，仅作统计）",
    )
    parser.add_argument("--export-golden", help="把人判过的提案导出成 JSON（建议放 .data/golden/）")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"找不到数据库：{db}", file=sys.stderr)
        return 2
    rows = _rows(db, args.kind)
    decided = [row for row in rows if row["status"] in POSITIVE | NEGATIVE]
    status_counter = Counter(row["status"] for row in rows)

    print(f"提案类型：{args.kind}    库：{db}")
    print(f"总数 {len(rows)}   状态分布：{dict(status_counter)}")
    print(f"人已决策（accepted/rejected/rolled_back）：{len(decided)}")
    if not rows:
        print("结论：库里还没有这类提案；先跑一轮 AI 复核，再逐条人审。")
        return 0
    if len(decided) < 5:
        print(
            f"结论：样本不足（{len(decided)} < 5）。本轮只输出描述统计，"
            "不改默认阈值；继续在待审队列里逐条采纳/拒绝，样本会自然积累。"
        )

    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in decided:
        buckets[_bucket(float(row["confidence"] or 0))].append(row)
    print("\n置信度分布（人已决策）：")
    for bucket in ("<0.92", "0.92–0.94", "0.95–0.96", "≥0.97"):
        group = buckets.get(bucket, [])
        if not group:
            continue
        accepted = sum(1 for row in group if row["status"] in POSITIVE)
        print(
            f"  {bucket:<10} 共 {len(group):>2} 条 · 人判通过 {accepted} · "
            f"通过率 {accepted / len(group):.0%}"
        )

    simulated = _simulate(decided)
    print("\n如果按阈值开自动接受（以人工决策为 ground truth）：")
    print("  阈值    会接受  正确  误改  precision  coverage")
    for item in simulated:
        precision = "—" if item["precision"] is None else f"{item['precision']:.2f}"
        print(
            f"  ≥{item['threshold']:.2f}   {item['predicted']:>3}   {item['correct']:>3}   "
            f"{item['wrong']:>3}   {precision:>8}   {item['coverage']:.0%}"
        )

    recommendation = _recommend(simulated)
    print(f"\n推荐：{recommendation['threshold'] if recommendation['threshold'] else '保持默认 0.95'}")
    print(f"理由：{recommendation['reason']}")
    if args.kind == "figure_bbox":
        print(
            "注意：框复核还有「页内 / 最小尺寸 / IoU / caption 锚点」四道硬校验，"
            "阈值只是其中一环；precision 高不代表可以绕过这四道。"
        )

    if args.export_golden:
        count = _export_golden(Path(args.export_golden), args.kind, rows)
        print(f"\n已导出 {count} 条人判提案 → {args.export_golden}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
