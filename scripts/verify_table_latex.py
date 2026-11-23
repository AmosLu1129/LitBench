# SPDX-License-Identifier: AGPL-3.0-or-later
"""表格导出 LaTeX 的编译验收（agent / 工具侧执行 xelatex）。

为什么拆成 prepare / report 两步：MiKTeX 25 把「未检查更新」记成 major issue，
**从 Python subprocess 拉起的 xelatex 会被误判失败且不产 PDF**；由 agent 的 shell
（PowerShell / bash）直接启动则正常。所以本脚本只做「生成 .tex」和「解析日志」，
中间那步编译交给 agent 执行（prepare 会连同 compile.ps1 / compile.sh 一起写好）。

用法：

    python scripts/verify_table_latex.py prepare --table-id <tab_id>
    # agent 在 shell 里跑（不要包在 Python 里）：
    powershell -File <out>/compile.ps1        # Windows
    bash <out>/compile.sh                     # Linux/macOS
    python scripts/verify_table_latex.py report --out <out>

默认对同一张表同时生成 auto / plain 两个版本，方便比较「按页宽换行」和旧版
``l`` 列的 Overfull 差异。退出码：report 在「缺 PDF / 有 LaTeX 错误 / Overfull
超过 --max-overfull」时非 0，可直接给 agent 当验收闸门。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from litbench.table_latex import TableLatexError, table_html_to_latex  # noqa: E402

DEFAULT_DB = ROOT / ".data" / "dev.db"
PREAMBLE = r"""\documentclass[UTF8]{article}
\usepackage{ctex}
\usepackage{multirow}
\usepackage[margin=2.5cm]{geometry}
\begin{document}
"""

COMPILE_PS1 = r"""$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
Get-ChildItem -Filter *.tex | ForEach-Object {
    Write-Host "== $($_.Name)"
    & xelatex -interaction=nonstopmode $_.Name
}
"""

COMPILE_SH = """#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
for tex in *.tex; do
    echo "== $tex"
    xelatex -interaction=nonstopmode "$tex" || true
done
"""


def _ascii_path(path: Path) -> bool:
    return all(ord(char) < 128 for char in str(path))


def _default_out(table_id: str) -> Path:
    """输出目录优先放 ASCII 路径（xelatex 处理中文 cwd 不如 Python 稳）。

    按顺序尝试：仓库内 .data/tmp → 本机 ASCII 目录联接（如 C:\\litbench，docs/09
    约定）→ 系统临时目录 → %LOCALAPPDATA%\\Temp → 用户目录；中文仓库路径会落到
    可写的 ASCII 目录。都不可写时提示用 --out 指定。
    """
    candidates: list[Path] = [ROOT / ".data" / "tmp" / "latex-verify"]
    for alias in (Path(r"C:\litbench"), Path("/c/litbench")):
        try:
            if alias.is_dir() and alias.resolve() == ROOT.resolve():
                candidates.insert(0, alias / ".data" / "tmp" / "latex-verify")
                break
        except OSError:
            continue
    candidates.extend(
        [
            Path(tempfile.gettempdir()) / "litbench-latex-verify",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "Temp"
            / "litbench-latex-verify",
            Path.home() / "litbench-latex-verify",
        ]
    )
    for candidate in candidates:
        if not _ascii_path(candidate):
            continue
        target = candidate / table_id[:8]
        try:
            target.mkdir(parents=True, exist_ok=True)
            return target
        except OSError:
            continue
    raise SystemExit("找不到可写的 ASCII 输出目录；请用 --out 指定")


def _load_table(db_path: Path, table_id: str) -> tuple[str, str, str | None, str | None]:
    if not db_path.is_file():
        raise SystemExit(f"数据库不存在：{db_path}；先跑 scripts/seed_dev.py 或传 --db")
    con = sqlite3.connect(db_path)
    row = con.execute(
        "select html, caption, label_raw, label_norm from tab where id = ?", (table_id,)
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"表不存在：{table_id}")
    html, caption, label_raw, label_norm = row
    if not html or not html.strip():
        raise SystemExit(f"表 {table_id} 没有结构化 HTML（图片表），无法导出 LaTeX")
    return html, caption or "", label_raw, label_norm


def prepare(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    table_id = args.table_id
    out_dir = Path(args.out) if args.out else _default_out(table_id)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    html, caption, label_raw, label_norm = _load_table(db_path, table_id)
    widths = ["auto", "plain"] if args.width == "both" else [args.width]
    meta: dict = {"table_id": table_id, "caption": caption, "targets": []}
    for width in widths:
        try:
            payload = table_html_to_latex(
                html,
                caption=caption,
                label_raw=label_raw,
                label_norm=label_norm,
                table_id=table_id,
                width=width,
            )
        except TableLatexError as exc:
            raise SystemExit(f"{width}: {exc}") from exc
        name = f"table-{width}"
        (out_dir / f"{name}.tex").write_text(
            PREAMBLE + payload.code + "\n\\end{document}\n", encoding="utf-8"
        )
        meta["targets"].append(
            {
                "name": name,
                "width": width,
                "tex": f"{name}.tex",
                "wrapped": payload.wrapped,
                "column_spec": payload.column_spec,
                "columns": payload.columns,
                "rows": payload.rows,
                "warnings": payload.warnings,
            }
        )
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "compile.ps1").write_text(COMPILE_PS1, encoding="utf-8")
    (out_dir / "compile.sh").write_text(COMPILE_SH, encoding="utf-8")

    print(f"prepared {len(meta['targets'])} target(s): {out_dir}")
    for target in meta["targets"]:
        print(
            f"  {target['name']}: wrapped={target['wrapped']} "
            f"cols={target['columns']} rows={target['rows']} spec={target['column_spec']}"
        )
    print()
    print("下一步（agent 在 shell 里执行，不要包进 Python subprocess）：")
    print(f'  powershell -File "{out_dir / "compile.ps1"}"')
    print(f'  python scripts/verify_table_latex.py report --out "{out_dir}"')
    return 0


def _pdf_pages(pdf_path: Path) -> int | None:
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415

        return len(pdfium.PdfDocument(str(pdf_path)))
    except Exception:  # noqa: BLE001 — 没有 pypdfium2 时只报 PDF 存在
        return None


def _parse_log(log_path: Path) -> dict:
    if not log_path.is_file():
        return {"overfull": -1, "underfull": -1, "errors": [], "details": []}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    details = re.findall(
        r"Overfull \\hbox \(([\d.]+)pt too wide\) in paragraph at lines ([\d-]+)", text
    )
    errors = [line for line in text.splitlines() if line.startswith("! ")]
    return {
        "overfull": len(details),
        "underfull": len(re.findall(r"Underfull \\hbox", text)),
        "errors": errors,
        "details": [f"{size}pt @ lines {lines}" for size, lines in details[:3]],
    }


def report(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    meta_path = out_dir / "meta.json"
    if not meta_path.is_file():
        raise SystemExit(f"没有 meta.json：{out_dir}；先跑 prepare")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    failures = 0
    print(f"{'target':<12}{'pdf':<6}{'pages':>6}{'overfull':>10}{'underfull':>11}{'latex err':>10}")
    for target in meta["targets"]:
        name = target["name"]
        pdf_path = out_dir / f"{name}.pdf"
        log_path = out_dir / f"{name}.log"
        info = _parse_log(log_path)
        exists = pdf_path.exists()
        pages = _pdf_pages(pdf_path) if exists else None
        errors = len(info["errors"])
        print(
            f"{name:<12}{'yes' if exists else 'NO':<6}"
            f"{(pages if pages is not None else '?'):>6}"
            f"{info['overfull']:>10}{info['underfull']:>11}{errors:>10}"
        )
        if not exists:
            failures += 1
            print(f"  ! 没有 PDF：{pdf_path}（编译被中断或 xelatex 未直接由 shell 启动）")
        if errors:
            failures += 1
            print(f"  ! LaTeX 错误：{info['errors'][0]}")
        if exists and info["overfull"] > args.max_overfull:
            failures += 1
            print(f"  ! Overfull {info['overfull']} 处（阈值 {args.max_overfull}）")
            for detail in info["details"]:
                print(f"      {detail}")
        if target.get("warnings"):
            print(f"  导出提示：{'；'.join(target['warnings'])}")
    print()
    print("通过" if failures == 0 else f"未通过（{failures} 项）")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="表格 LaTeX 编译验收（prepare/compile/report）")
    sub = parser.add_subparsers(dest="mode", required=True)

    prep = sub.add_parser("prepare", help="从库里的表生成 .tex + compile.ps1/sh")
    prep.add_argument("--table-id", required=True)
    prep.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    prep.add_argument("--width", choices=("auto", "plain", "both"), default="both")
    prep.add_argument("--out", default=None, help="输出目录（默认 .data/tmp 或系统临时目录）")
    prep.set_defaults(func=prepare)

    rep = sub.add_parser("report", help="解析 xelatex 日志与 PDF，给验收结论")
    rep.add_argument("--out", required=True)
    rep.add_argument("--max-overfull", type=int, default=0)
    rep.set_defaults(func=report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
