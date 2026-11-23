# SPDX-License-Identifier: AGPL-3.0-or-later
"""校验文档里的「当前基线」计数是否与仓库实际一致（2026-11-16，自修批 B）。

背景：测试/迁移/表数一变，文档里的「测试基线：后端 N / MCP M」就容易过期，
而且历史交付记录里本来就该保留旧数字——全库扫描必然误报。这里只校验
**明确声明为当前基线**的锚点（docs/09 头部、docs/17 头部、README 当前基线段），
历史小节不碰。

用法（仓库根目录）::

    python scripts/check_doc_counts.py

默认自己跑 ``pytest --collect-only`` 取实际数字；CI 里也可以用
``--server-count/--mcp-count`` 直接传（避免重复收集）。

退出码：0 = 一致；1 = 有不一致（打印 file:line、现值、期望值）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SERVER_TESTS = ROOT / "server" / "tests"
MIGRATIONS = ROOT / "server" / "migrations" / "versions"
MODELS = ROOT / "server" / "litbench" / "models.py"
CSL_STYLES = ROOT / "server" / "litbench" / "csl_styles"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_of(text: str, pattern: re.Pattern[str], match: re.Match[str]) -> int:
    return text.count("\n", 0, match.start()) + 1


def _pytest_count(cwd: Path) -> int:
    """跑一次收集（不执行测试），返回收集到的用例数。"""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"(\d+) tests? collected", output)
    if match is None:
        raise SystemExit(
            f"无法从 pytest --collect-only 解析用例数（cwd={cwd}）：\n{output[-2000:]}"
        )
    return int(match.group(1))


def _migration_head() -> tuple[int, int]:
    numbers = sorted(
        int(path.stem.split("_", 1)[0])
        for path in MIGRATIONS.glob("0*.py")
        if path.stem.split("_", 1)[0].isdigit()
    )
    if not numbers:
        raise SystemExit("没有找到迁移文件")
    return numbers[-1], numbers[-1] + 1


def _table_count() -> int:
    return len(re.findall(r'__tablename__\s*=\s*"', _read(MODELS)))


def _csl_style_count() -> int:
    return len(list(CSL_STYLES.glob("*.csl")))


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checked = 0

    def expect(
        self,
        path: Path,
        pattern: str,
        expected: tuple[str, ...],
        *,
        label: str,
    ) -> None:
        """在文件里找唯一一处 ``pattern``，把捕获组与 expected 逐项比对。"""
        text = _read(path)
        regex = re.compile(pattern)
        matches = list(regex.finditer(text))
        rel = path.relative_to(ROOT).as_posix()
        if not matches:
            self.failures.append(f"{rel}: 找不到锚点「{label}」({pattern})")
            return
        if len(matches) > 1:
            self.failures.append(
                f"{rel}: 锚点「{label}」出现 {len(matches)} 次，无法唯一校验"
            )
            return
        match = matches[0]
        actual = match.groups()
        self.checked += 1
        if tuple(str(item) for item in actual) != tuple(str(item) for item in expected):
            line = _line_of(text, regex, match)
            self.failures.append(
                f"{rel}:{line} 锚点「{label}」="
                + "/".join(str(item) for item in actual)
                + "，期望 "
                + "/".join(str(item) for item in expected)
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验文档当前基线计数")
    parser.add_argument("--server-count", type=int, help="后端测试收集数（不传则现跑）")
    parser.add_argument("--mcp-count", type=int, help="MCP 测试收集数（不传则现跑）")
    args = parser.parse_args(argv)

    server_count = (
        args.server_count
        if args.server_count is not None
        else _pytest_count(ROOT / "server")
    )
    mcp_count = (
        args.mcp_count if args.mcp_count is not None else _pytest_count(ROOT / "mcp")
    )
    head, next_head = _migration_head()
    tables = _table_count()
    styles = _csl_style_count()

    checker = Checker()
    server = str(server_count)
    mcp = str(mcp_count)

    # 当前测试基线：三处头部/基线句 + README 命令注释
    checker.expect(
        DOCS / "09-现状交接.md",
        r"测试基线：后端 \*\*(\d+) passed\*\*；MCP 薄层 \*\*(\d+) passed\*\*",
        (server, mcp),
        label="docs/09 头部测试基线",
    )
    checker.expect(
        DOCS / "17-下一步路线.md",
        r"\*\*测试基线\*\*：后端 (\d+) / MCP (\d+)",
        (server, mcp),
        label="docs/17 头部测试基线",
    )
    checker.expect(
        ROOT / "README.md",
        r"测试基线现为\*\*后端 (\d+) / MCP (\d+)\*\*",
        (server, mcp),
        label="README 当前测试基线",
    )
    checker.expect(
        ROOT / "README.md",
        r"后端 (\d+) 个测试 \+ MCP (\d+) 个测试",
        (server, mcp),
        label="README 测试数字",
    )
    checker.expect(
        ROOT / "README.md",
        r"pytest -q\s+# (\d+) passed",
        (server,),
        label="README pytest 命令注释",
    )
    checker.expect(
        DOCS / "09-现状交接.md",
        r"pytest -q\s+# (\d+) passed",
        (server,),
        label="docs/09 pytest 命令注释",
    )

    # 迁移编号：docs/09 头部索引必须指向 head+1
    checker.expect(
        DOCS / "09-现状交接.md",
        r"（下一个是 (\d{4})）",
        (f"{next_head:04d}",),
        label="docs/09 下一个迁移编号",
    )
    # 数据模型表数
    checker.expect(
        DOCS / "09-现状交接.md",
        r"## 5\. 数据模型（(\d+) 张表）",
        (str(tables),),
        label="docs/09 数据模型表数",
    )
    # CSL 官方样式数（docs/15 §12.42 是 M8 的定义处）
    checker.expect(
        DOCS / "15-知识库与Agent协作长线计划.md",
        r"内置 (\d+) 个官方样式",
        (str(styles),),
        label="docs/15 CSL 样式数",
    )

    if checker.failures:
        print("文档计数不一致：")
        for item in checker.failures:
            print(f"  - {item}")
        print(
            "\n提示：历史小节里的数字是当时快照，不要改；只更新「当前基线」锚点。"
        )
        return 1
    print(
        f"文档计数一致：后端 {server} / MCP {mcp} / "
        f"迁移 0001–{head:04d}（下一个 {next_head:04d}） / "
        f"{tables} 张表 / {styles} 个 CSL 样式（{checker.checked} 个锚点）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
