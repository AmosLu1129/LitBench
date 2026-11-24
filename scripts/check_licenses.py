# SPDX-License-Identifier: AGPL-3.0-or-later
"""拦截不允许进入主镜像的依赖。

用法：
    pip-licenses --format=csv --with-license-file --output-file /tmp/licenses.csv
    python scripts/check_licenses.py /tmp/licenses.csv

规则：
    - 允许：MIT / Apache / BSD / ISC / PSF / PostgreSQL / MPL / LGPL
    - 禁止：AGPL、GPL（非 LGPL）、SSPL、RSAL、Commons Clause、Non-Commercial、UNKNOWN

LGPL 放行的理由：西文 LGPL 允许动态链接场景的闭源使用，这里只做依赖引入。
注意 GPL 的正则必须排除 LGPL，否则 psycopg 之类的库会被误杀。
"""

from __future__ import annotations

import csv
import re
import sys

BANNED = re.compile(
    r"(?:^|[^L])GPL"  # GPL / AGPL，但放过 LGPL
    r"|SSPL|RSAL|Commons\s*Clause|Non-?Commercial|UNKNOWN",
    re.IGNORECASE,
)

ALLOWLIST = {
    "MIT",
    "MIT License",
    "BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "Apache Software License",
    "ISC",
    "ISC License (ISCL)",
    "PSF",
    "Python Software Foundation License",
    "PostgreSQL",
    "PostgreSQL License",
    "MPL-2.0",
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
}


def main(path: str) -> int:
    # --with-license-file 会把完整许可证正文写入 CSV；opencv-python-headless 的
    # LICENSE-3RD-PARTY.txt 约 185 KB，超过 Python csv 默认的 128 KB 字段上限。
    csv.field_size_limit(10 * 1024 * 1024)
    bad: list[tuple[str, str, str]] = []
    total = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            total += 1
            name = (row.get("Name") or "").strip()
            version = (row.get("Version") or "").strip()
            lic = (row.get("License") or "").strip()
            if lic in ALLOWLIST:
                continue
            if not lic or BANNED.search(lic):
                bad.append((name, version, lic or "<empty>"))

    if bad:
        print(f"检查了 {total} 个包，发现 {len(bad)} 个不允许进入主镜像的依赖：\n", file=sys.stderr)
        for name, version, lic in bad:
            print(f"  - {name}=={version}  [{lic}]", file=sys.stderr)
        print(
            "\n处理方式：\n"
            "  1) 换成 permissive 替代（优先）\n"
            "  2) 移到 adapters/ 下，以独立容器或子进程接入\n"
            "见 THIRD_PARTY.md。",
            file=sys.stderr,
        )
        return 1

    print(f"licenses ok ({total} packages)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: check_licenses.py <pip-licenses.csv>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
