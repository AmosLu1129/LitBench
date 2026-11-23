# SPDX-License-Identifier: MIT
"""``python -m litbench_mcp`` 的入口（console script 也指向 ``main``）。"""

from __future__ import annotations

from .cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
