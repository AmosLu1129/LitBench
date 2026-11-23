# SPDX-License-Identifier: AGPL-3.0-or-later
"""下载 arXiv 开放获取 PDF，作为 P0 解析验证的样本集。

    python scripts/fetch_samples.py

只下载不提交：文件落在 ``.data/samples/``（已在 .gitignore 里）。
选目原则：按「图表密度」挑学科，而不是挑自己熟悉的领域——
材料（XRD/SEM/TGA）、生物（显微图）、地学（地图/剖面）、
计算机视觉（多栏 + 架构图）、物理化学（谱图）。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request
from pathlib import Path

UA = {
    "User-Agent": "litbench-p0/0.1 (open-source research tool; contact: local)",
    "Accept": "application/pdf,application/atom+xml,*/*",
}

# Windows 系统证书库有时缺中间证书，直接用 certifi 的信任链更稳。
# certifi 是 httpx 的既有依赖，不会额外引入包。
try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    SSL_CTX = ssl.create_default_context()

CATS = [
    ("cond-mat.mtrl-sci", "材料：XRD / SEM / 热分析，图多"),
    ("q-bio.QM", "生物医学：显微图、blot"),
    ("physics.geo-ph", "地学：地图 / 剖面 / 时序"),
    ("cs.CV", "计算机视觉：双栏 + 架构图"),
    ("physics.chem-ph", "物理化学：谱图"),
]


def fetch(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read()


def arxiv_ids(category: str, n: int = 2) -> list[str]:
    query = (
        "http://export.arxiv.org/api/query"
        f"?search_query=cat:{category}"
        "&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={n}"
    )
    xml = fetch(query).decode("utf-8", "ignore")
    return re.findall(r"<id>http://arxiv\.org/abs/([^<]+)</id>", xml)


def main() -> int:
    out = Path(__file__).resolve().parents[1] / ".data" / "samples"
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []

    for category, note in CATS:
        try:
            ids = arxiv_ids(category, 2)
        except Exception as exc:  # noqa: BLE001
            print(f"!! {category}: {exc}")
            continue
        for arxiv_id in ids:
            safe = arxiv_id.replace("/", "_")
            dest = out / f"{category}__{safe}.pdf"
            if dest.exists() and dest.stat().st_size > 100_000:
                print(f"skip  {dest.name}")
                manifest.append(
                    {"category": category, "note": note, "arxiv_id": arxiv_id,
                     "file": dest.name, "bytes": dest.stat().st_size}
                )
                continue
            try:
                data = fetch(f"https://arxiv.org/pdf/{arxiv_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"!! download {arxiv_id}: {exc}")
                continue
            if not data.startswith(b"%PDF"):
                print(f"!! not a pdf: {arxiv_id}")
                continue
            dest.write_bytes(data)
            print(f"ok    {dest.name}  {len(data) / 1024:.0f} KB")
            manifest.append(
                {"category": category, "note": note, "arxiv_id": arxiv_id,
                 "file": dest.name, "bytes": len(data)}
            )
            time.sleep(3)  # arXiv API 限速要求

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"total: {len(manifest)}  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
