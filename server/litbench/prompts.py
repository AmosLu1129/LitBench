# SPDX-License-Identifier: AGPL-3.0-or-later
"""prompt registry：prompt 以文件形式入库（``litbench/prompts/*.md``），版本 = 内容哈希。

为什么用文件而不是硬编码字符串：
* prompt 改动可 diff、可 review；
* ``prompt_version`` 跟着内容哈希走，摘要行能追溯「这段文字是哪版 prompt 生成的」；
* M2b 调 prompt 时可以直接对比不同版本的产出。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
_cache: dict[str, "Prompt"] = {}


class PromptNotFoundError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    text: str
    version: str  # sha256(text)[:12]


def load_prompt(name: str) -> Prompt:
    """加载并缓存一个 prompt；``version`` 是内容哈希，改一个字就会变。"""
    cached = _cache.get(name)
    if cached is not None:
        return cached
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"prompt 不存在：{path}")
    text = path.read_text(encoding="utf-8")
    prompt = Prompt(
        name=name,
        text=text,
        version=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
    )
    _cache[name] = prompt
    return prompt


def render(prompt: Prompt, **values: object) -> str:
    """把 ``{{name}}`` 占位符替换成实际值（不做 str.format，避免正文里的花括号炸掉）。"""
    out = prompt.text
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


__all__ = ["PROMPT_DIR", "Prompt", "PromptNotFoundError", "load_prompt", "render"]
