# SPDX-License-Identifier: AGPL-3.0-or-later
"""图号校核的规则层（纯函数，可单测）。

解析器只负责「图块在哪、图注文本是什么」；**这个模块负责回答「这张图到底叫什么」**，
并给每个判断标上来源和置信度。为什么需要它：真实课本里 171/352 张图没有编号，
但其中大多数并不是没有编号——而是

* 一个系列图被版面模型拆成多个图块，图号只挂在其中一个上（子图）；
* 图注文本里其实有「图 7-15」，只是没被提取出来；
* 文字层/OCR 把编号识别成了 ``图 10凸7``、``图 4.e.1``、``图 7 - 25`` 这种形态。

规则层不重新识别图像，只在已有文本上做：**容错规范化 → 上下文校验 → 子图归组/继承**。
所有判断都写 ``label_source`` + ``label_confidence``，低置信只作候选（进 review 队列），
不自动写进引用。

许可证：本模块不依赖任何模型，AGPL 无传染问题。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .labels import LABEL_RE, normalize_label

# --- 置信度档位（写进 figure.label_confidence，review UI 按这个分级） -----------
CONF_REVIEW = 0.95  # 原始文本直接提取，正常
CONF_REPAIRED = 0.8  # 字符修复后（e→5、凸→- 等）且通过上下文校验
CONF_INHERIT = 0.7  # 从同页父图号继承（子图）
CONF_INFER = 0.35  # 按编号序列推断，只作候选
CONF_NONE = 0.0

# 子图归组时不要覆盖的来源：这些是校核结论/人工判断
_KEEP_SOURCES = {"ocr", "ocr_consensus", "ocr_cross_conflict", "ocr_cross_unresolved", "review"}

# 低置信阈值：低于它就在界面上打「待确认」
LOW_CONFIDENCE = 0.6
# 子图归组的几何间隙（pt）：同一图区里两个图块的间隙小于它才算相邻
ZONE_GAP = 100.0

# OCR/文字层常见错字 → 数字：只在「替换后能构成合法图号」时使用
_OCR_DIGIT_MAP = {
    "e": "5",
    "E": "5",
    "o": "0",
    "O": "0",
    "l": "1",
    "I": "1",
    "S": "5",
    "B": "8",
    "g": "9",
    "q": "9",
}

# 分隔符乱码：中文教材的「图 10-5」常被识别成 10/5、10凸7、10^2、10_12
_SEP_JUNK = "/\\凸^_—–~·"
_SEP_JUNK_RE = re.compile(rf"[\\/凸^_—–~·]")
# 「图 7 - 25」这类：编号中间被空格撑开
_SPACED_SEP_RE = re.compile(r"(\d)\s+([-–—.])\s+(\d)")

# 质量评估用：出现这些字符说明文字层/OCR 已经明显坏了
_SUSPECT_CHARS = set("\ufffd\u25a1\u25a0\u25cf\u2013\u2014\u00b7\u3000")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 常见的「碎片化编号」形态（图 10/0、图 10凸7、图 4.e.1）；正常的 - . 分隔不算
_BROKEN_LABEL_RE = re.compile(
    r"(图|表)\s*\d{1,3}\s*(?:[^\u4e00-\u9fff\s\d\-–—.]|凸)[^\u4e00-\u9fff\s]{0,2}\d{1,3}"
)
# 「图 4.e.1」这种数字段里混了字母，也是坏形态
_BROKEN_LETTER_RE = re.compile(r"(图|表)\s*\d{1,3}\s*[.\-]\s*[a-zA-Z]")


@dataclass(slots=True)
class LabelCandidate:
    """一次「这张图叫什么」的判断结果。"""

    label_raw: str | None = None
    label_norm: str | None = None
    confidence: float = CONF_NONE
    source: str = "none"
    reason: str = ""


@dataclass(slots=True)
class TextQuality:
    """文字层质量分。阶段 1 只记录；阶段 2 用它决定是否改走 OCR。"""

    score: float = 1.0
    flags: dict = field(default_factory=dict)

    @property
    def is_poor(self) -> bool:
        return self.score < 0.5


# ---------------------------------------------------------------------------
# 编号规范化 / 修复
# ---------------------------------------------------------------------------
def _to_halfwidth(text: str) -> str:
    """全角数字/字母/标点 → 半角（NFKC 对中文汉字无副作用）。"""
    return unicodedata.normalize("NFKC", text)


def _clean_label_text(raw: str) -> str:
    text = _to_halfwidth((raw or "").strip())
    # 图 7 - 25 → 图 7-25
    text = _SPACED_SEP_RE.sub(r"\1\2\3", text)
    # 图 10凸7 / 10/0 / 10^2 → 10-7 / 10-0 / 10-2（分隔符统一成短横）
    text = _SEP_JUNK_RE.sub("-", text)
    # 去掉「图」和数字之间的奇怪符号（保留点分：图4.2.1）
    text = re.sub(r"(图|表)\s+", r"\1", text)
    return text


def _digit_variants(segment: str) -> list[str]:
    """把疑似 OCR 错字还原成数字的所有候选（每个非数字字符给一个可能值）。"""
    options = []
    for ch in segment:
        if ch.isdigit():
            options.append([ch])
        elif ch in _OCR_DIGIT_MAP:
            options.append([_OCR_DIGIT_MAP[ch], ch])  # 先尝试替换，再保留原字
        else:
            options.append([ch])
    # 组合数很小（段长 ≤ 3），直接笛卡尔积
    variants = [""]
    for opts in options:
        variants = [v + o for v in variants for o in opts]
    return variants


def _needs_repair(raw: str) -> bool:
    """这段原文是不是被污染过（需要降置信、标 rule_repair）？

    正常写法「图 10-5」不算；「图 10/0」「图 10凸7」「图 7 - 25」「图 4.e.1」算。
    """
    return (
        bool(_SEP_JUNK_RE.search(raw))
        or bool(re.search(r"\d\s+[-–—.]\s+\d", raw))
        or "\ufffd" in raw
    )


def canonicalize_label(raw: str, *, known_labels: set[str] | None = None) -> LabelCandidate:
    """把一段图注文本/图号原文修成规范 label，并给出置信度。

    ``known_labels``：同一本书/同一章已经见到的 ``fig:x-y`` 集合，用来做上下文校验。
    修复后的编号必须「看起来属于本章的编号体系」，否则降置信、不硬改。
    """
    if not raw or not raw.strip():
        return LabelCandidate(reason="空文本")
    original = LABEL_RE.search(raw.strip())
    text = _clean_label_text(raw)
    match = LABEL_RE.search(text)
    broken_tail = bool(match) and bool(re.match(r"^[.\-]\s*[a-zA-Z]", text[match.end():].lstrip()[:3]))
    if match and not broken_tail:
        norm = normalize_label(match.group(0))
        if norm and norm.startswith("fig"):
            repaired = _needs_repair(raw)
            # 引用要写原文写法（「图 4.2.1」不是「图4.2.1」）：原文能解析且等价就用原文
            raw_label = match.group(0).strip()
            if original and normalize_label(original.group(0)) == norm:
                raw_label = original.group(0).strip()
            return LabelCandidate(
                label_raw=raw_label,
                label_norm=norm,
                confidence=CONF_REPAIRED if repaired else CONF_REVIEW,
                source="rule_repair" if repaired else "text",
                reason="分隔符/空格修复后解析" if repaired else "直接解析",
            )

    # 直接解析失败或尾部还有字母段（图 4.e.1）：把 OCR 错字还原成数字再试
    for m in re.finditer(r"(图|表)\s*([0-9a-zA-Z]{1,3}(?:[.\-][0-9a-zA-Z]{1,3}){0,2})", text):
        kind, body = m.group(1), m.group(2)
        for candidate_body in _cartesian_segments(body):
            candidate_text = f"{kind}{candidate_body}"
            cm = LABEL_RE.search(candidate_text)
            if not cm:
                continue
            # 候选必须把整段数字都吃干净，否则「4.e.1」会被当成「图4」收下
            residual = candidate_text[cm.end():].lstrip()
            if residual and re.match(r"^[.\-a-zA-Z0-9]", residual):
                continue
            norm = normalize_label(cm.group(0))
            if not norm or not norm.startswith("fig"):
                continue
            conf, reason = _context_check(norm, known_labels)
            return LabelCandidate(
                label_raw=cm.group(0).strip(),
                label_norm=norm,
                confidence=conf,
                source="rule_repair",
                reason=reason,
            )
    return LabelCandidate(reason="无法解析出图号")



def _cartesian_segments(body: str) -> list[str]:
    """把 ``4.e.1`` 这种 body 按分隔符拆段，对每段做 OCR 数字还原。"""
    parts = re.split(r"([.\-])", body)
    segments: list[list[str]] = []
    for part in parts:
        if part in (".", "-"):
            segments.append([part])
        else:
            segments.append(_digit_variants(part))
    out = [""]
    for opts in segments:
        out = [prefix + opt for prefix in out for opt in opts]
    return out


def _context_check(norm: str, known: set[str] | None) -> tuple[float, str]:
    """用同书已见编号校验修复候选：前缀对得上吗？序号跳得离谱吗？"""
    if not known:
        return CONF_INFER, "无上下文可校验"
    tail = norm.split(":", 1)[1]
    parts = tail.split("-")
    # 同前缀（图 4.3.x 的 4-3）已出现过 → 可信度更高
    if len(parts) >= 3:
        prefix = "-".join(parts[:-1])
        if any(k.split(":", 1)[-1].startswith(prefix + "-") for k in known):
            return CONF_REPAIRED, f"同前缀 {prefix} 已出现过"
    # 章号对得上
    if any(k.split(":", 1)[-1].split("-")[0] == parts[0] for k in known):
        return 0.65, f"章号 {parts[0]} 对得上"
    return CONF_INFER, "与已见编号无共同前缀"


# ---------------------------------------------------------------------------
# 子图归组 / 继承
# ---------------------------------------------------------------------------
def _caption_related(a, b) -> bool:
    """两个图块的 caption 是否指向同一个图注（相同 / 包含 / 共用前缀）。"""
    ca = (a.caption or "").strip()
    cb = (b.caption or "").strip()
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    return len(short) >= 4 and short in long_


def _same_group(a, b) -> bool:
    """同一张图被拆成多个图块的两个判据（任一成立即可）。

    1. 图注相同/包含（最可靠）；
    2. 几何上属于同一图区：水平/垂直间隙都 ≤ ZONE_GAP（2×2 排列的子图靠
       中间元素连通成一组；独立的相邻图靠「簇内多父号」再拆开）。
    """
    if _caption_related(a, b):
        return True
    if a.page != b.page:
        return False
    hgap, vgap = _zone_gap(a.bbox, b.bbox)
    return hgap <= ZONE_GAP and vgap <= ZONE_GAP


def _zone_gap(a: list[float], b: list[float]) -> tuple[float, float]:
    """两个 bbox 的水平/垂直间隙（pt，重叠时为负）。"""
    hgap = max(a[0], b[0]) - min(a[2], b[2])
    vgap = max(a[1], b[1]) - min(a[3], b[3])
    return hgap, vgap


def _center_dist(a, b) -> float:
    ax = (a.bbox[0] + a.bbox[2]) / 2
    ay = (a.bbox[1] + a.bbox[3]) / 2
    bx = (b.bbox[0] + b.bbox[2]) / 2
    by = (b.bbox[1] + b.bbox[3]) / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _norm_tail(norm: str) -> str:
    return norm.split(":", 1)[1] if ":" in norm else norm


def _looks_like_subfigure_caption(caption: str | None) -> bool:
    """图注是否以子图字母开头（「(a) 等效结构」「a)方案1」「（b）」）。"""
    cap = (caption or "").strip()
    if not cap:
        return False
    return bool(
        re.match(r"^[（(]\s*[a-hA-H]\s*[)）]\s*\S", cap)
        or re.match(r"^[a-hA-H]\s*[)）.、:：]\s*\S", cap)
    )


def _near_parent_label(parent_norm: str, child_norm: str) -> bool:
    """OCR 把子图号读坏时的判据：父号的尾段是子图号尾段的前缀，只多出 1–2 个字符。

    例：父号 ``fig:4-50``、候选 ``fig:4-500``（「图 4-50a」→「图 4-500」）。
    反过来不成立（候选项比父号短），避免把 7-1 当成 7-10 的父号时误并。
    """
    a, b = _norm_tail(parent_norm), _norm_tail(child_norm)
    if not a or not b or a == b or not b.startswith(a):
        return False
    extra = b[len(a) :]
    return 1 <= len(extra) <= 2 and all(ch.isalnum() for ch in extra)


def _spatial_order(members: list) -> list:
    """按阅读顺序排子图：先分行（垂直中心接近的算一行），行内从左到右。

    为什么不能直接按 ``(top, left)`` 排：并排子图的上边缘常差几 pt（一个高一点），
    直接按 top 排会把右边的图排到左边。真实课本里 03/04/05 三本都踩到这个，
    表现为「(a)/(b) 标反」。行容差取中位高度的 35%（最小 8pt），既能容忍几 pt 的
    错位，又不会把上下两行并成一行。
    """
    if not members:
        return []
    heights = sorted(max(1.0, f.bbox[3] - f.bbox[1]) for f in members)
    median_h = heights[len(heights) // 2]
    tol = max(8.0, median_h * 0.35)

    ordered = sorted(members, key=lambda f: (f.bbox[1] + f.bbox[3]) / 2)
    rows: list[list] = []
    centers: list[float] = []
    for fig in ordered:
        cy = (fig.bbox[1] + fig.bbox[3]) / 2
        if rows and abs(cy - centers[-1]) <= tol:
            rows[-1].append(fig)
            # 行心用均值：第一个元素可能偏高/偏低，不能拿它当整行基准
            centers[-1] = sum((f.bbox[1] + f.bbox[3]) / 2 for f in rows[-1]) / len(rows[-1])
        else:
            rows.append([fig])
            centers.append(cy)

    result: list = []
    for row in rows:
        result.extend(sorted(row, key=lambda f: f.bbox[0]))
    return result


def _cluster(items: list) -> list[list]:
    """把同页图块按「图注相关 / 几何相邻」连通成图区簇（union-find）。"""
    n = len(items)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            if _same_group(items[i], items[j]):
                union(i, j)
    buckets: dict[int, list] = {}
    for i, item in enumerate(items):
        buckets.setdefault(find(i), []).append(item)
    return list(buckets.values())


def group_subfigures(figures: list) -> dict:
    """同一系列的子图归组，没有编号的子图继承父图号。

    真实课本里一个 2×2 排布的系列图经常被版面模型切成 4 个图块，只有 1 个图块
    带着完整图注（「图 4.3.1 …(a)…(b)…」），其余图注只有「(a)」「(b)」或空。
    做法：按图区簇聚合 → 簇内每个无号图块归给**距离最近的带号图块**（同簇可能
    不止一个父图号，比如一页上上下两张独立图）；每个父号一个 ``group_key``。

    子图字母按**阅读顺序**分配（见 ``_spatial_order``），不再从 ``label_raw`` 末尾
    猜字母——OCR 会把「图 4.3.2J」读出一个 J，那是脏字符，不是子图 j。

    每次调用都先清空 ``group_key`` / ``sublabel`` 再重算：阶段 2 的 OCR 融合会改标签，
    第二次调用必须从干净状态出发，否则会留下上一轮的组（父号已被改掉的僵尸组）。

    就地修改 ``figures``（group_key/sublabel/label_raw/label_source），返回统计。
    """
    for fig in figures:
        fig.group_key = None
        fig.sublabel = None

    stats = {"groups": 0, "inherited": 0, "reassigned": 0}
    reassigned_ids: set[int] = set()
    by_page: dict[int, list] = {}
    for fig in figures:
        by_page.setdefault(fig.page, []).append(fig)

    for page, items in by_page.items():
        if len(items) < 2:
            continue
        for zone in _cluster(items):
            if len(zone) < 2:
                continue
            labeled = [f for f in zone if f.label_raw and normalize_label(f.label_raw or "")]
            if not labeled:
                continue  # 整簇都没号：阶段 2 交给双 OCR，规则层不猜
            # 同一个 label_norm 的多个图块 = 同一张图被切成多块（或子图复用同一图注）
            label_groups: dict[str, list] = {}
            for member in labeled:
                norm = normalize_label(member.label_raw or "")
                if norm and norm.startswith("fig"):
                    label_groups.setdefault(norm, []).append(member)
            if not label_groups:
                continue
            parents = {norm: members[0] for norm, members in label_groups.items()}

            # OCR 常把子图号读坏：「图 4-50a」→「图 4-500」。这种块如果留在自己的
            # 父号上，就会变成一张孤立图，整个系列少一块、字母全错位。判据：
            # 图注以 (a)/a) 开头 + 尾段是某个父号尾段的前缀扩展（只多 1–2 个字符）。
            for norm in list(label_groups):
                members = label_groups.get(norm) or []
                if len(members) != 1 or norm not in parents:
                    continue
                member = members[0]
                if not _looks_like_subfigure_caption(member.caption):
                    continue
                candidates = [p for p in parents if p != norm and _near_parent_label(p, norm)]
                if not candidates:
                    continue
                best = min(candidates, key=lambda p: _center_dist(member, parents[p]))
                label_groups[best].extend(members)
                del label_groups[norm]
                parents.pop(norm, None)
                reassigned_ids.add(id(member))
                stats["reassigned"] += 1

            assign: dict[str, list] = {norm: list(members) for norm, members in label_groups.items()}
            for member in zone:
                if member in labeled:
                    continue  # 含被重归组的「图 4-500」——它已经在父号的 bucket 里了
                nearest = min(parents, key=lambda norm: _center_dist(member, parents[norm]))
                assign[nearest].append(member)

            for norm, members in assign.items():
                needs_group = len(members) > 1 or len(label_groups[norm]) > 1
                if not needs_group:
                    continue
                parent = parents[norm]
                stats["groups"] += 1
                key = f"p{page}-g{stats['groups']}"
                for idx, member in enumerate(_spatial_order(members)):
                    member.group_key = key
                    if member is not parent:
                        forced = id(member) in reassigned_ids
                        # 已经过 OCR 交叉校核的结论（一致/冲突/待确认）不被子图继承覆盖；
                        # 但「图注以子图字母开头 + 尾段是父号前缀扩展」的那类 OCR 坏号要强改，
                        # 否则它会一直挂着 fig:4-500 这种不存在的号。
                        if member.label_source in _KEEP_SOURCES and not forced:
                            pass
                        else:
                            member.label_raw = parent.label_raw if forced else (member.label_raw or parent.label_raw)
                            member.label_source = "rule_inherit"
                            member.label_confidence = CONF_INHERIT
                            stats["inherited"] += 1
                    member.sublabel = chr(ord("a") + idx) if idx < 26 else None
                    if member.label_source is None and member.label_raw:
                        member.label_confidence = CONF_REVIEW
                        member.label_source = "text"
    return stats


# ---------------------------------------------------------------------------
# 修复 / 回填
# ---------------------------------------------------------------------------
def repair_missing_labels(figures: list, *, known_labels: set[str] | None = None) -> dict:
    """caption 里其实有图号、但 label 为空的，补上（并继承子图）。"""
    stats = {"repaired": 0, "inherited": 0}
    for fig in figures:
        if fig.label_raw is not None:
            continue
        if not (fig.caption or "").strip():
            continue
        cand = canonicalize_label(fig.caption, known_labels=known_labels)
        if not cand.label_norm:
            continue
        # label_raw 要尽量保留原文写法（引用时写进论文的是「图 4.2.1」而不是归一化形式）；
        # 只有原文本身解析结果和修复结果不一致时（图 4.e.1 → fig:4-5-1），才用修复后的写法。
        original = LABEL_RE.search(fig.caption)
        original_norm = normalize_label(original.group(0)) if original else None
        fig.label_raw = (
            original.group(0).strip() if original and original_norm == cand.label_norm else cand.label_raw
        )
        fig.label_source = "rule_repair"
        fig.label_confidence = cand.confidence
        stats["repaired"] += 1
    return stats


def apply_label_rules(doc) -> dict:
    """对一份 ParsedDocument 跑完整规则层（返回统计，供日志/报告用）。

    顺序很重要：先修 caption 里漏提的编号，再分组继承（分组依赖父图号已存在），
    最后统一给没有来源的图打上 text/低置信标记。
    """
    known = set()
    for block in doc.blocks:
        if block.type != "caption":
            continue
        norm = normalize_label(block.text or "")
        if norm and norm.startswith("fig"):
            known.add(norm)
    for fig in doc.figures:
        norm = normalize_label(fig.label_raw or "")
        if norm and norm.startswith("fig"):
            known.add(norm)

    stats = repair_missing_labels(doc.figures, known_labels=known)
    grouped = group_subfigures(doc.figures)
    stats.update(grouped)
    for fig in doc.figures:
        if fig.label_raw and fig.label_source is None:
            fig.label_source = "text"
            fig.label_confidence = CONF_REVIEW
        elif not fig.label_raw:
            fig.label_source = fig.label_source or "none"
            fig.label_confidence = CONF_NONE
    stats["unlabeled"] = sum(1 for f in doc.figures if not f.label_raw)
    stats["labeled"] = sum(1 for f in doc.figures if f.label_raw)
    return stats


def caption_noise(doc) -> dict:
    """图注文本里可见的噪声统计（决定要不要做双 OCR 交叉校核）。

    为什么需要它：纯文字层质量分抓不到「合法但错」的字（交又/涧湿），
    但图注编号的碎片化/空格化是**可见的**——真实课本里这类书正是最该复核的。
    """
    broken = spaced = repaired = captions = 0
    for fig in doc.figures:
        cap = fig.caption or ""
        if not cap:
            continue
        captions += 1
        if _BROKEN_LABEL_RE.search(cap) or _BROKEN_LETTER_RE.search(cap):
            broken += 1
        if re.search(r"\d\s+[-–—.]\s+\d", cap):
            spaced += 1
        if fig.label_source == "rule_repair":
            repaired += 1
    return {"captions": captions, "broken": broken, "spaced": spaced, "repair": repaired}


def needs_text_layer_audit(doc, quality: "TextQuality", min_score: float = 0.8) -> bool:
    """文字层书要不要跑双 OCR 校核：质量低，或图注里已经看到可见噪声/修复痕迹。

    ``min_score`` 来自 ``settings.text_quality_min``（原先硬编码 0.8，改 env 不生效）。
    """
    if not doc.text_layer:
        return False
    if quality.score < min_score:
        return True
    noise = caption_noise(doc)
    return noise["broken"] > 0 or noise["spaced"] > 0 or noise["repair"] > 0


# ---------------------------------------------------------------------------
# 文字层质量（阶段 1 只记录，阶段 2 用来触发 OCR）
# ---------------------------------------------------------------------------
def assess_text_layer(doc) -> TextQuality:
    """给文字层打分（0–1，越高越可信）。

    这不是语言模型困惑度，而是「肉眼可见的坏」的几个代理指标：
    乱码/替换字符、控制符、碎片化编号（图 10/0、图 10凸7）、可疑短行占比、
    行的重复率。阶段 2 会再和两套 OCR 的一致性交叉校准。
    """
    texts = [b.text or "" for b in doc.blocks if b.type in ("text", "caption", "title", "section")]
    joined = "".join(texts)
    chars = [c for c in joined if not c.isspace()]
    total = len(chars)
    if total == 0:
        return TextQuality(score=0.0, flags={"empty": True})

    garbage = sum(1 for c in chars if c in _SUSPECT_CHARS or unicodedata.category(c) == "Cc")
    garbage_ratio = garbage / total

    # 碎片化编号：图注/正文里出现「图 10/0」「图 10凸7」「图 4.e.1」这种形态
    broken = len(_BROKEN_LABEL_RE.findall(joined)) + len(_BROKEN_LETTER_RE.findall(joined))
    broken_per_1k = broken * 1000 / total

    # 可疑短行占比（文字层坏的时候经常出现 1–2 字的行）
    lines = [ln for ln in re.split(r"[\r\n]+", joined) if ln.strip()]
    short_ratio = sum(1 for ln in lines if len(ln.strip()) <= 2) / len(lines) if lines else 0.0

    # 重复行：文字层错位时同一行会被抽两遍
    repeated = 1 - (len(set(lines)) / len(lines)) if lines else 0.0

    score = 1.0
    score -= min(0.35, garbage_ratio * 20)
    score -= min(0.35, broken_per_1k * 1.5)
    score -= min(0.2, short_ratio * 0.6)
    score -= min(0.2, repeated * 0.8)
    score = max(0.0, min(1.0, score))
    return TextQuality(
        score=round(score, 3),
        flags={
            "garbage_ratio": round(garbage_ratio, 4),
            "broken_labels": broken,
            "broken_per_1k": round(broken_per_1k, 2),
            "short_line_ratio": round(short_ratio, 3),
            "repeated_line_ratio": round(repeated, 3),
            "chars": total,
        },
    )


__all__ = [
    "CONF_INFER",
    "CONF_INHERIT",
    "CONF_REPAIRED",
    "CONF_REVIEW",
    "LOW_CONFIDENCE",
    "LabelCandidate",
    "TextQuality",
    "apply_label_rules",
    "assess_text_layer",
    "canonicalize_label",
    "caption_noise",
    "group_subfigures",
    "needs_text_layer_audit",
    "repair_missing_labels",
]
