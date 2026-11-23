# LitBench SOTA 审查指引（先读我，再读代码）

> 这份文件是给外部审查模型的**最小上下文**：读完应能定位模块、理解设计取舍、
> 知道哪些是已知问题/已修问题，从而把 token 花在真正的代码审查上。
> 当前基线（2026-11-16）：
> - 功能代码 `6512b6c`（自修批 C）；文档 `9ad792f`
> - 后端 **490 passed** / MCP **123 passed** / 前端 `tsc + vite build` 通过
> - 22 张表、迁移 `0001–0016`（下一个 0017）、MCP 16 工具、CSL 3 样式、手写引用预设 13 种
> - **不要**把「测试数/迁移数」当审查目标：它们由 `scripts/check_doc_counts.py` 校验

---

## 1. 项目是什么（30 秒）

LitBench 是一个**自部署的文献证据工作台**：把 PDF 解析成 block / 图 / 表，人工或 AI
复核后，用「claim 卡片 → 对比矩阵 / LaTeX / Word / PDF / 学位论文包」产出可回溯的写作素材。
不是通用聊天机器人，也不是 Zotero 替代品。

四条产品不变量（审查时优先看它们是否被破坏）：

1. **图表是一级对象**：图有 cite_key、bbox、页码、caption、复核状态，能回到原文；
2. **每条结论可回溯**：claim 的证据必须指向本版本的 block/figure/table，不编造；
3. **Zotero 只读**：LitBench 永不写用户 Zotero 库；导入只做镜像；
4. **不自动写正文**：AI 只生成/复核素材，正式写作与判断由人做。

明确不做：知识图谱大屏、通用「和文献对话」、全自动写论文、多租户 SaaS、
把 MinerU/Docling 打进主镜像、Zotero 双向同步。

---

## 2. 运行形态（先理解这个，很多设计就顺了）

```
浏览器 SPA (web/) ──HTTP──> FastAPI (server/litbench)
                                 │
                    ┌────────────┼─────────────┐
                 SQLite/PG    .data/storage   worker（解析/OCR/任务）
                                 │
                    pandoc/typst（导出）  LLM API（摘要/复核/查询改写）
                                 
MCP 客户端 (Claude/DSH/Cursor) ──stdio 或 HTTP──> mcp/src/litbench_mcp ──HTTP+内部令牌──> API
```

- **API**：`server/litbench/main.py`，全部路由在 `server/litbench/routers/`；
- **worker**：`server/litbench/workers/`，消费 `job`（解析/升级）与 `agent_task`（复核）；
- **前端**：React 18 + Vite + TS，入口 `web/src/App.tsx`；P0 页面是 PDF 阅读器 + 图表工作台；
- **MCP 薄层**：独立 Python 包 `mcp/`（MIT），不复制业务逻辑，只封装 HTTP API；
- **两种权限形态**：`AUTH_ENABLED=0` 单机（全局库，工作组接口 503）；
  `AUTH_ENABLED=1` 团队（账号/工作组/私有库/共享；管理面收紧为管理员/system）；
- **解析档位**：pdfium（默认轻量）→ Docling（增强，按需升级）→ MinerU（扫描页 OCR，
  独立 venv 子进程，AGPL 隔离）；双 OCR 交叉校核可选；
- **外部工具**：pandoc ≥3.0（CSL/Word/PDF）、typst（PDF 引擎）、Noto CJK 字体；
  查找逻辑统一在 `server/litbench/toolchain.py`，版本门槛在 `csl.py`。

---

## 3. 目录地图（只列审查相关）

### 3.1 后端核心（`server/litbench/`，按审查优先级）

| 文件 | 职责 | 审查价值 |
| --- | --- | --- |
| `main.py` | FastAPI 入口、CORS、认证中间件、路由挂载 | 高：401 豁免、内部令牌、CORS |
| `config.py` | 全部 env 配置、内部令牌最小长度 | 中 |
| `models.py` | 22 张表的 ORM（paper/version/block/figure/table/claim/summary/proposal/task/audit/user/workgroup…） | 高：数据模型与索引 |
| `schemas.py` | HTTP 入参/出参（与 ORM 分离） | 中：字段越权/校验 |
| `auth.py` | scrypt 密码、服务端 session、system 主体、`admin_when_enabled` | 高：会话/权限 |
| `team.py` | 私有库/共享/工作组/claim/collection 的**唯一判定层**（can_* / ensure_*） | **最高**：越权漏洞集中地 |
| `routers/` | 15 个路由文件，见 3.2 | 高 |
| `writing.py` | claim CRUD/矩阵/LaTeX/BibTeX/引用校验/导出加载 | 高 |
| `csl.py` + `doc_export.py` + `thesis.py` + `outline.py` | CSL 引用、Word/PDF、学位论文骨架、大纲交叉检查 | 高：M8 导出链 |
| `references.py` | 13 种手写引用格式 | 中 |
| `metadata.py` | Crossref/arXiv 查询、diff、显式 apply（不写 Zotero） | 中：外部请求/超时/大小 |
| `toolchain.py` | pandoc/typst 查找与版本 | 低-中 |
| `adapters/` | pdfium / Docling / MinerU 解析适配 | 高：坐标/阅读顺序/caption |
| `labeling.py`, `labels.py`, `ocr_cross.py`, `figure_filter.py` | 图号规则、双 OCR 校核、二维码过滤 | 中-高：误判/正则 |
| `figure_review.py`, `bbox_review.py`, `auto_accept.py` | AI 图纠错、框复核、自动接受/回滚 | 高：写入边界/阈值 |
| `audit.py`, `tasks.py`, `escalate.py`, `reparse.py`, `golden.py` | 审计、任务租约、解析升级、重解析锚点、golden 回归 | 中-高 |
| `retrieval.py`, `search.py`, `query_expand.py` | BM25、图表检索、中文查询改写（**不是向量召回**） | 中 |
| `summaries.py`, `paper_summary.py`, `sections.py` | 章节摘要、整篇速览、章节切分 | 中 |
| `llm.py`, `llm_config.py`, `prompts.py` | 三协议 LLM 客户端、BYO key、prompt 版本 | 中：SSRF/key 处理 |
| `zotero.py`, `paths.py`, `db.py` | Zotero 只读导入、路径守卫、DB 会话 | 中：路径穿越/只读保证 |

### 3.2 路由（`server/litbench/routers/`）

- `papers.py`：上传/列表/详情/正文/图/表/PDF；**列表与详情可见性**
- `figures.py`：图搜索/详情/图片/复核/改号/归组/删除；**404-vs-403**
- `summaries.py`：章节摘要生成/编辑/发布/历史
- `writing.py`：claim、矩阵、LaTeX/BibTeX/Markdown、Word/PDF、大纲、引用检查
- `references.py`：13 模板 + CSL（`engine=template|csl`）+ CSL JSON
- `thesis.py`：学位论文 LaTeX 包导出/交叉检查
- `metadata.py`：元数据 lookup（只读）/apply（owner/admin）
- `agent.py`：AI 复核提案、自动接受、批量采纳、任务租约、审计
- `team.py`：工作组/成员/文献共享
- `auth.py`：setup/login/logout/me、管理员建号/停用/重置
- `knowledge.py`：目录树、Zotero 导入、导入历史
- `jobs.py`, `dataset.py`, `citations.py`, `settings.py`：任务状态、golden 导出、引用复核、LLM 设置

### 3.3 测试（把测试当「行为规格」读，比读实现省 token）

`server/tests/` 共 55 个文件 / 490 条，全部 SQLite + 临时目录，无外部服务。高价值文件：

- 权限/团队：`test_auth.py`、`test_auth_permissions.py`、`test_team_workgroups.py`、
  `test_internal_token.py`、`test_writing_privacy.py`
- M8 导出：`test_csl_references.py`、`test_doc_export.py`、`test_metadata.py`、
  `test_outline.py`、`test_thesis.py`
- 解析/标注：`test_pipeline.py`、`test_docling_tables.py`、`test_labels.py`、
  `test_ocr_cross.py`、`test_table_latex.py`、`test_table_struct.py`、
  `test_citation_review.py`、`test_golden.py`
- 写作/治理：`test_writing_api.py`、`test_m3_governance.py`、`test_m5_agent_write.py`

`mcp/tests/` 123 条用 `httpx.MockTransport` 假 API，不依赖真实服务。

### 3.4 其它

- `mcp/src/litbench_mcp/`：`server.py`（工具注册）、`tools.py`（16 工具实现）、
  `api.py`（HTTP 客户端）、`auth.py`（令牌/scope）、`cli.py`
- `web/src/`：`App.tsx`（工作台/状态）、`PaperReader.tsx`、`Writing.tsx`、
  `Proposals.tsx`、`Team.tsx`、`api.ts`、`types.ts`、`styles.css`
- `scripts/`：下载/导出/验收/评测脚本；`scripts/check_doc_counts.py` 是文档计数守卫
- `compose.yaml`、`server/Dockerfile`、`deploy/`：部署链（未实跑验证）
- `docs/`：**审查时不要通读**。只有需要追溯决策时看 §3.5 的几节

### 3.5 文档阅读策略（省 token 关键）

- 现状：`docs/09-现状交接.md` **只读 §0（约 120 行）**；§0.0 是阅读清单；
- 路线：`docs/17-下一步路线.md` **只读 §4**（当前阶段/未验证项）；
- 交付细节：`docs/15-知识库与Agent协作长线计划.md` **只读 §12.42–§12.48**
  （M8 与自修批 A/B/C）；其它 §12 历史记录不必读；
- `docs/01/02/03/05/08/11/12/14/16` 是历史设计与验收快照，审查不需要；
- `docs/assets/`、`docs/*.png` 不要读。

---

## 4. 设计思路（关键取舍与原因）

1. **证据链优先于生成**：解析产物落 block（页码/bbox/类型/顺序）→ 图/表挂版本 →
   claim 的证据必须指向本版本对象。服务端在生成摘要/提案时做确定性校验（证据非空、
   属于本版本、图号在候选集），不通过就 422 不落库。审查时重点看「校验是否可被绕过」。
2. **版本感知**：图/块/摘要挂 `paper_version` 而非 `paper`；重解析会清空并重建，
   人工/agent 产物按「页 + bbox + 文本」重锚（`reparse.py`），不能跨版本乱认。
3. **确定性检查 + 概率生成分离**：图号规则、引用解析、大纲交叉检查、元数据 diff 都是
   确定性代码；LLM 只用于摘要/图纠错/框复核/查询改写。自动接受有白名单 + 阈值 +
   证据 + 「人工改过永不动」。
4. **权限只有一层**：所有可见性/归属判定集中在 `team.py`；路由只负责把 False 翻译成
   404（看不见，避免存在性泄露）或 403（看得见但只读）。私有库 = owner=我 + legacy(NULL)；
   团队库 = 组内共享；system 内部令牌 = 全库管理面。
5. **引用/导出先做窄、做对**：13 种手写模板保持默认；官方 CSL 走 `pandoc --citeproc`
   （不用 citeproc-py，实测会吞 GB/T 的 `[J]`/`[D]` 等）；Word/PDF 只导出 claim 草稿，
   PDF 用 typst（xelatex 从 Python 子进程拉起在本机不可用）；pandoc 一律 `--sandbox`。
6. **MCP 不复制业务**：MCP 只做协议适配 + 工具 schema + 审计/限流，权限与业务全在 API；
   auth 打开后 MCP 用内部令牌以 system 主体调 API。
7. **单机与团队同源**：`AUTH_ENABLED` 切换而不是两套代码；单机时团队接口明确 503，
   不悄悄放宽。桌面化/分发是后续事项（当前不做）。
8. **失败要可读**：外部服务失败 502、参数/内容 422、不可见 404、覆盖冲突 409、
   功能关闭/缺工具 503；不吞错、不静默截断（如 500 张导出上限明确 422）。

---

## 5. 当前状态：哪些已修、哪些已知未修

### 已做过的独立审查/自修（不要重复报告，除非发现回归）

- **自修批 A**：pandoc `--sandbox`（原可被 claim 内 `![]()` 读取服务器本地文件）、
  `\cite{}` 在 docx 中丢失、额外文献不进参考文献表、引用索引 300 篇截断、图表正则误报、
  导出可见性、metadata 响应上限、Docker/CI 加固。`docs/15 §12.46`。
- **自修批 B**：auth 打开后的权限矩阵（提案/批量采纳/自动接受/任务/审计/golden/模型配置/
  服务端导入的越权与泄露）、共享只读写成链、404-vs-403、会话踢出、最后管理员保护、
  重复上传重解析、文档计数校验。`docs/15 §12.47`。
- **自修批 C**：工具链收敛 + pandoc ≥3 版本门槛、summary 审计 actor、内部令牌最小长度、
  MCP system 权限回归。`docs/15 §12.48`。

### 已知未验证（环境限制，不是代码错误）

- Docker 镜像重建未完成（本机网络 GitHub/镜像 SSL EOF/超时）；`docker compose config` 通过；
- GitHub Actions 未实跑（含 docs 计数 job）；
- 第二台物理机 LAN 登录/上传/共享、正式证书 80→443 未验收。

### 已知技术债/边界（可评风险，不必要求本次修）

- 解析：缩略图/图注仍有误报（几何启发式 + 人工复核兜底）；OCR 双路径是补充不是保证；
- PDF 导出用 typst，排版与 xelatex/学校模板不完全一致；导出只覆盖 claim 草稿；
- 检索：BM25 + 可选中文 chat 查询改写，**没有 embedding 向量召回**（产品决策：一个模型 API，
  chat 不提供 embedding）；
- 多用户：登录无速率限制、无 CSRF token（同站前端不受影响）、MCP 内部令牌未与用户绑定、
  admin 管理面在团队模式对成员 403（产品取舍）；
- 前端没有单元/E2E 测试；`web/src/App.tsx`、`server/litbench/routers/papers.py`、
  `mcp/src/litbench_mcp/tools.py` 偏大；
- 开发 SQLite 库没有 alembic_version（迁移 DDL 手动应用）；生产 Postgres 走 alembic；
- 查询改写在进程内缓存；检索路径里有同步 LLM 调用（只在配置了模型且查询含中文时触发）；
- `import/runs` 与 golden 未按用户维度建模（v1 用可见性/管理员近似）；
- 文档里历史小节数字是当时快照；只有「当前基线」锚点被校验。

---

## 6. 建议的审查方式（省 token + 高收益）

### 优先审查（按收益排序）

1. **权限/越权**：`team.py` + 每个 router 的 `ensure_*`/`can_*` 调用；重点看
   「列表过滤了、详情忘了」「按 id 的写入口」「导出/引用/元数据等派生接口」。
   测试：`test_auth_permissions.py`、`test_team_workgroups.py`。
2. **数据完整性/迁移**：`models.py` + `server/migrations/versions/` 是否与 ORM 一致
   （`test_migration_matches_models.py`）；重解析/回滚/审计链（`reparse.py`、
   `auto_accept.py`、`figure_review.py`）。
3. **M8 导出/引用**：`csl.py`、`doc_export.py`、`thesis.py`、`outline.py`、
   `references.py`；重点看用户输入（claim/元数据）能否注入 pandoc 参数/本地文件，
   引用改写是否吞引用，错误码是否正确。
4. **解析正确性**：`adapters/pdfium_parser.py`（坐标/阅读顺序/caption 合并）、
   `labeling.py`（图号正则/误判）、`ocr_cross.py`（跨版本/跨页锚定）。
5. **任务/并发**：`tasks.py` 租约/幂等/过期回收；`workers/` 消费；上传去重/重解析竞态。
6. **MCP 边界**：`mcp/src/litbench_mcp/` 是否只做薄封装；工具描述/scope/错误映射；
   内部令牌是否被滥用。
7. **前端状态**：`App.tsx` 的竞态/静默刷新、`PaperReader`/`Writing` 的失败态；
   （已知无前端测试，这部分建议静态审查 + 手工路径推演）。

### 不建议花时间

- 逐行读 `docs/15`、`docs/09` 全文或历史章节；读 `docs/assets/`；
- 读 `.data/`、`.venv/`、`node_modules/`、`web/dist/`、`pytest-of-*`、`torchinductor_*`；
- 重新报告自修批 A/B/C 已修问题（除非找到回归）；
- 要求新增产品功能/大规模重构——本轮目标是**找真 bug 与设计风险**。

### 命令（Windows 开发机；CI 用 Linux 等价命令）

```powershell
# 后端（约 1.5 分钟，490 条）
cd <repo>\server
..\.venv\Scripts\python.exe -m pytest -q

# MCP（约 3 秒，123 条）
cd <repo>\mcp
..\.venv\Scripts\python.exe -m pytest -q

# 前端类型 + 构建（TEMP 指到项目内，避免 Windows 权限问题）
cd <repo>\web
$env:TEMP='<repo>\.data\tmp\esbuild'; $env:TMP=$env:TEMP
npm run build

# 文档当前基线计数（490/123/0017/22 表/3 样式）
cd <repo>
.\.venv\Scripts\python.exe scripts\check_doc_counts.py

# 部署静态校验（不启动容器）
docker compose config
```

环境注意：项目在 Windows 上开发，部分中文输出在终端可能乱码（文件本身 UTF-8）；
`.data/` 是本地数据/密钥目录，**不要打印 `llm-config.json` 内容**；
真实 Zotero 路径只读，任何测试都不要写。CI 用 GitHub Actions（`.github/workflows/ci.yml`）。

### 输出建议

请按以下格式给结论，便于直接转成修复任务：
`严重度 | 文件:行号 | 问题 | 触发条件/复现 | 建议修复 | 是否有测试覆盖`。
对「设计取舍」类问题单独分组，不与 bug 混排；不确定的标注「推测」。

---

## 7. 一行总结

这是一个「证据可回溯 + 确定性校验 + 权限单层判定」的文献工作台；审查重点不是功能多少，
而是**权限边界、数据完整性、导出安全和解析正确性**。先读本文件 + 测试，再按需读模块，
比通读历史文档省一个数量级的 token。
