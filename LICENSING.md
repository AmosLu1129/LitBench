<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 许可证说明（混合模式 C）

本仓库是**双许可证**，按目录划分：

| 路径 | 许可证 | 为什么 |
| --- | --- | --- |
| 仓库根（`server/` `web/` `adapters/`） | AGPL-3.0-or-later | 服务端会（可选）集成 AGPL 组件；同时挡住「拿去做闭源 SaaS」 |
| `schemas/` | MIT | 领域卡片定义要社区贡献，越宽松越好 |
| `mcp/` | MIT | 要被第三方 agent 嵌入 |
| `templates/` | 随各模板 | LaTeX 模板多为 LPPL，保持原有许可证，不重许可 |

## 三条必须守住的规则

1. **每个源文件加 SPDX 头**
   `# SPDX-License-Identifier: AGPL-3.0-or-later`（主仓）或 `MIT`（`schemas/` `mcp/`）

2. **AGPL / GPL 组件只能以独立容器或子进程接入**
   放在 `adapters/` 下，通过 HTTP 或 CLI 调用。不 import、不 vendor、不 submodule、不打进主镜像。
   见 [THIRD_PARTY.md](THIRD_PARTY.md)。

3. **`schemas/` 与 `mcp/` 不得 import 主仓代码**
   只允许通过 HTTP 与主服务通信。一旦 import，它们会被 AGPL 传染，MIT 声明失效。

## 为什么不全 AGPL，也不全 MIT

- 全 AGPL：`schemas/` 和 `mcp/` 是最需要外部贡献的两块（领域卡片、客户端集成），copyleft 会劝退一半贡献者。
- 全 MIT：挡不住有人把服务端包一层做付费墙。
- 混合：服务端 copyleft 挡住白嫖，边缘 permissive 换取生态。

## 贡献

- 用 DCO（`git commit -s`），不搞 CLA。
- 提交即同意以对应目录的许可证发布。
