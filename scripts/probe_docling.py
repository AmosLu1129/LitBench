# SPDX-License-Identifier: AGPL-3.0-or-later
"""探针：确认 Docling 2.x 的实际 API 形状（字段名/坐标原点/caption 链接）。"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from docling.document_converter import DocumentConverter

SAMPLE = next(
    iter(
        sorted(
            (Path(__file__).resolve().parents[1] / ".data" / "samples").glob("*.pdf"),
            key=lambda p: p.stat().st_size,
        )
    )
)


def main() -> int:
    print("file:", SAMPLE.name, f"{SAMPLE.stat().st_size / 1024:.0f} KB")
    started = time.time()
    converter = DocumentConverter()
    result = converter.convert(str(SAMPLE))
    elapsed = time.time() - started
    doc = result.document
    print(f"convert seconds: {elapsed:.1f}")
    print("pages:", len(doc.pages))

    labels: Counter[str] = Counter()
    first_pic = None
    first_text = None
    for item, _level in doc.iterate_items():
        label = str(getattr(item, "label", ""))
        labels[label] += 1
        if first_pic is None and label.lower() == "picture":
            first_pic = item
        if first_text is None and label.lower() in ("text", "section_header"):
            first_text = item
    print("labels:", dict(labels))

    for name, item in (("picture", first_pic), ("text", first_text)):
        if item is None:
            print(f"{name}: none")
            continue
        print(f"--- {name} ---")
        print("  type:", type(item).__name__)
        print("  attrs:", [a for a in dir(item) if not a.startswith("_")][:25])
        prov = getattr(item, "prov", None)
        print("  prov len:", len(prov) if prov else 0)
        if prov:
            p0 = prov[0]
            print("  prov attrs:", [a for a in dir(p0) if not a.startswith("_")][:15])
            print("  page_no:", getattr(p0, "page_no", None))
            bbox = getattr(p0, "bbox", None)
            print("  bbox:", bbox)
            for field in ("l", "t", "r", "b", "coord_origin", "to_top_left"):
                print(f"    {field}:", getattr(bbox, field, "<none>"))
        if name == "picture":
            caps = getattr(item, "captions", None)
            print("  captions:", len(caps) if caps else 0)
            if caps:
                print("  caption ref:", type(caps[0]).__name__, getattr(caps[0], "cref", None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
