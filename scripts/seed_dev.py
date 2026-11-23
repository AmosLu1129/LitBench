# SPDX-License-Identifier: AGPL-3.0-or-later
"""把 ``.data/samples`` 的 PDF 解析成一份本地 SQLite 开发库。

    python scripts/seed_dev.py [--db .data/dev.db]

用途：没有 Postgres、没有 Docker 时，也能把前端跑起来看**真实解析结果**。
产物：``.data/dev.db`` + ``.data/images/*.png``（都在 .gitignore 里）。

安全约束（2026-11-13 审查修复）：
* 只允许写仓库内的 ``.data/dev.db`` 这一个文件（防止环境里残留的
  ``DATABASE_URL`` 指向真实库时被误清空）；
* 全部表按外键依赖顺序清空（旧版只清 4 张表，dev 库因此积累了 2.2 万孤儿 block）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".data" / "dev.db"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用 .data/samples 生成 dev SQLite 库")
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="只允许仓库内的 .data/dev.db（默认即该路径；指向其它文件会被拒绝）",
    )
    return parser.parse_args()


async def main(db_path: Path) -> int:
    from sqlalchemy import delete

    from litbench.adapters import PdfiumParser
    from litbench.config import settings
    from litbench.db import dispose_engine, get_engine, get_sessionmaker
    from litbench.models import (
        Base,
        Block,
        Collection,
        Figure,
        FigureProposal,
        ImportRun,
        Job,
        Paper,
        PaperCollection,
        PaperVersion,
        Summary,
        SummaryRevision,
        TableBlock,
    )
    from litbench.paths import to_storage_relative
    from litbench.workers.run import persist

    samples = sorted((ROOT / ".data" / "samples").glob("*.pdf"))
    if not samples:
        print("没有样本。先跑：python scripts/fetch_samples.py")
        return 1

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = get_sessionmaker()
    parser = PdfiumParser(render_images=True)
    started = time.time()
    total_figs = 0

    async with Session() as db:
        # 幂等：按外键依赖顺序清空全部表（不能被 CASCADE 兜底，SQLite 不一定开 FK）
        for model in (
            FigureProposal,
            SummaryRevision,
            Summary,
            Figure,
            TableBlock,
            Block,
            Job,
            PaperCollection,
            ImportRun,
            Collection,
            PaperVersion,
            Paper,
        ):
            await db.execute(delete(model))
        await db.commit()

        seen: dict[str, str] = {}
        for index, pdf in enumerate(samples, 1):
            category = pdf.stem.split("__")[0]
            arxiv_id = pdf.stem.split("__")[-1].replace("v1", "").replace("v2", "")
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()

            # 同一篇论文可能被两条检索结果重复抓到（跨分类交叉挂载很常见），
            # 按 sha256 去重；库里的唯一约束也会拦，但这里给出可读的提示。
            if digest in seen:
                print(f"  [{index}/{len(samples)}] {pdf.name[:42]:<44} 跳过（与 {seen[digest]} 同一文件）")
                continue
            seen[digest] = pdf.name

            # 样本文件名是假 arXiv 编号（如 2611.xxxxx），前两位是年份：26 → 2026。
            try:
                year = 2000 + int(arxiv_id[:2])
            except (TypeError, ValueError):
                year = None

            paper = Paper(
                title=f"{category} · {arxiv_id}",
                arxiv_id=arxiv_id,
                year=year,
                venue="arXiv",
                status="new",
            )
            db.add(paper)
            await db.flush()

            version = PaperVersion(
                paper_id=paper.id,
                kind="preprint",
                arxiv_id=arxiv_id,
                file_sha256=digest,
                file_path=to_storage_relative(settings.storage_dir, pdf),
            )
            db.add(version)
            await db.flush()

            try:
                doc = await parser.parse(pdf)
            except Exception as exc:  # noqa: BLE001
                print(f"  !! {pdf.name}: {type(exc).__name__}: {exc}")
                paper.status = "failed"
                await db.commit()
                continue

            rows = await persist(db, version, doc)
            await db.commit()
            total_figs += len(doc.figures)
            print(
                f"  [{index}/{len(samples)}] {pdf.name[:42]:<44}"
                f" blocks={len(doc.blocks):>5} figures={len(doc.figures):>3} rows={rows:>5}"
            )

    await dispose_engine()
    print(f"\n完成：{len(samples)} 篇，{total_figs} 张图，用时 {time.time() - started:.1f}s")
    print(f"数据库：{db_path}")
    print("\n起服务：")
    print(f"  set DATABASE_URL=sqlite+aiosqlite:///{db_path.as_posix()}")
    print("  set STORAGE_DIR=" + str(ROOT / ".data"))
    print("  cd server && uvicorn litbench.main:app --reload")
    return 0


def _cli() -> int:
    args = _parse_args()
    db_path = Path(args.db).resolve()
    if db_path != DEFAULT_DB.resolve():
        print(
            f"拒绝：--db 只允许 {DEFAULT_DB}（当前 {db_path}）。\n"
            "这是防止误清空真实库；需要别的库请手工建或改脚本后显式评审。",
            file=sys.stderr,
        )
        return 2
    # 固定本次进程的 DB/存储，不受外部环境变量影响；必须在 import litbench 之前设置。
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    os.environ["STORAGE_DIR"] = str(ROOT / ".data")
    sys.path.insert(0, str(ROOT / "server"))
    return asyncio.run(main(db_path))


if __name__ == "__main__":
    raise SystemExit(_cli())
