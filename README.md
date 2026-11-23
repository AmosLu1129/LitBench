<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# LitBench · 文献证据工作台

把文献里的段落、表、图、曲线变成**结构化、可检索、可溯源、能直接进写作流程**的资产。

它不是「AI 读文献工具」，而是从「读过」到「写出带正确引用的稿子」之间那层**证据中间层**——这段路今天全靠手工搬。

> **状态**：P0 / P1 与 M1/M2 知识库、章节摘要、AI 一键总结、图提取 AI 纠错已交付；
> **M3 正式化 + M3c 框复核已交付**（审计 / 自动接受器 / 回滚 / agent 任务租约 / 任务面板；
> 整页+红框标注+裁图三图多模态框复核，默认只提议）；
> **M4 只读 MCP + M5 写侧 MCP 已交付**（`mcp/`，8 个 read + 8 个提案/任务工具，
> stdio + Streamable HTTP，读/写 scope 分离）；
> **M5 写作侧已交付**（claim 卡片 + 派生对比矩阵 + LaTeX/BibTeX 导出 + 确定性引用校验；
> **References 条目生成：GB/T 7714 / APA / IEEE / MLA / Chicago / Nature / Science / ACS /
> Elsevier / Vancouver / BibTeX / RIS / 自定义模板共 13 种预设**）；
> 顶栏「写作」入口）；
> **团队共享已交付（2026-11-15，前后端）**：本地账号 / 登录 / 管理员建号 / 成员管理；
> 工作组 / 文献级共享 / 私有库与团队库隔离 / 笔记收藏按用户隔离
> （迁移 0014 + 7 个 API + 全站可见性）；顶栏「个人 / 工作组」切换、卡片共享菜单、
> 工作组管理浮层；A/B 两个账号真机验收截图 `docs/assets/team/06–11`
> （细节见 [docs/15](docs/15-知识库与Agent协作长线计划.md) §12.28）。
> **MCP 内部服务令牌已交付**：认证打开后 MCP 用 `LITBENCH_MCP_API_TOKEN` 对应 API 的
> `INTERNAL_API_TOKEN`，以 `system` 身份访问全库；compose 与 `.env.example` 已接线
> （[docs/15](docs/15-知识库与Agent协作长线计划.md) §12.29）。
> **Docker + Postgres 已首次真跑通过（2026-11-15）**：四容器 healthy、迁移 0001→0014、
> worker 解析、认证/共享、内部令牌、`down`+`up` 后数据不丢；真跑修了 3 个部署 bug
> （greenlet、布尔默认值、健康探针 401），见 [docs/15](docs/15-知识库与Agent协作长线计划.md) §12.30。
> **部署收尾已交付**：局域网 HTTPS 反代（`deploy/nginx-lan.conf` + profile `lan`）、
> 备份/恢复脚本（`scripts/backup.sh` / `restore.sh`）、MCP 容器化（`mcp/Dockerfile` + profile `mcp`）、
> Windows+WSL 并发验收；截图 `docs/assets/deploy/`。
> **学位论文 LaTeX 包已交付（2026-11-15）**：`paper.entry_type` + 学位论文著录字段（迁移 0015）；
> GB/T 7714 `[D]`、BibTeX `@phdthesis`/`@mastersthesis`、RIS `THES`；
> `/api/thesis/export`（ctexbook 骨架 + `refs.bib` + GB/T 参考文献）与 `/api/thesis/check`
> （`\cite{}` / `\ref{fig:*}` / `\ref{tab:*}` 交叉检查）；写作台「学位论文」入口；
> xelatex 真机编译 10 页、截图 `docs/assets/thesis/`（细节见 [docs/17 §11.3](docs/17-下一步路线.md)）。
> **表格进界面已交付（2026-11-15）**：文献详情「表格」页签（结构化 HTML 渲染 /
> 表题与页码 / 点回原文 / 复制引用 / 写进卡片）；白名单清洗保留合并单元格，
> 截图 `docs/assets/tables/`（细节见 [docs/17 §2](docs/17-下一步路线.md) 第 3 项）。
> **写作台小修已交付（2026-11-15）**：claim 编辑历史（迁移 0016，改动前留快照，可查看/恢复，
> 恢复前也留一份）、对比矩阵自定义维度（10 个可选维度，默认仍是原 7 行）；
> 截图 `docs/assets/writing/`（细节见 [docs/17 §2](docs/17-下一步路线.md) 第 4 项）。
> **M9.1a 检索升级已交付（2026-11-15）**：语义检索从 ILIKE 换成 **BM25**（中文单字+二元组、
> 英文按词、字段加权），`/api/papers?q=` 同步升级（标题/摘要/作者/citekey/标签），
> 出处检索路径不变；20 条真实查询评估 figure hit@5 5%→100%、paper hit@1 5%→100%，0 回归
> （`scripts/eval_retrieval.py`，细节见 [docs/15 §12.35](docs/15-知识库与Agent协作长线计划.md)）。
> **M9.3a 表格 LaTeX 导出已交付（2026-11-15）**：结构化表 → `tabular` / 可粘贴浮动体，
> `colspan`/`rowspan` 保留、Unicode 数学归一、缺表题不编造；表格页签「复制 LaTeX」；
> 真机 3 张真实表 + 合成 rowspan 表 xelatex 编译通过（`docs/assets/tables/04-*`，
> 细节见 [docs/15 §12.36](docs/15-知识库与Agent协作长线计划.md)）。
> **M9.3b 表格结构对比已交付（2026-11-15）**：同一批 9 篇 arXiv 论文（按 sha 去重）分别用
> Docling tableformer 与 MinerU `standard` 全页解析；25 对表，行列一致率 68%、格位 IoU 0.912、
> 单元格文本一致 71%、3-gram 文本 F1 0.974。MinerU 会拆长单元格/补空列，暂不切换结构化表格来源
> （`scripts/eval_table_structure.py`，细节见 [docs/15 §12.37](docs/15-知识库与Agent协作长线计划.md)）。
> **M9.3c 宽表自动换行已交付（2026-11-15）**：结构化表导出 LaTeX 时按内容估算列宽，
> 宽表自动把 `l` 列换成按比例的 `p{…\linewidth}`（合并列/合并行也换行），超长英文串、
> 列数过多会 warning 建议横排或拆表；接口 `?width=auto|plain`，前端 toast 提示。
> 真机 xelatex 编译 auto 0 Overfull、plain 溢出 46.5pt/1717pt
> （`docs/assets/tables/05-*`，细节见 [docs/15 §12.38](docs/15-知识库与Agent协作长线计划.md)）。
> **M9.4 MCP 质量已交付（2026-11-15）**：`cite.resolve(review=true)` 在词面档外可选模型复核，
> 顶层 `tier=lexical|model` 标明实际档位，模型不可用时逐条回退并说明；`dataset.golden` 增
> `bbox_only=true`（`format=bboxes`）导出已复核图框标注集。真机模型复核 supported 0.99；
> M9.4 交付时后端 390 / MCP 123（细节见 [docs/15 §12.39](docs/15-知识库与Agent协作长线计划.md)；
> 随后 M9.1b-lite +7 → 397，见 §12.40）。
> **M9.1b-lite 中文查询改写已交付（2026-11-15）**：复用同一套 chat 模型配置，把中文
> 查询改写成英文术语再走 BM25（`/api/figures`、`/api/papers?q=`）；响应带
> `query_expanded`/`expanded_keywords`/provider/model，并明确标注「不是向量召回」。
> 真机「多孔介质强化传热」图检索 total 0→131（openai-chat/deepseek-v4.1-flash）。
> **真正的 embedding 向量召回仍未做**（chat 不提供 embedding）；**M9.2 精度量化已放弃**
> （2026-11-15 产品决策，不再做人工标注）；下一阶段转 M8 写作完整度。
> **M8 第一块 CSL 引用引擎已交付（2026-11-16）**：写作台 References 可切
> 「官方 CSL 样式」（GB/T 7714-2015 / APA 7 / IEEE），引擎用 `pandoc --citeproc`
> （与 Zotero/Word 一致；实测后没有采用 citeproc-py）；新增 CSL JSON 导出
> （`POST /api/references/csl-json`），13 种手写模板保持默认。开发机用
> `scripts/get_pandoc.py` 装便携版 pandoc；没装时 CSL 入口 503，模板照常。
> 测试基线现为**后端 510 / MCP 123**（细节见
> [docs/15 §12.47](docs/15-知识库与Agent协作长线计划.md)）。
> **M8 第二块 Word/PDF 导出已交付（2026-11-16）**：写作台「导出」页签可把 claim 草稿
> 下载成 `.docx` / `.pdf`（pandoc --citeproc + 官方 CSL；PDF 用 typst 引擎，不用
> xelatex——API 进程拉 xelatex 会被 MiKTeX 权限问题挡住）。依赖
> `scripts/get_pandoc.py` + `scripts/get_typst.py`，缺工具时按钮置灰/接口 503；
> 细节见 [docs/15 §12.43](docs/15-知识库与Agent协作长线计划.md)。
> **M8 第三块 Crossref/arXiv 元数据补全已交付（2026-11-16）**：References →
> 「补全卷期页」可在线查 DOI/arXiv/题名搜索，逐字段确认后写入；默认只填空白，
> 覆盖已有值要显式勾选，不写 Zotero。细节见
> [docs/15 §12.44](docs/15-知识库与Agent协作长线计划.md)。
> **M8 第四块写作大纲 + 引用/图表交叉检查已交付（2026-11-16）**：写作台新增「大纲」
> 页签，claim 按章节别名归章/排序，正文图号与卡片证据对不上、`\cite` 解析不了会
> 列提示；可复制/下载 Markdown 大纲。细节见
> [docs/15 §12.45](docs/15-知识库与Agent协作长线计划.md)。
> **外部审查前自修批 A 已完成（2026-11-16）**：pandoc `--sandbox`（防本地文件读取/
> SSRF）、`\cite{}` 不再被 Word/PDF 静默丢弃、导出可见性与 500 张上限、引用索引
> 不再按 300 篇截断、metadata 外部响应上限与错误映射测试、CI 安装 pandoc/typst +
> Noto CJK。当时后端 467 测试。细节见 [docs/15 §12.46](docs/15-知识库与Agent协作长线计划.md)。
> **外部审查前自修批 B 已完成（2026-11-16）**：补 auth 打开时的权限矩阵（匿名 401 全覆盖、
> 私有/共享只读边界、元数据/大纲/CSL/论文包/文档导出按 id 统一 404、agent 任务/审计/
> 自动接受/模型配置/导入历史收紧为管理员、golden/jobs/proposal 统计按可见性过滤）、
> 停用账号立刻踢掉旧 session、管理员最后一个账号保护、共享文献重复上传不再触发重解析、
> 审计 actor 取当前用户；新增 `scripts/check_doc_counts.py` 校验文档当前基线计数（接入 CI）。
> 细节见 [docs/15 §12.47](docs/15-知识库与Agent协作长线计划.md)。
> **自修批 C 已完成（2026-11-16）**：外部工具查找收敛到 `toolchain.py` 并加
> pandoc ≥3.0 版本门槛（低版本 CSL/Word/PDF 明确 503，模板照常）；摘要编辑/发布/
> 回滚的审计 actor 强制当前用户；内部服务令牌少于 16 字符启动即拒绝；补 MCP system
> 令牌在任务/审计/golden/自动接受管理面上的回归测试。细节见
> [docs/15 §12.48](docs/15-知识库与Agent协作长线计划.md)。
> **M6a–M6c 欢迎页已交付**（three.js 独立懒加载 chunk / 34s 一镜到底 / 后处理景深与光轴 /
> 高度场地形与天空散射 / 三维桃枝；剩余为美术精修与移动端）；
> **后端 510 个测试 + MCP 123 个测试**。
> **本机 dev 库**：2026-11-13 导入 10 篇英文热/流体 arXiv 论文作测试语料
> （5,695 block / 140 图 / 19 表；清库前旧备份仍在 `.data/backups/`）。
> **形态**：自部署 Web 应用；本地开发不需要 Docker / Postgres（SQLite 即可）。
> **许可证**：混合 —— 服务端 AGPL-3.0-or-later，`schemas/` 与 `mcp/` MIT。见 [LICENSING.md](LICENSING.md)。
> **上下文压缩交接**：先读 [docs/09 §0 速览 + 阅读清单](docs/09-现状交接.md)，剩余开工清单在 [docs/17 §11.4](docs/17-下一步路线.md)。
> **外部模型审查**：先读 [SOTA-REVIEW-GUIDE.md](SOTA-REVIEW-GUIDE.md)（代码地图/设计取舍/已知问题/省 token 路线）。

## 为什么做

- 现有 AI 文献工具已经覆盖「读」：OCR、结构化总结、多文档问答、引用条目复制。这层没有壁垒。
- 空白在「写」和「图」：跨文献对比、骨架 + 证据挂载、引用一致性校验、图表证据复用。
- 面向全领域、按图表占比最高准备：图/表是一等公民，不是文献的附件。

## 不做什么

- 不做 PDF 阅读器 / 文献管理器（寄生 Zotero）
- 不做通用「和文献对话」与自由文本一键总结（已商品化）
- 不做全自动写正文（学术风险）
- 不做知识图谱可视化大屏
- 不打包 AGPL 代码进主镜像

## 技术栈

| 层 | 选型 |
| --- | --- |
| 前端 | React 18 + Vite（无 UI 框架）+ 自写 CSS |
| 后端 | FastAPI（Python 3.11+） |
| 解析 | pdfium（默认，零模型）· Docling（增强档）· MinerU（AGPL，可选容器） |
| 存储 | Postgres（部署）/ SQLite（本地开发）；当前为本地文件存储，S3 兼容对象存储**规划中** |
| 队列 | 无独立队列服务：任务队列 = DB `job` 表 |
| 引用 / LaTeX | 自写确定性导出（LaTeX 骨架 + `refs.bib` + `\cite{}` 校验 + 13 种 references 格式预设）；**官方 CSL 引擎**（GB/T 7714-2015 / APA 7 / IEEE，`pandoc --citeproc` 子进程）+ CSL JSON 导出 |
| Word / PDF | **claim 草稿导出**（`pandoc --citeproc` → `.docx`；`.pdf` 走 typst 引擎），不做学校模板/图表排版 |
| 部署 | Docker Compose |

> 解析默认走 `pdfium` 轻量档（192 页约 30 秒）；图号可定位率 68%（精度优先，
> 已拒绝「把正文当图」的误检，见 [P0 验证报告](docs/05-P0验证报告.md)）。
> 通栏+双栏混排的复杂版面交给 Docling 增强档。

## 目录

```
compose.yaml          自部署栈（mineru 在 ocr profile）
LICENSING.md          混合许可证说明
THIRD_PARTY.md        第三方组件与禁用清单
server/               FastAPI + workers + adapters
  adapters/           解析引擎适配器（Docling / MinerU 隔离在这里）
  requirements.txt    只允许 permissive 依赖
  pyproject.toml
web/                  前端（工作台、图表墙、草稿）
schemas/              领域卡片定义（MIT；规划中，当前未被读取）
mcp/                  MCP 出口（MIT；8 只读 + 8 提案/任务工具 + stdio/HTTP + token/审计）
templates/            LaTeX 模板（规划中；CSL 样式已内置在 server/litbench/csl_styles/）
scripts/              许可证检查、pandoc/typst 下载、验收脚本等
docs/                 设计、评审、ADR
```

## 快速开始

服务端可以先独立跑（不需要 Docker、不需要 Postgres）：

```bash
cd server
python -m venv ../.venv
../.venv/Scripts/pip install -r requirements.txt pytest pytest-asyncio httpx aiosqlite uvicorn
../.venv/Scripts/python -m pytest -q          # 510 passed
../.venv/Scripts/python -m uvicorn litbench.main:app --reload
# 打开 http://localhost:8000/api/docs
```

**带界面的本地开发（推荐）**——先用样本 PDF 生成一份 SQLite 开发库：

```bash
# 从仓库根目录执行；先保证依赖装在 .venv
.venv/Scripts/python scripts/seed_dev.py       # 解析 .data/samples，产出 .data/dev.db + 图片
# 可选：官方 CSL 样式渲染需要 pandoc（不装也能用 13 种手写模板；CSL 入口会返回 503）
.venv/Scripts/python scripts/get_pandoc.py     # 下载钉版本 3.14 到 .data/tools/pandoc/
# 可选：PDF 导出再装 typst（不装则 Word 可用、PDF 按钮置灰；GitHub 慢会自动换镜像）
.venv/Scripts/python scripts/get_typst.py      # 下载钉版本 0.15.1 到 .data/tools/typst/
# 脚本结束时会打印接下来要跑的两条命令（API + 前端）
cd web && npm install && npm run dev   # http://localhost:5173
```

**上传一篇文献（端到端）**——界面右上角「上传 PDF」即可。命令行等价：

```bash
curl -X POST http://127.0.0.1:8000/api/papers -F "file=@paper.pdf;type=application/pdf"
# 另开一个终端消费 job（解析在后台 worker 里跑；从 server/ 目录启动）
cd server
../.venv/Scripts/python -m litbench.workers.run          # 常驻，2 秒轮询
../.venv/Scripts/python -m litbench.workers.run --once   # 只处理一个就退出
```

worker 与 API 必须指向同一个 `DATABASE_URL` / `STORAGE_DIR`（见 `.env.example`）；
`storage_dir` 在进程启动时解析成绝对路径，所以两个进程从同一目录启动最省事。

**M4：接到 agent 上（MCP 只读出口）**——Claude / DSH / Cursor 等支持 MCP 的客户端可直接查证据：

```bash
# 一次性把薄层装进 .venv（MIT 包，不 import 主仓代码）
.venv/Scripts/pip install -e mcp
# stdio：客户端拉起（Claude/DSH 的 mcpServers JSON 里 command/args 指向下面这行）
.venv/Scripts/python -m litbench_mcp serve --api http://127.0.0.1:8000
# HTTP：先签只读 token，再起 8765；请求必须带 Authorization: Bearer <token>
.venv/Scripts/litbench-mcp token issue --scope agent-read --label dsh
.venv/Scripts/litbench-mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

工具面：只读 `library.search` / `evidence.get` / `figure.get` / `table.get` /
`cite.resolve`（可选 `review=true` 模型复核）/ `collection.tree` / `summary.get` / `dataset.golden`（`bbox_only=true` 导图框标注集）；
M5 写侧 `proposal.create|list|get` + `task.list|claim|heartbeat|complete|fail`。
写 token 用多 scope 签发：

```bash
.venv/Scripts/litbench-mcp token issue --scope agent-read,agent-propose,agent-task --label dsh-writer
```

详见 [mcp/README.md](mcp/README.md)；示例 agent 见 `mcp/examples/agent_demo.py`（检索 → 证据 → 模型回答）与 `mcp/examples/agent_task_loop.py`（领任务 → 取证 → 模型提案 → 交任务）。

**团队共享（预览）**：认证默认关闭（单机模式）；要用账号/工作组共享，启动服务端前设
`AUTH_ENABLED=1`，首次打开页面会引导创建第一个管理员，之后在顶栏用户菜单「成员管理」里建号、
在工作区切换器里建组/加人。团队库只放成员主动共享的文献（文献级共享，图/表/摘要随文献可见）；
每个人的笔记/收藏/阅读状态各用各的。Postgres/Docker 部署已真跑通过
（见 [docs/17](docs/17-下一步路线.md) §11.2）。

**学位论文 LaTeX 包**：写作台「学位论文」页签填题目/作者/学校/学位/导师/年份，勾选 claim 与文献，
一键生成 `ctexbook` 骨架 + `refs.bib` + `references_gbt.tex`（GB/T 文本版），并用「检查引用」
扫 `\cite{}` / `\ref{fig:*}` / `\ref{tab:*}` 与库内对象比对。只给可改骨架，不做学校模板、不代写正文，
缺学校/年份等字段只在 warnings 里提示。

**按需升级**：默认档（pdfium）解析完会判一次「这份结果明显不够吗」——图注里的图号没定位到、
或图注被截断到几十字——是的话自动再排一个 Docling 任务（单篇 40–60 秒），
换来完整 caption 和结构化表格。开关和阈值见 `ESCALATE_*`：
要跑 Docling，本地 worker 必须从 ASCII 路径启动（`C:\litbench` 目录联接），见 `docs/09-现状交接.md` §11。

**改解析器之前先量一下**：人工复核过的图会变成 golden set，回放就能算出 recall 是涨了还是掉了。

```bash
python scripts/export_golden.py -o .data/golden.json
python scripts/replay_golden.py .data/golden.json                    # 按条目原后端重放
python scripts/replay_golden.py .data/golden.json --backend pdfium   # 基线对比
```

**表格结构要不要换解析器，也先量**（M9.3b）：同一批 PDF 分别用 Docling tableformer 与
MinerU `standard` 全页解析，比较表格网格与文本的一致性。Docling 的 C++ 解析层打不开中文
路径，需从 `C:\litbench` 目录联接跑并指 `HF_HOME`（MinerU `standard` 另需约 2GB 模型）：

```powershell
cd C:\litbench
$env:HF_HOME='C:\litbench\.hf-cache'
.venv\Scripts\python.exe scripts\eval_table_structure.py --samples C:\litbench\.data\samples --engines docling
.venv\Scripts\python.exe scripts\eval_table_structure.py --samples C:\litbench\.data\samples --engines mineru --tier standard
.venv\Scripts\python.exe scripts\eval_table_structure.py --samples C:\litbench\.data\samples --report-only
```

**表格导出后的编译验收**（M9.3c；xelatex 由 agent 的 shell 执行，别包进 Python subprocess）：

```powershell
.venv\Scripts\python.exe scripts\verify_table_latex.py prepare --table-id <表 id> --width auto
powershell -File <prepare 打印的 out 目录>\compile.ps1
.venv\Scripts\python.exe scripts\verify_table_latex.py report --out <out 目录>
```

`report` 会报 PDF/页数/Overfull/LaTeX 错误，默认 Overfull>0 即非 0 退出，可当 agent 验收闸门；
`--width both` 可同时验收 auto 与旧 plain 列。

全栈（需要先装 Docker Desktop；2026-11-15 已在 WSL2 的 docker.io + compose v2 上真跑通过）：

```bash
cp .env.example .env        # 至少设 POSTGRES_PASSWORD；多人用再设 AUTH_ENABLED=1 / INTERNAL_API_TOKEN
docker compose up --build
# 打开 http://localhost:8080 ，首次打开页面初始化管理员
```

认证 + MCP 内部服务令牌（局域网/多人部署）：

```bash
# 生成一个随机串，API 与 MCP 用同一个值
python -c "import secrets;print(secrets.token_urlsafe(32))"
# .env: AUTH_ENABLED=1、INTERNAL_API_TOKEN=<串>
# MCP 进程：LITBENCH_MCP_API_TOKEN=<同一个串>，指向 http://<host>:8000
```

局域网 HTTPS（TLS 反代，api/web 默认只绑 127.0.0.1）：

```bash
bash scripts/gen_selfsigned_cert.sh 192.168.1.50 litbench.lan   # 自签测试证书
# .env: AUTH_COOKIE_SECURE=true、HTTPS_PORT=443
docker compose --profile lan up -d proxy
# 访问 https://192.168.1.50/ ；正式环境把证书换成内部 CA / Let's Encrypt
```

MCP 容器（客户端 Bearer token 与内部服务令牌是两套）：

```bash
docker compose --profile mcp run --rm mcp \
  token issue --scope agent-read,agent-propose,agent-task --label dsh
docker compose --profile mcp up -d mcp      # http://127.0.0.1:8765/mcp
```

备份 / 恢复（整库回滚：PG + storage + MCP token 卷）：

```bash
bash scripts/backup.sh
bash scripts/restore.sh .data/backups/deploy-<时间戳> --yes
```

2026-11-15 已在 WSL2 的 docker.io + compose v2 上把上面全部真跑过：HTTPS 入口、
备份/恢复回滚、MCP 容器 16 工具、Windows+WSL 并发 147 请求无 5xx；
截图 `docs/assets/deploy/`。

启动顺序由 compose 编排，不用手动建表：

- `.env` 里的配置经 `compose.yaml` 的 `x-app-env` 显式注入 api / worker 容器
  （`.env` 本身只用于 compose 插值；想加新变量要同时写进 `x-app-env`）；
- api 容器的 entrypoint 在启动 uvicorn 前自动执行 `alembic upgrade head`
  （`RUN_MIGRATIONS=1`），数据库结构永远跟镜像里的迁移一致；
- worker 的 `RUN_MIGRATIONS=0`，并且 `depends_on: api: condition: service_healthy`——
  等 api 跑完迁移、`/api/ready` 查库通过后才启动，避免两个容器并发迁移；
- 镜像的 HEALTHCHECK 用 `/api/ready`（会查一次库）而不是不碰库的 `/health`；
  `/api/ready` 在认证打开时也保持免登录，否则容器会永远 unhealthy；
- 局域网只经 `proxy`（profile `lan`）的 443 进；`api`/`web` 默认绑定 `127.0.0.1`
  （`API_BIND` / `WEB_BIND` 可改），不要直接把 8000 暴露到局域网；
- 备份/恢复脚本在 `scripts/backup.sh` / `scripts/restore.sh`，MCP 容器在 profile `mcp`；
- 构建上下文由仓库根目录的 `.dockerignore` 收缩（排除 `.git` / `.venv` / `.data` /
  `node_modules` 等，也避免宿主依赖覆盖容器里的 `npm ci` 产物）。

启用 OCR 增强档（AGPL 组件，独立容器）：

```bash
docker compose --profile ocr up
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/papers` | 上传 PDF（流式 + sha256 去重），返回 paper/version/job |
| GET | `/api/papers` | 文献列表（含图表计数）；`q=` 走 BM25（标题/摘要/作者/citekey/标签），中文 `q` 先由 chat 模型改写英文术语（M9.1b-lite） |
| GET | `/api/papers/{id}` | 文献详情（含图表计数） |
| GET | `/api/papers/{id}/figures` | 按文献列图（前端「按文献」轴） |
| GET | `/api/papers/{id}/tables` | 按文献列表格（通常来自按需升级后的 Docling） |
| GET | `/api/papers/{id}/tables/{table_id}/latex` | **表格 → LaTeX**：tabular / 浮动体 / 合并单元格 warnings；`width=auto`（宽表按页宽换行）/ `plain`（旧版 l 列） |
| GET | `/api/papers/{id}/pdf` | PDF 本体（PDF.js 画 bbox overlay 用，带路径守卫） |
| GET | `/api/figures?q=&paper=&label=` | 图表检索；出处检索确定性匹配，语义 `q` 走 BM25（`engine=bm25`）；中文 `q` 先由 chat 模型改写英文术语（M9.1b-lite，响应 `query_expanded`，不是向量） |
| GET | `/api/figures/{id}` | 单图详情 |
| GET | `/api/figures/{id}/image` | 图片本体（带路径穿越校验） |
| POST | `/api/figures/{id}/review` | 纠错：确认 / 判错 + 可选修正框（人工与 agent 共用） |
| GET | `/api/jobs`、`/api/jobs/{id}` | 任务列表 / 状态 |
| GET | `/api/dataset/golden` | 导出已复核的图 + 样本出处（解析器回归用）；`format=bboxes` 只导图框标注集 |
| POST | `/api/citations/review` | **引用模型复核（M9.4）**：claim + 证据块 → supported/insufficient + provider/model |
| GET/POST | `/api/proposals` | AI/agent 纠错提案：列表 / 外部 agent 投递（必须带证据 block） |
| GET/POST | `/api/tasks` | agent 任务租约：列表 / 创建；`claim/heartbeat/complete/fail/retry` |
| GET/POST | `/api/claims` | **写作卡片（M5）**：一句论断 + 证据（block/figure/table/summary），证据硬约束 |
| GET | `/api/claims/{id}/revisions` | **claim 编辑历史**：每次改动前的快照（新→旧） |
| POST | `/api/claims/{id}/revisions/{rid}/restore` | 恢复到历史版本（恢复前先压当前状态） |
| GET | `/api/matrix?paper_ids=` | 对比矩阵（1–8 篇，实时派生，不落库）；`dimensions=` 可选维度 |
| GET | `/api/matrix/dimensions` | 矩阵可选维度清单（默认 7 行 + 引用键/条目类型/标签） |
| POST | `/api/writing/export` | 导出 LaTeX（附 `refs.bib`）/ BibTeX / Markdown |
| GET | `/api/writing/export/capabilities` | **Word/PDF 导出能力（M8）**：pandoc / typst 是否可用 |
| POST | `/api/writing/export/document` | **Word/PDF 导出（M8）**：claim 草稿 → `.docx`（pandoc）/ `.pdf`（typst），二进制响应 |
| POST | `/api/papers/{id}/metadata/lookup` | **元数据补全（M8）**：按 DOI/arXiv/题名查 Crossref，返回字段 diff（只读） |
| POST | `/api/papers/{id}/metadata/apply` | 确认后写入著录字段（默认只填空白，覆盖要 `overwrite=true`；owner/管理员） |
| POST | `/api/writing/outline` | **写作大纲 + 引用/图表交叉检查（M8）**：claim 按章节归并排序，正文图号 vs 证据、`\cite` 解析检查（只读） |
| POST | `/api/writing/check-citations` | 校验 `\cite{key}` 能否在文献库解析 |
| GET | `/api/references/styles` | **References 格式预设**：13 种（GB/T 7714 / APA / IEEE / …）+ 样例 + CSL 引擎可用性 |
| POST | `/api/references/format` | 一键生成 references 条目（逐条 + 合并文本；缺字段给出 warnings）；`engine=csl` 走官方 CSL 样式 |
| POST | `/api/references/csl-json` | **CSL JSON 导出（M8）**：pandoc / Zotero 通用输入（不落库） |
| POST | `/api/thesis/export` | **学位论文 LaTeX 包（M8）**：`ctexbook` 骨架 + `refs.bib` + GB/T 参考文献 |
| POST | `/api/thesis/check` | 交叉检查 `\cite{}` / `\ref{fig:*}` / `\ref{tab:*}`（确定性，不调模型） |

## 许可证检查

```bash
pip-licenses --format=csv --with-license-file --output-file /tmp/licenses.csv
python scripts/check_licenses.py /tmp/licenses.csv
```

CI 在每次 push / PR 跑同一套检查，见 [.github/workflows/license.yml](.github/workflows/license.yml)。

## 文档

- [01 评审与定位](docs/01-评审与定位.md) — 六个功能判定、该砍什么
- [02 开源复用与许可证](docs/02-开源复用与许可证.md) — 复用清单、AGPL 隔离规则、CI 卡口
- [03 架构与路线](docs/03-架构与路线.md) — Web 拓扑、数据模型、图表检索、16 周路线
- [04 服务端骨架](docs/04-服务端骨架.md) — 已实现的接口、两个埋雷点、还没做什么
- [05 P0 验证报告](docs/05-P0验证报告.md) — 轻量档在 10 篇样本上的结果、修掉的 4 个 bug、失败模式
- [06 前端](docs/06-前端.md) — 双轴工作台、三个界面状态、已知问题
- [15 知识库与 Agent 协作长线计划](docs/15-知识库与Agent协作长线计划.md) — M1–M6 实施记录与决策
- [16 欢迎页设计规格](docs/16-欢迎页设计规格.md) — 场景/材质/后处理/时间轴
- [17 下一步路线](docs/17-下一步路线.md) — M6 之后做什么、不做什么
- [ADR-0001](docs/adr/0001-形态-许可证-复用.md) — 形态、许可证、复用、图表检索的决策记录

## 贡献

用 DCO（`git commit -s`），不用 CLA。提交即同意以对应目录的许可证发布。

