# SPDX-License-Identifier: AGPL-3.0-or-later
"""测试用的最小 PDF 生成器。

为什么手搓而不是塞一个样本文件进仓库：

* 样本 PDF 有版权，不能进 MIT/AGPL 仓库；
* 手搓的文件只有 800 字节，测试快、diff 可读、内容完全可控（图号/图注长度都能精确构造）。

要点：**xref 偏移必须按实际字节算**。写死偏移会得到一个「pdfium 能打开、但对象错位」
的文件，报错信息完全不指向真正原因。
"""

from __future__ import annotations


def make_scanned_pdf(
    *,
    caption: str = "Fig. 10-3 Test caption on a scanned page",
    page_w: float = 469.0,
    page_h: float = 649.0,
) -> bytes:
    """模拟一页扫描课本：整页一张位图 + 一层 OCR 文字（正文一行 + 图注一行）。

    用来回归「扫描页不能当底纹丢掉」这条路径：页面只有 image 对象和文字对象，
    没有任何 path —— 和真实影印课本的对象构成一致。
    """
    import zlib

    # 8x8 灰度图：中间画一条黑带，方便断言"裁出来的图里有墨迹"
    pixels = bytearray()
    for y in range(8):
        for _x in range(8):
            pixels.extend((0, 0, 0) if y in (3, 4) else (255, 255, 255))
    image_data = zlib.compress(bytes(pixels))

    lines = [
        (
            page_h - 60,
            "Body text on the scanned page, long enough to be a blocker for the band.",
        ),
        (
            page_h - 80,
            "Another body line so the OCR text layer passes the 100-char threshold.",
        ),
        (page_h * 0.45, caption),
    ]
    content = (
        f"q {page_w:.1f} 0 0 {page_h:.1f} 0 0 cm /Im1 Do Q\n"
        + "".join(f"BT /F1 10 Tf 40 {y:.1f} Td ({text}) Tj ET\n" for y, text in lines)
    ).encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.1f %.1f] "
            b"/Resources << /XObject << /Im1 5 0 R >> /Font << /F1 6 0 R >> >> "
            b"/Contents 4 0 R >>" % (page_w, page_h)
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        (
            b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream"
            % (len(image_data), image_data)
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref_at,
    )
    return bytes(out)


def make_pdf(
    *,
    caption_gap: float = 8.0,
    caption: str = "Fig. 1. Temperature dependence of the reaction rate.",
    extra_text: bool = True,
) -> bytes:
    """一页 PDF：正文几行 + 一个矢量矩形 + 一行 figure caption。

    * 字体用 Helvetica（base14，阅读器自带），不需要嵌字体。
    * ``extra_text=True`` 让正文总长 > 100 字符，解析器才会判 ``text_layer=True``；
      ``extra_text=False`` 可构造「没有文字层」的扫描件路径。
    """
    rect_x0, rect_y0, rect_x1, rect_y1 = 100.0, 520.0, 400.0, 650.0
    cap_y = rect_y0 - caption_gap
    lines = [(cap_y, caption)]
    if extra_text:
        lines.append((700.0, "Results are shown in the figure below."))
        lines.append((740.0, "Supplementary notes: the experiment was repeated three times."))
    content = (
        "0.8 w\n"
        f"{rect_x0:.1f} {rect_y0:.1f} {rect_x1 - rect_x0:.1f} {rect_y1 - rect_y0:.1f} re\n"
        "S\n" + "".join(f"BT /F1 10 Tf 72 {y:.1f} Td ({text}) Tj ET\n" for y, text in lines)
    ).encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref_at,
    )
    return bytes(out)
