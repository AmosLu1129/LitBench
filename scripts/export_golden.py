# SPDX-License-Identifier: AGPL-3.0-or-later
"""把人工/agent 的复核结果导出成 golden set JSON。

    python scripts/export_golden.py -o .data/golden.json
    python scripts/export_golden.py --include-pending      # 连未复核的一起，做基线用

不用起 API：直接读 ``DATABASE_URL`` 指向的库。``source_path`` 是相对 storage 根的路径，
所以这份 JSON 换机器、换挂载点也能被 ``replay_golden.py`` 回放。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{(ROOT / '.data' / 'dev.db').as_posix()}"
)
os.environ.setdefault("STORAGE_DIR", str(ROOT / ".data"))
sys.path.insert(0, str(ROOT / "server"))

from litbench.golden import export_golden  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="导出 golden set")
    ap.add_argument("-o", "--out", type=Path, help="输出文件（默认打印到 stdout）")
    ap.add_argument(
        "--include-pending",
        action="store_true",
        help="连未复核的图一起导出（只适合做基线，不适合当 golden 判分）",
    )
    args = ap.parse_args()

    from litbench.db import dispose_engine, get_sessionmaker

    try:
        async with get_sessionmaker()() as db:
            data = await export_golden(db, include_pending=args.include_pending)
    finally:
        await dispose_engine()

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"已写出 {args.out}：{data['item_count']} 条", file=sys.stderr)
    else:
        print(text)

    ok = sum(1 for i in data["items"] if i["review_status"] == "ok")
    wrong = sum(1 for i in data["items"] if i["review_status"] == "wrong")
    print(f"golden: {data['item_count']} 条（ok={ok} / wrong={wrong}）", file=sys.stderr)
    if data["item_count"] == 0:
        print(
            "提示：还没有任何复核记录。在界面里点「定位正确 / 定位错了」，"
            "或 POST /api/figures/{id}/review 造几条再来。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
