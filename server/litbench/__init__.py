# SPDX-License-Identifier: AGPL-3.0-or-later
"""LitBench 服务端。

分层约定（别破坏它）：
    routers/   只用 HTTP 语义，不写 SQL
    search.py  查询构建，不碰 HTTP
    models.py  表结构，不写业务
    adapters/  外部解析引擎，**AGPL 组件只能出现在这里**，且必须走独立容器
"""

__version__ = "0.0.1"
