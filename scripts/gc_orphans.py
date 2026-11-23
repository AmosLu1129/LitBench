# SPDX-License-Identifier: AGPL-3.0-or-later
"""清理 dev SQLite 库里的孤儿行（父行已不存在的引用）。

    python scripts/gc_orphans.py           # 只统计，不删
    python scripts/gc_orphans.py --apply   # 真正删除

背景：2026-11-13 审查发现旧 seed_dev 只清 4 张表，dev 库积累了 22,888 个孤儿
block（FK 在 SQLite 上又默认关闭，CASCADE 不生效）。现在 SQLite 连接已开
``PRAGMA foreign_keys=ON``，但历史孤儿仍在；这个脚本只清历史遗留。

安全：只接受仓库内 ``.data/dev.db``，且只有 `--apply` 才写；按外键依赖顺序删除。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".data" / "dev.db"

# 顺序即删除顺序：先删引用方，再删被引用方。
CHECKS: list[tuple[str, str]] = [
    (
        "summary_revision（summary 已不在）",
        "SELECT count(*) FROM summary_revision WHERE summary_id NOT IN (SELECT id FROM summary)",
    ),
    (
        "summary（paper_version 已不在）",
        "SELECT count(*) FROM summary WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "figure_proposal（paper_version 或 figure 已不在）",
        "SELECT count(*) FROM figure_proposal WHERE paper_version_id NOT IN (SELECT id FROM paper_version)"
        " OR figure_id NOT IN (SELECT id FROM figure)",
    ),
    (
        "job（paper_version 已不在）",
        "SELECT count(*) FROM job WHERE paper_version_id IS NOT NULL"
        " AND paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "figure（paper_version 已不在）",
        "SELECT count(*) FROM figure WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "block（paper_version 已不在）",
        "SELECT count(*) FROM block WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "tab（paper_version 已不在）",
        "SELECT count(*) FROM tab WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "paper_collection（paper 或 collection 已不在）",
        "SELECT count(*) FROM paper_collection WHERE paper_id NOT IN (SELECT id FROM paper)"
        " OR collection_id NOT IN (SELECT id FROM collection)",
    ),
    (
        "paper_version（paper 已不在）",
        "SELECT count(*) FROM paper_version WHERE paper_id NOT IN (SELECT id FROM paper)",
    ),
]

DELETES: list[tuple[str, str]] = [
    (
        "summary_revision",
        "DELETE FROM summary_revision WHERE summary_id NOT IN (SELECT id FROM summary)",
    ),
    (
        "summary",
        "DELETE FROM summary WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "figure_proposal",
        "DELETE FROM figure_proposal WHERE paper_version_id NOT IN (SELECT id FROM paper_version)"
        " OR figure_id NOT IN (SELECT id FROM figure)",
    ),
    (
        "job",
        "DELETE FROM job WHERE paper_version_id IS NOT NULL"
        " AND paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "figure",
        "DELETE FROM figure WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "block",
        "DELETE FROM block WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "tab",
        "DELETE FROM tab WHERE paper_version_id NOT IN (SELECT id FROM paper_version)",
    ),
    (
        "paper_collection",
        "DELETE FROM paper_collection WHERE paper_id NOT IN (SELECT id FROM paper)"
        " OR collection_id NOT IN (SELECT id FROM collection)",
    ),
    (
        "paper_version",
        "DELETE FROM paper_version WHERE paper_id NOT IN (SELECT id FROM paper)",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 dev SQLite 孤儿行")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只统计）")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if db_path != DEFAULT_DB.resolve():
        print(f"拒绝：只允许 {DEFAULT_DB}（当前 {db_path}）", file=sys.stderr)
        return 2
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2

    con = sqlite3.connect(db_path)
    try:
        print(f"数据库：{db_path}\n")
        total = 0
        for label, sql in CHECKS:
            n = con.execute(sql).fetchone()[0]
            total += n
            print(f"  {label}: {n}")
        print(f"\n合计孤儿行：{total}")
        if not args.apply:
            print("\n（dry-run）加 --apply 才会删除。")
            return 0
        if total == 0:
            print("没有需要清理的行。")
            return 0

        con.execute("PRAGMA foreign_keys=ON")
        deleted = 0
        for label, sql in DELETES:
            cur = con.execute(sql)
            print(f"  删除 {label}: {cur.rowcount}")
            deleted += max(cur.rowcount, 0)
        con.commit()
        print(f"\n完成：删除 {deleted} 行。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
