# SPDX-License-Identifier: AGPL-3.0-or-later
r"""两条 OCR 路径的对比：Docling（RapidOCR）vs MinerU（独立子进程）。

    C:\litbench\.venv\Scripts\python.exe C:\litbench\scripts\compare_ocr.py \
        --books 01,02,03 --pages 1-3

为什么要用它：课本里有 3 本没有文字层，选哪个 OCR 后端不能拍脑袋。

指标口径（先写清楚，免得自己感动自己）：

* ``chars``        —— 识别出的正文字数（不含空白）
* ``cjk_ratio``    —— 中文字符占非空白字符的比例。OCR 乱码会显著拉低它
* ``garbage_ratio``—— 既不是中日韩、也不是拉丁字母/数字/常见标点的字符比例（乱码代理）
* ``captions``     —— 识别出的图注行数（"图 4-2 ..." 形态）
* ``figures``      —— 检出的图区域数；``ink`` 是这些框里「有墨迹」的比例（空框=瞎框）
* ``sim_vs_textlayer`` —— **只在有嵌入文字层的书上算**：把识别文本和 pdfium 从原 PDF
  文字层抽出的文本做字符级相似度（difflib ratio）。这是伪真值，不是人工标注，
  但足以比较两条 OCR 路径谁更接近出版社自己的文字层。

注意：Docling 的解析层打不开非 ASCII 路径（venv 路径也不能带），所以：
  - 本脚本要用 ``C:\litbench\.venv\Scripts\python.exe`` 跑（ASCII 目录联接）；
  - 语料会先复制到 ``C:\mineru-work\corpus``（MinerU 同样怕非 ASCII）。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

CORPUS = Path(os.environ.get("LITBENCH_CORPUS_DIR", str(ROOT / ".data" / "corpus")))
ASCII_CORPUS = Path(r"C:\litbench\.data\mineru-work\corpus")
NAME_MAP = {
    "01": "01-md.pdf",
    "02": "02-trans.pdf",
    "03": "03-act.pdf",
    "04": "04-comb.pdf",
    "05": "05-eet.pdf",
}
# 书 01/02 有嵌入文字层，可当伪真值
HAS_TEXT_LAYER = {"01", "02"}

CAPTION_RE = re.compile(r"^\s*(图|表|Fig\.?|Figure|Table)\s*\d", re.IGNORECASE)
CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F), (0xFF00, 0xFFEF))
COMMON_PUNCT = set("，。、；：？！“”‘’（）《》—…·,.;:?!\"'()[]{}<>/-+%°±×÷=≈≤≥→←↑↓")


def stage(book: str) -> Path:
    """把语料复制到 ASCII 工作目录（语料本身只读，不动它）。"""
    src = next(CORPUS.glob(f"{book}-*.pdf"))
    dst = ASCII_CORPUS / NAME_MAP[book]
    if not dst.is_file():
        ASCII_CORPUS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return dst


def clean(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in CJK_RANGES)


def char_stats(text: str) -> dict:
    chars = [c for c in clean(text) if not c.isspace()]
    if not chars:
        return {"chars": 0, "cjk_ratio": 0.0, "garbage_ratio": 0.0}
    cjk = sum(1 for c in chars if is_cjk(c))
    garbage = 0
    for c in chars:
        if is_cjk(c) or c.isascii() and (c.isalnum() or c in COMMON_PUNCT):
            continue
        if unicodedata.category(c).startswith(("P", "S", "Z", "C")):
            garbage += 1
        else:
            garbage += 1
    return {
        "chars": len(chars),
        "cjk_ratio": round(cjk / len(chars), 3),
        "garbage_ratio": round(garbage / len(chars), 3),
    }


def ink_ratio(pdf_path: Path, bbox: list[float], page: int) -> float:
    """图框里深色像素占比（用于判断是不是空框/正文框）。"""
    try:
        import pypdfium2 as pdfium

        from litbench.adapters.render import crop_to_png, render_page
        from PIL import Image

        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            if page < 1 or page > len(doc):
                return 0.0
            page_obj = doc[page - 1]
            image = render_page(page_obj, 100)
            page_obj.close()
        finally:
            doc.close()
        if image is None:
            return 0.0
        data = crop_to_png(image, bbox, 100)
        if not data:
            return 0.0
        gray = Image.open(io.BytesIO(data)).convert("L")
        histogram = gray.histogram()
        total = sum(histogram)
        if not total:
            return 0.0
        return round(sum(histogram[:128]) / total, 3)
    except Exception:  # noqa: BLE001
        return 0.0



def strip_text_layer(src: Path, pages: tuple[int, int], out: Path) -> Path:
    """把指定页渲染成纯图片 PDF（剥掉文字层），让两条 OCR 引擎做同一道题。"""
    import pypdfium2 as pdfium
    from PIL import Image

    lo, hi = pages
    images: list[Image.Image] = []
    doc = pdfium.PdfDocument(str(src))
    try:
        for page_no in range(lo, min(hi, len(doc)) + 1):
            page = doc[page_no - 1]
            bitmap = page.render(scale=150 / 72.0)
            images.append(bitmap.to_pil().convert("RGB"))
            page.close()
    finally:
        doc.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(out, save_all=True, append_images=images[1:], resolution=150)
    return out


def pdfium_text(pdf_path: Path, pages: tuple[int, int] | None) -> str:
    """用 pdfium 抽嵌入文字层（书 01/02 的伪真值）。

    直接调同步实现：这个函数是在 async main 里被调用的，不能再 asyncio.run。
    """
    from litbench.adapters.pdfium_parser import PdfiumParser

    doc = PdfiumParser(render_images=False)._parse_sync(pdf_path)
    lo, hi = pages or (1, doc.page_count)
    return "".join(b.text for b in doc.blocks if lo <= b.page <= hi and b.type == "text")


async def run_docling(pdf: Path, pages: tuple[int, int]) -> dict:
    import time

    from litbench.adapters.docling_adapter import DoclingParser

    parser = DoclingParser(render_images=False, ocr=True, table_structure=False)
    t0 = time.time()
    doc = await parser.parse(pdf)
    elapsed = time.time() - t0
    lo, hi = pages
    blocks = [b for b in doc.blocks if lo <= b.page <= hi]
    text = "".join(b.text for b in blocks)
    captions = sum(1 for b in blocks if b.type == "caption" or CAPTION_RE.match(b.text or ""))
    figs = [f for f in doc.figures if lo <= f.page <= hi]
    inks = [ink_ratio(pdf, f.bbox, f.page) for f in figs[:10]]
    return {
        "backend": "docling+rapidocr",
        "seconds": round(elapsed, 1),
        "blocks": len(blocks),
        "captions": captions,
        "figures": len(figs),
        "ink_ok": round(sum(1 for i in inks if i > 0.01) / len(inks), 3) if inks else None,
        "sample": text[:160],
        "text": text,
        **char_stats(text),
    }


async def run_mineru(pdf: Path, pages: tuple[int, int]) -> dict:
    import time

    from litbench.adapters.mineru_adapter import MineruParser

    parser = MineruParser(render_images=False, pages=f"{pages[0]}-{pages[1]}")
    t0 = time.time()
    doc = await parser.parse(pdf)
    elapsed = time.time() - t0
    lo, hi = pages
    blocks = [b for b in doc.blocks if lo <= b.page <= hi]
    text = "".join(b.text for b in blocks)
    captions = sum(1 for b in blocks if b.type == "caption" or CAPTION_RE.match(b.text or ""))
    figs = [f for f in doc.figures if lo <= f.page <= hi]
    inks = [ink_ratio(pdf, f.bbox, f.page) for f in figs[:10]]
    return {
        "backend": "mineru",
        "seconds": round(elapsed, 1),
        "blocks": len(blocks),
        "captions": captions,
        "figures": len(figs),
        "ink_ok": round(sum(1 for i in inks if i > 0.01) / len(inks), 3) if inks else None,
        "sample": text[:160],
        "text": text,
        **char_stats(text),
    }


def shingles(text: str, n: int = 3) -> set[str]:
    text = clean(text)
    if len(text) < n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def similarity(a: str, b: str) -> float:
    """3-gram shingle F1：顺序无关，避免「阅读顺序不同」把相似度压成 0。

    precision = OCR 的 shingle 有多少出现在伪真值里（乱码/幻觉会拉低它）
    recall    = 伪真值的 shingle 有多少被 OCR 覆盖（漏字会拉低它）
    """
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    precision = inter / len(sa)
    recall = inter / len(sb)
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 3)


async def main() -> int:
    ap = argparse.ArgumentParser(description="两条 OCR 路径对比")
    ap.add_argument("--books", default="01,02,03", help="逗号分隔：01..05")
    ap.add_argument("--pages", default="1-3", help="页范围，如 1-3")
    ap.add_argument("--backends", default="docling,mineru")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "11-OCR路径对比.md")
    ap.add_argument("--json", type=Path, default=ROOT / ".data" / "ocr-compare.json")
    args = ap.parse_args()

    lo, hi = (int(v) for v in args.pages.split("-", 1))
    pages = (lo, hi)
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    books = [b.strip() for b in args.books.split(",") if b.strip()]

    results: list[dict] = []
    for book in books:
        pdf = stage(book)
        truth = pdfium_text(pdf, pages) if book in HAS_TEXT_LAYER else None
        target = pdf
        if truth is not None:
            # 有文字层的书：剥成纯图片 PDF 再测，否则 docling 会直接读文字层、不是 OCR
            target = strip_text_layer(pdf, pages, ASCII_CORPUS / f"{book}-stripped.pdf")
        for backend in backends:
            print(f"[{book}] {backend} …", flush=True)
            try:
                if backend == "docling":
                    row = await run_docling(target, pages)
                else:
                    row = await run_mineru(target, pages)
            except Exception as exc:  # noqa: BLE001
                row = {"backend": backend, "error": f"{type(exc).__name__}: {exc}"}
            row["book"] = book
            row["pages"] = f"{lo}-{hi}"
            if truth and row.get("sample"):
                row["sim_vs_textlayer"] = similarity(row.get("text", ""), truth)
            results.append(row)
            print("   ", {k: row.get(k) for k in ("seconds", "chars", "captions", "figures", "sim_vs_textlayer", "error")})

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(args.out, results, books, f"{lo}-{hi}")
    print(f"\n报告: {args.out}\n原始: {args.json}")
    return 0


def write_markdown(path: Path, results: list[dict], books: list[str], pages: str) -> None:
    lines = [
        "# OCR 路径对比（Docling+RapidOCR vs MinerU）",
        "",
        f"- 页范围：{pages}",
        f"- 语料：{', '.join(books)}（`<LITBENCH_CORPUS_DIR>`，只读）",
        "- 伪真值：书 01/02 的嵌入文字层（pdfium 抽取），字符级相似度",
        "",
        "| 书 | 后端 | 耗时 s | 字数 | 图注 | 图 | 图框有墨 | 中文字符比 | 乱码字符比 | 与文字层相似度 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in results:
        if row.get("error"):
            lines.append(
                f"| {row['book']} | {row['backend']} | — | — | — | — | — | — | — | 失败：{row['error'][:60]} |"
            )
            continue
        data = dict(row)
        sim = data.pop("sim_vs_textlayer", None)
        data["ink_ok"] = data.get("ink_ok") if data.get("ink_ok") is not None else "—"
        lines.append(
            "| {book} | {backend} | {seconds} | {chars} | {captions} | {figures} | {ink_ok} | "
            "{cjk_ratio} | {garbage_ratio} | {sim} |".format(
                sim=sim if sim is not None else "—", **data
            )
        )
    lines.append("")
    lines.append("## 文本样例")
    for row in results:
        if row.get("sample"):
            lines.append(f"\n**{row['book']} · {row['backend']}**\n\n> {row['sample'][:150]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
