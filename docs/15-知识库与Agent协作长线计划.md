# 知识库与 Agent 协作长线计划

> 状态：2026-11-12 立项；2026-11-15 **M1–M8 + Docker/Postgres 部署与收尾 + 全部 P0
> （表格进界面、写作台小修）已交付；M9.1a（BM25）、M9.1b-lite（chat 查询改写）、
> M9.3a/b/c（表格）、M9.4（MCP 质量）已完成**（真正的 embedding 向量召回未做；
> M9.2 精度量化已放弃（2026-11-15 产品决策））。
> 2026-11-16 **M8 第一块：CSL 引用引擎（pandoc --citeproc + 官方 GB/T/APA/IEEE 样式）
> 与 CSL JSON 导出已交付**（§12.42）；**第二块 Word/PDF 导出（pandoc → docx，typst → pdf）
> 已交付**（§12.43）；**第三块 Crossref/arXiv 元数据补全已交付**（§12.44）；**第四块
> 写作大纲 + 引用/图表交叉检查已交付**（§12.45）——M8 写作完整度四块完成；
> **外部审查前自修批 A 已交付**（§12.46：sandbox/引用/可见性/CI 等）；
> **自修批 B 已交付**（§12.47：auth 打开时的权限矩阵、会话踢出、全局面收紧、文档计数自动校验）；
> **自修批 C 已交付**（§12.48：工具链收敛 + pandoc 版本门槛、summary actor、内部令牌强度、MCP 回归）；
> **SOTA 审查高/中危修复批已交付**（§12.49：Zotero owner 隔离/上传限流、重解析 claim 证据重锚
> + 迁移 0017、矩阵/旧卡可见性、提案回滚快照）；
> 下一阶段是部署/CI 实跑与外部审查（`docs/17` §4）。
> 关联文档：`docs/09-现状交接.md`（默认先读 §0.0）、`docs/14-图号校核与文本层置信度.md`、
> `01-评审与定位.md`（产品定位：证据中间层）。
> 本文是**长线计划 + 决策记录**；每个阶段的实施细节写进 `docs/16-*` 或追加到本文的进度节。

---

## 0. 一句话

不做第二个 Zotero（阅读器/文献管理），做 agent 时代的 **证据层 + 协作层**：
文献树是导航地图，章节摘要/卡片是带证据的低风险衍生物，提案 + 任务队列是 agent 进入系统的
受控通道，MCP 是对外插口。Zotero 继续管「读」，我们管「读过之后到写出正确引用之间那段搬运」。

---

## 1. 已定决策（2026-11-12，用户拍板）

| # | 决策 | 含义 | 影响 |
| --- | --- | --- | --- |
| D1 | **Zotero 只读导入** | 单向镜像：Zotero → LitBench；LitBench 的标注/摘要不回写 Zotero | 不做冲突合并/删除传播；导入实现 1–2 天；不碰用户 Zotero 库 |
| D2 | **摘要章节级** | 按章节（section）生成摘要；不是整篇 TL;DR，也不是图表级 | 需要 section 树与 block 证据绑定；M2 的数据模型按章节建 |
| D3 | **agent 提案默认自动接受，但用户可改文本** | proposal 落库后默认 accepted；用户编辑摘要/标注后，该对象的 `source=human_edited`，自动接受不再覆盖 | 需要 `review_proposal` 状态机 + `summary.source/revision`；自动接受要有白名单/置信阈值/可回滚 |
| D4 | **LLM 走 API，已确认 A 方案：DSH 背后的 OpenAI 兼容网关，LitBench 服务端直连** | 不内置本地模型、不复用 DSH 会话额度；provider 抽象，第一个真实 provider 是 DSH 网关（OpenAI 兼容）。**先不给 key，M2 先用 mock provider 开发**；key 到位后再接真实网关 | `litbench/llm.py` + prompt registry；key 只在服务端 env/.env；token/费用审计 |
| D5 | **M2 之前先插 M1.5：装 Zotero 并实测只读导入** | M1 只对 fixture 验证了 json/sqlite；真实 `zotero.sqlite` 与 Zotero 7 local API 未实测。M1.5 装 Zotero、建真实集合、跑通 auto 导入 | M2 开工前完成；不阻塞代码骨架（可并行） |
| D6 | **mock 先行、真实 key 后接** | M2 拆为 M2a（无 key：mock provider + 全链路骨架 + UI + 测试）与 M2b（key 到位后接 DSH 网关真实模型） | 不因等 key 停摆；M2b 只换 provider，不改表结构/UI/校验器 |

---

## 2. 分层架构

```
L4 产品层   知识库/文献树 UI · 章节摘要 · 对比矩阵 · 写作侧（LaTeX/引用）
L3 协议层   MCP server（read / propose / task 三类工具，stdio + HTTP）
L2 协作层   agent_task（租约/心跳/重试）· review_proposal（状态机/自动接受）· audit_log
L1 访问层   REST API（现有 + collections/import/summary/task）· token scope（human/agent）
L0 数据层   paper/version/block/figure/table（已有）+ collection/summary/claim/task/proposal
```

原则：

- **agent 不直接改事实表**，只领任务、交提案；接受后由服务端写正式字段，并留 before/after。
- **所有 AI 生成物必须带证据**（`evidence_block_ids` 非空才允许落库），这是 ADR 里
  「图/表是一等公民、结论可回溯」的延伸。
- `schemas/`、`mcp/` 保持 MIT 薄层，不 import AGPL 服务端代码；模型 SDK 依赖许可需过卡口。

---

## 3. 数据模型（按阶段加）

### M1（本阶段）

- `collection`：`id` `name` `parent_id`(self FK) `sort_order` `source`(manual|zotero)
  `zotero_key`(unique) `created_at`。Zotero 集合按 key 幂等映射。
- `paper_collection`：`paper_id` + `collection_id`（联合主键）。一篇可属多个集合。
- `paper` 扩展：`abstract` `tags_json` `reading_status`(unread|reading|done|shelved)
  `zotero_item_key`(unique) `metadata_source`(upload|zotero|arxiv) `imported_at`。
- `import_run`：`id` `source`(json|sqlite|api) `started_at` `finished_at` `stats_json` `error`。

### M2（摘要）

- `summary`：`id` `paper_version_id` `section_key`(章节路径哈希) `section_title` `level`
  `content`(章节摘要) `key_points_json` `evidence_block_ids_json` `source`(agent|human_edited)
  `provider` `model` `prompt_version` `status`(draft|published) `tokens_in/out` `cost` `created_at`
  `edited_by` `edited_at`；实现另加 `auto_accepted`(是否服务端自动接受)、`human_locked`(人工锁定)、
  `updated_at`，唯一约束 `(paper_version_id, section_key)` 保证幂等。
- `summary_revision`：每次生成/人工编辑留旧版本（可回滚，自动接受前快照），
  `reason` 记 generated|edited|regenerated|restored。

### M3（agent 协作）

- `agent_task`：`id` `kind`(ocr_verify|bbox_verify|caption_cleanup|table_structure|summary)
  `target_type` `target_id` `payload_json` `status`(queued|leased|running|done|failed|expired)
  `lease_until` `attempts` `idempotency_key` `result_json` `error` `actor` `cost` 时间戳。
- `review_proposal`：`id` `kind`（与 task 对应）`target_type` `target_id` `proposed_json`
  `evidence_block_ids_json` `confidence` `actor` `status`(pending|accepted|rejected|superseded)
  `decided_by` `decided_at` `decision_reason`。
- `audit_log`：`id` `actor` `action` `target_type` `target_id` `before_json` `after_json`
  `evidence_json` `created_at`。

---

## 4. 阶段路线与验收

### M1 文献树 + Zotero 只读导入 + 知识库 UI（约 1 周）

**交付**

1. 迁移 0005：`collection` / `paper_collection` / `paper` 扩展 / `import_run`；
2. `litbench/zotero.py`：三种来源的统一导入器
   - `api`：Zotero 7 local API（`http://127.0.0.1:23119/api/users/0`，Zotero 运行时可用）；
   - `sqlite`：用户指定 `zotero.sqlite`，**只读 URI**（`mode=ro`），不复制、不写；
   - `json`：Zotero「导出库」/ local API 形状的 JSON 文件（测试与离线场景）；
3. API：`GET/POST/PATCH/DELETE /api/collections`、`POST /api/import/zotero`、
   `GET /api/import/runs`、`GET /api/papers?collection_id=`、`POST /api/collections/{id}/papers`；
4. 前端：侧栏「知识库」树（展开/折叠、每个集合显示文献数）、点集合过滤文献、
   「从 Zotero 导入」按钮 + 结果提示；
5. 测试：导入器幂等/集合映射/只读源不写库；collections CRUD；paper 过滤。

**验收**

- 用 JSON fixture 导入：集合树与条目正确、重复导入不产生重复 paper；
- 现有 14 篇文献不受影响；`pytest` 全绿；UI 可用（截图入库）；
- 导入失败有明确错误（Zotero 没开 / 路径不对 / schema 不识别），不静默。

**不在 M1**：PDF 从 Zotero 链接导入并解析（后续）、双向同步、摘要。

### M1.5 装 Zotero + 真实只读导入实测（约 0.5 天，M2 之前）

> **状态（2026-11-12 15:5x）：已完成。** 真机 Zotero 10.0.8，sqlite / local API 两条路都跑通，
> 并修掉真机暴露的 3 个问题。记录见 §11.7。

**为什么**：M1 的导入器只用 fixture 验证过 `json` / `sqlite`；本机没装 Zotero，
真实 `zotero.sqlite` 与 Zotero 7 local API 都还没实测。M2 之前先把这条补上。

**交付**

1. 本机装 Zotero 7，建 2–3 个真实集合、放入若干真实文献（含中文条目）；
2. `source=sqlite`（默认 `~/Zotero/zotero.sqlite`，只读 URI）跑一次导入：
   核对集合树/父子关系、标题/作者/年份/DOI/摘要/标签；
3. 打开 Zotero 后用 `source=api`（local API）再跑一次：核对分页、父集合、条目数；
4. 界面「导入 Zotero」按钮走一遍；验收错误提示（Zotero 未开 / 路径不对）是可读的；
5. 记录真实数字到 `docs/15` §11.7 与 `docs/09`。

**验收**：真实库导入后集合树与 Zotero 一致；重复导入幂等；Zotero 侧改动重新导入后能反映；
不写用户 Zotero 库（导入前后文件哈希一致 / API 只读）。

**不在 M1.5**：Zotero 附件 PDF **自动**导入并解析（仍留给后续）。
（2026-11-12 补：手工补传 PDF 已支持；同日 B 方案已完成——Zotero 附件 PDF 自动导入并解析，
见 §12.9。）

### M2 章节级摘要（约 1.5–2 周，分两段）

> **两段式：M2a mock 先行（无 key）→ M2b 接真实网关（key 到位后）。**
> 表结构 / UI / 证据校验器两段共用，M2b 只换 provider。

**M2a 无 key 骨架（约 1 周）**

> **状态（2026-11-12 16:xx）：已完成**（后端提交 `0ee785a`、前端 `dab41b2`）。
> 真实语料跑通 mock 全链路；前端「章节摘要」页签已合并（截图 `docs/assets/summaries/`）。
> 记录见 §12。M2b（真实网关）已于 2026-11-12 深夜完成，见 §12.13。

- 章节树：从 `block.type=section/title` 构建 section 区间（复用解析顺序），图表 caption 归属章节；
- 迁移 0006：`summary` / `summary_revision`（字段见 §3）；
- `litbench/llm.py` + **`mock` provider**（返回固定/可配置假响应）+ prompt registry + `prompt_version`；
- 证据校验器：`evidence_block_ids` 必须存在且非空，否则拒绝落库；
- 自动接受（D3）：默认 accepted；`source=human_edited` 不再被覆盖；人工锁定优先；
- UI：文献详情「章节摘要」页签，可生成（mock）、编辑、发布、点回原文；
- 端到端：mock 生成 → 校验 → 落库 → UI 展示/编辑，`pytest` 全绿。

**M2b 接真实模型（约 0.5–1 周，key 到位后）**

> **状态（2026-11-12 深夜）：已完成**——基础设施见 §12.8；收尾见 §12.13（真实模型跑完
> 02-传热学第 7 章 69 节：60 条摘要、**0 失败**、53,297/65,839 tokens、**$0.0893**；
> 置信度落库、自动接受阈值校准为保留 0.9；摘要页签生成/发布/编辑锁定/历史回滚已点击验收）。
> M2b 不再等 key；下一步转 M4。

- 部署者用环境变量提供 DSH 网关的 `LLM_BASE_URL / LLM_MODEL / LLM_API_KEY`
  （服务端持有，不进仓库/DB/前端）；
- 适配真实 OpenAI 兼容细节：JSON mode、`max_tokens`、错误码、限流、超时；
- 用 1 本真实教材跑通，调 prompt，记录真实 tokens/费用，校准自动接受阈值；
- 验收：章节摘要人工抽检 + 证据绑定率 100% + 失败可见（不阻塞其它章节）。

### M3 agent 任务与提案：三项 AI 纠错（约 1.5–2 周；2026-11-12 排期细化，当晚轻量版已实现）

> **状态：轻量版已实现并真机验收（2026-11-12 晚，见 §12.10）。** 已完成「提案 + 人工采纳」
> 主链路：图号复核（候选集合 + 归一化校验）、图注清洗（长度约束）、裁框检查（多模态判断，
> 采纳只标记定位错误、不自动改框），以及**整篇速览**（AI 一键总结文献，§12.10）。
> **仍按原计划留到后续**：`agent_task` 租约/心跳/重试表、`audit_log`、自动接受器
> （当前全部人工采纳；只有「采纳高置信」批量按钮）、表格结构复核、外部 agent 接入（M4）。
> 三项共用一套「任务 + 提案 + 审计」地基的完整版：**2026-11-13 下午已完成正式化**
> （audit_log / agent_task 租约 / 自动接受器 / 回滚 / 批量 / 任务 API，见 §12.18）。

#### M3a 低置信图号复核（先做，约 3 天）

- **目标对象**：当前「待确认」队列（低置信 / 两套 OCR 冲突 / 无编号，约 38 张）+ 解析后抽检。
- **输入**（纯文本 prompt）：该图 caption、当前 `label_raw/label_norm/sublabel`、同页邻近图注、
  所在系列父图号、前后图号序列（给候选图号列表，不让模型自由编）、章节标题。
- **输出 JSON**：`{"suggested_label": "图 4-50", "suggested_sublabel": "a", "confidence": 0..1,
  "reason": "...", "evidence_block_ids": [...]}`。
- **校验**：`suggested_label` 必须能归一成 `fig:x`（或明确「无编号」）；必须在候选集合内；
  evidence 非空且属于本版本。不通过 → 不落库，进失败清单。
- **自动接受**：proposal(kind=figure.label)；置信 ≥ **0.95** + 目标非人工锁定 → 自动写
  `label_*`、`label_source=agent`、`label_confidence`、`reviewed_by=agent`；否则留待审。
- **验收**：38 张队列跑完；人工抽检 30 条准确率 ≥ 95%，自动接受占比如实记录；
  误改可一键回滚（audit before/after）；绝不覆盖 `label_source=review`（人工改过的）。
- **风险**：模型编图号 → 强制候选集合 + evidence + 归一化校验，三道闸。

#### M3b 图注清洗（约 3 天，复用 M3a 地基）

- **目标对象**：caption 有明显 OCR 噪声/断行/错字的图（先用现有文本质量与双 OCR 信号打分排序）。
- **输入**：caption 原文 + 邻近文本块上下文 + 双 OCR 结果（如有）；**输出**：清洗后 caption
  + evidence + confidence。
- **约束**：只做「去噪 / 补断行 / 修明显 OCR 错字」，**不改写、不扩写、不翻译**；数字/单位/
  上下标/希腊字母必须保留；字符长度变化设上限，超限只出 proposal 不自动写；保留旧值可回滚。
- **自动接受阈值 0.95**；用户改过的 caption（human_edited）不动。
- **验收**：抽 30 条人工判断「可读且信息不丢」≥ 90%；不通过的自动接受为 0（必须留待审）。

#### M3c 框复核（最后做，约 3–4 天，需要多模态）

- **目标对象**：定位框错误/漏框（当前靠人工拖框修）。
- **能力升级**：`llm.py` 现在只发文本；M3c 要把「整页低分辨率图 + 目标图裁图 + bbox 画在原页上的
  可视化」作为 image 内容发给多模态模型（OpenCode Go 的 `qwen3.8-*` / `claude-haiku-5-5` /
  `gpt-6-luna` 都支持图片；chat/messages/responses 三种协议都要加 image part）。
- **输出 JSON**：`{"verdict": "ok|wrong", "bbox": [x0,y0,x1,y1], "confidence", "reason",
  "evidence_block_ids": [...]}`；服务端校验页内、与现有框 IoU、与 caption 锚点一致性。
- **自动接受（最保守）**：默认只产生 proposal 不自动写；仅当「与现有框 IoU 很低或很高」+
  「与双 OCR / caption 锚点一致」+「置信 ≥ 0.95」三条同时满足才自动接受；否则进人工拖框队列。
- **验收**：用 50 张已人工复核图做测试集，自动建议与人判一致率 ≥ 90%，误改率 < 2%，其余待审。
- **状态（2026-11-13 下午）：提案链与「默认只提议」已实现并真机跑通**——三图输入（整页 +
  红框标注页 + 目标裁图）、归一化坐标 → pt 四道校验（页内/最小尺寸/IoU/caption 锚点）、
  默认 `BBOX_REVIEW_AUTO_ACCEPT=0`；UI 待审卡片显示「定位框旧值→新值 + IoU + caption 锚点」。
  真机：6 张 arXiv 图全判 ok（0.95–0.98）；把一张图的框人为改坏后实测 wrong → pending →
  采纳写 bbox_override + 重裁缩略图 → 回滚恢复。**50 张人工复核集尚未建立**（当前语料
  无人工框标注），因此「自动建议与人判一致率」指标待补。详见 §12.19。

#### 共用地基（三件一起做）

- 迁移 0007：`agent_task`（kind/target/lease_until/attempts/idempotency_key/status/result/error/actor/cost）、
  `review_proposal`（kind/target_type/target_id/proposed_json/evidence/confidence/status/decided_by/decision_reason）、
  `audit_log`（actor/action/target/before/after/evidence）。
- 服务端：任务租约 + 心跳 + 重试 + 超时回收（把 worker 的 stale 阈值坑一次改对）；
  proposal 自动接受器（白名单 + 分类阈值 + human_locked/human_edited 永不覆盖）；
  接受时写正式字段 + audit + 保留 proposal 供回滚。
- API：`POST/GET /api/tasks`、`POST /api/tasks/{id}/claim|complete|fail`、`GET /api/proposals`、
  `POST /api/proposals/{id}/decide`；外部 agent（DSH）接入放 M4。
- 人审 UI：待审提案队列（图号 / 图注 / 框三类），before/after 对比、接受/拒绝/批量、回滚。
- **执行方式（先简后繁）**：M3 先用服务端内置 worker 同步调 LLM（单任务、可重试，够本机用）；
  外部 agent 领任务走 M4 的 MCP。
- 明确不做：重新解析/重新切图、表格结构复核、跨文献引用核对。

### M4 MCP read-only（约 1 周；**2026-11-13 已交付**，实施记录见 §12.17）

- `mcp/` 薄层 server（stdio + Streamable HTTP），工具：
  `library.search` `evidence.get` `figure.get` `table.get` `cite.resolve`
  `collection.tree` `summary.get` `dataset.golden`；
- token scope、rate limit、审计；示例 agent（DeepSeek Harness）跑通「查证据 → 回答」闭环；
- 提示注入防线：文献内容是数据不是指令（工具描述 + 服务端不执行）。
- **已实现（2026-11-13）**：8 个 read 工具 + stdio/Streamable HTTP + Bearer/agent-read scope +
  每 actor 限流 + JSONL 审计 + `mcp/examples/agent_demo.py`；MCP 单测 102 passed，实录见 §12.17。

### M5 MCP propose/task + 写作侧（**2026-11-13 已交付**，实施记录见 §12.23/§12.24）

- MCP 暴露提案/任务工具；agent 纠错闭环全自动化（自动接受 + 人工抽检）；
- 对比矩阵、claim/卡片、LaTeX 导出、引用校验（机构库 P3）。

**已实现（2026-11-13）**：`proposal.create|list|get` + `task.list|claim|heartbeat|complete|fail`
共 8 个写侧工具（scope `agent-propose` / `agent-task`，HTTP 写 token 需同时含 `agent-read`）；
写作台 claim 卡片（证据硬约束、同版本）、派生对比矩阵（1–8 篇）、LaTeX/BibTeX/Markdown 导出、
确定性引用校验；**references 条目生成（13 种格式预设，见 §12.25）**；
**CSL 引擎（官方 GB/T/APA/IEEE 样式，pandoc --citeproc）见 §12.42**。
**尚未做**：pandoc 导出 docx/pdf、引用条目 Crossref 自动补全、机构库 P3。

### M7 团队共享与部署（2026-11-15 ✅ 已交付）

用户 2026-11-15 拍板：**工作组形式**（每位用户的私有文献库只归个人；团队文献库只放用户主动共享的内容）、
**本地账号 + 管理员建号**、**局域网 Docker/Postgres**、学位论文**只做 LaTeX**。

- ✅ **身份层**：账号/登录/session/管理员建号/成员管理，认证默认关闭（`AUTH_ENABLED=1` 开启）；
  记录见 §12.27。
- ✅ **工作组 / 私有库 / 共享**：迁移 0014（workgroup / workgroup_member / paper_share /
  user_paper_state + paper/collection/claim.owner_user_id）、`team.py` 判定层、7 个 API、
  全站可见性、前端工作区切换/共享菜单/工作组管理、A/B 验收；记录见 §12.28。
- ✅ **MCP 内部服务令牌**：`INTERNAL_API_TOKEN` ↔ `LITBENCH_MCP_API_TOKEN`，system 主体；
  记录见 §12.29。
- ✅ **Docker/Postgres 首次真跑 + 部署收尾**：迁移/worker/认证/共享/持久化；局域网 HTTPS、
  备份恢复、MCP 容器化、两客户端并发；记录见 §12.30/§12.31。
- ⏳ 只剩「有第二台物理机时」的局域网补验 + 正式证书/80→443 跳转；不阻塞开发。
- 部署细节与边界见 §12.30/§12.31；学位论文包见 §12.32。

### M8 学位论文 LaTeX 包（2026-11-15 ✅ 已交付）

用户拍板**只做 LaTeX**。目标不是排版器，而是把 claim/文献元数据装进学位论文骨架：

- ✅ `paper.entry_type` + 学位论文元数据（学校/学位/地点）→ 迁移 0015；
  Zotero `itemType/university/thesisType/place` 导入映射；手工上传可 PATCH 补全；
- ✅ References 的 GB/T 7714 `[D]` 条目 + BibTeX `@phdthesis/@mastersthesis` + RIS `THES`；
  APA/IEEE/MLA/Chicago/Nature/Science/ACS/Elsevier/Vancouver 都有 thesis 写法；
- ✅ 新 `/api/thesis/export`：ctexbook 章节骨架 + claim 挂载 + `.tex`/`refs.bib`/GB/T 文本；
- ✅ `/api/thesis/check`：`\cite{}`、`\ref{fig:*}`、`\ref{tab:*}` 与库内对象交叉检查；
- ✅ 前端写作台「学位论文」入口（元数据表单 / 范围勾选 / 章节顺序 / 预览下载 / 交叉检查）。

完整交付记录与边界见 `docs/17` §11.3.7、`docs/15` §12.32。**当前不要做的事**：不要重做
M1–M8 已交付的东西；不要把学位论文包做成 Word/学校定制模板。

---

## 5. 自动接受策略（D3 细则）

1. proposal 先落 `pending`，随后由服务端**自动接受器**处理；
2. 自动接受白名单与阈值（M3 实现；阈值宁严勿滥，先在测试集上校准）：

   | proposal kind | 自动接受条件（全部满足） |
   | --- | --- |
   | `summary.section` | 证据非空且属于本节 + 置信 ≥ 0.9（M2a 已按此实现） |
   | `figure.label` | 图号能归一 / 明确无编号 + 在候选集合内 + 证据非空 + 置信 ≥ **0.95** |
   | `figure.caption` | 只去噪不改写（长度/字符约束通过）+ 数字符号一致 + 证据非空 + 置信 ≥ **0.95** |
   | `figure.bbox` | 页内合法 + 与现有框 IoU 极低或极高 + 与 caption/OCR 锚点一致 + 置信 ≥ **0.95**；默认只提议不自动写 |
   | `figure.group` | 父图号候选唯一 + 证据非空 + 置信 ≥ 0.95 |

3. 目标被人工锁定（`human_locked`）或 `source=human_edited` / `label_source=review` → 永不自动覆盖；
4. 接受时写正式字段 + `audit_log(before/after)`；保留 proposal 供回滚；
5. 拒绝/低置信 → 留在待审队列（M3 UI）；
6. 用户编辑摘要文本 → `source=human_edited`、`edited_by/at`，自动接受失效；
   重新生成需用户显式触发（新 revision）。

---

## 6. LLM 适配（D4 细则，A 方案 + BYO key + mock 兜底）

- **M2a（无 key）**：`mock` provider（确定性假响应）先跑通「章节抽取 → prompt → JSON 解析 →
  证据校验 → 落库 → UI」全链路；
- `litbench/llm.py`：`LLMClient` 异步协议（`async complete(messages, json_schema?, meta?)`）；
- **M2b 真实 provider（2026-11-12 17:xx 起）**：用户在 LitBench 设置页里自己填
  `base_url / model / api_key`，**必须显式选择协议**，后端按协议组请求：

  | 协议 key | 名称 | 请求端点 | 兼容谁 |
  | --- | --- | --- | --- |
  | `chat_completion` | OpenAI Chat Completions | `POST {base}/v1/chat/completions` | OpenAI / DeepSeek / DSH 兼容网关 / 大多数第三方网关 |
  | `anthropic_messages` | Anthropic Messages | `POST {base}/v1/messages`（`x-api-key` + `anthropic-version: 2023-06-01`） | Anthropic Claude 原生 |
  | `openai_responses` | OpenAI Responses | `POST {base}/v1/responses`（`input[]` / `output[]`） | OpenAI 新版 Responses API |

- **配置优先级**：界面保存的配置（`source=ui`）> 服务端环境变量（`source=env`，
  `LLM_BASE_URL/LLM_MODEL/LLM_API_KEY`，旧的 OpenAI 兼容方式）> `mock`；
- **界面 API**：`GET/PUT/DELETE /api/settings/llm`、`POST /api/settings/llm/test`（可先用表单值
  测连接、不落库）；GET 只回**掩码**（`sk-t…7890`），key 原文永不回传前端；
- prompt registry：`litbench/prompts/<name>.md` + `prompt_version` 哈希；
- 每次调用记 tokens/耗时到生成物（费用字段留 0，各家计价不同，后续按 provider 补价目表）；
- 缓存（`(version, section, model, prompt_hash)`）与失败重试 ≤2 次仍待补，M2b 末尾一起做；
- 文献文本作为「数据」包在明确分隔符里，不执行其中的指令（prompt injection 防线）。

### 6.1 密钥归属与调用链路（2026-11-12 澄清，17:xx 更新为界面 BYO）

- **谁调用**：LitBench 服务端进程（`litbench/llm.py`），不是前端/浏览器；前端只提交
  「生成摘要」这个动作和「保存模型配置」这个动作。
- **链路**：UI → `POST /api/papers/{id}/summaries/generate` → 章节抽取 → `llm.py` →
  按协议请求（chat/completions、messages 或 responses）→ 证据校验 → `summary` 落库。
- **key 由谁持有/存哪（2026-11-12 17:xx 更新）**：用户/部署者**在 LitBench「设置 → 模型 API」
  页里自己填**；只写到服务端 `<storage_dir>/llm-config.json`（默认 `.data/llm-config.json`，
  `.data/` 已在 `.gitignore`，文件按 0600 创建）。**不进仓库、不进数据库、不下发前端**：
  配置接口只回掩码；日志不打印 key；可随时在页面清除/轮换。环境变量仍作为部署级兜底。
- **什么时候触发**：只在用户显式点「生成章节摘要」（单篇/批量）时；默认**不做**后台
  自动总结；同一章节重复生成要显式确认；缓存命中不重复调用（缓存待补）。
- **成本算谁**：算 key 所属账户（用户自己）。每次调用把 tokens/耗时记到 `summary` 行；
  费用留 0（各家计价不同，后续按 provider 补价目表）；预算上限/缓存/重试在 M2b 收尾时补。
- **为什么不复用 DSH 会话额度**：DSH 的模型额度绑定交互会话，LitBench 是独立服务，
  当前没有受支持的「借额度」接口；硬接既不稳定也无法按文献/章节审计。如果确实想让
  agent 额度出摘要，那是 M4 的 `agent` provider（agent 领任务、结果回调落库），
  不是 M2 的默认路径。
- **接入形态（A 方案，已落地）**：DSH 背后是 OpenAI 兼容网关；用户在设置页填网关的
  `base_url/model/key`、协议选 `chat_completion` 即可。也支持 Anthropic Messages 和
  OpenAI Responses 两种原生协议（用户原话要求三种协议都必须注明）。
- **provider 抽象**：`mock`（无配置时默认）/ `api`（界面配置，三种协议）/ `env`（环境变量兜底）/
  `agent`（M4 预留）。接口不变，换 provider 不动摘要表与 UI。

### 6.2 模型 API 设置页（M2b 基础设施，2026-11-12 17:xx）

用户明确要求：**不要只靠环境变量，直接在 LitBench 界面上人工配置 base_url / model / API key，
并且必须注明支持哪种协议**（用户原话提到 anthropic messages、chat completion、responses 三种）。

- 界面：顶栏「设置」→ 浮层，当前生效卡片（来源 ui/env/mock、协议、端点、model、key 掩码、
  更新时间）+ 表单（三协议 radio 卡片、base_url、model、api key、超时）+ 按钮
  （保存配置 / 测试连接 / 清除配置 / 关闭）+ 安全说明。
- 三种协议都必须写明名称、端点路径、适用场景，并且**协议清单由后端 `supported_protocols`
  下发**（前端不维护第二份，避免漂移）。
- 后端：
  - `litbench/llm_config.py`：协议登记表、`LLMConfig`、load/save/delete（原子写 + 0600）、
    `endpoint_url()` 归一化（用户填 `https://api.deepseek.com` / `.../v1` / 完整端点三种形式都对）、
    掩码、校验；
  - `litbench/llm.py`：`HttpLLMClient`（异步）按协议组 headers/body、解析 content/usage；
    `get_client()` 解析优先级 ui > env > mock；`test_connection()` 发最小请求；
  - API：`GET/PUT/DELETE /api/settings/llm`、`POST /api/settings/llm/test`（可先用表单值测，
    不落库）；422/400 都带可读中文；
  - 存储：`<storage_dir>/llm-config.json`（默认 `.data/`，.gitignore，不进 DB/仓库/日志）；
    GET 只回掩码（`sk-t…7890`），key 原文永不回传前端。
- 顶栏状态灯：`last_test_ok` / `last_tested_at` 落盘（只有测试连接会写，配置未变时保留、
  改配置后重置）。已连接显示绿点「已连接」；未配置显示灰点「模型未配置」。
- **未配置模型时禁用 AI 功能**：文献详情的「章节摘要」页签置灰，点击直接打开设置页引导配置；
  不再向用户解释 mock。设置页文案精简：主标题下只保留一句「由于 LitBench 需要使用模型的
  视觉能力，因此建议配置原生多模态模型。」；协议卡片只留名称+端点（去掉请求头/适用场景描述）；
  API Key 与底部安全说明文字全部去掉；Model 下只留「大小写敏感」；Base URL 下保留地址拼接说明。
- 配置后章节摘要自动走真实模型；清除后回落到环境变量。真实 key 的连通性与摘要质量已在
  2026-11-12 用 OpenCode 的 `deepseek-v4.1-flash` 实测（测试连接 ok，36/15 tokens）。

---

## 7. MCP 设计（M4 摘要）

- 工具三档：read（只读查询）/ propose（提交提案）/ task（远程 agent 领任务）；
- 传输：stdio（本地 agent）+ Streamable HTTP（远程）；Bearer token + scope；
- 许可边界：`mcp/` MIT，只依赖 `schemas/` 契约，通过 HTTP 调 AGPL 服务端；
- 审计：每个工具调用记 actor/tool/params 摘要/耗时，不记录完整文献原文。

---

## 8. 安全、许可与成本

- 路径守卫（已有 `paths.py`）覆盖所有新接口；Zotero sqlite 只读 URI，禁止复制/写；
- token：human / agent-read / agent-propose / agent-task 四种 scope；
- 提示注入：文献内容 untrusted，工具描述与系统提示双重声明；
- 成本：BYO key；按任务预算；缓存；自动接受前只生成一次；
- 许可：新依赖（OpenAI SDK、Zotero SDK 如引入）过 `check_licenses.py`；MCP 保持薄层。

---

## 9. 风险与备选

| 风险 | 应对 |
| --- | --- |
| Zotero schema 随版本变化 | 导入器按「字段名 + 缺省容忍」写，不硬编码 itemTypeID；失败给明确错误 |
| 自动接受把错误写进库 | 仅白名单 + 阈值 + 可回滚 + 抽检；人工编辑对象永不覆盖 |
| 章节摘要幻觉 | evidence_block_ids 强校验；摘要 UI 可点回原文；prompt 要求「只基于给定文本」 |
| LLM 成本失控 | 缓存 + 按章生成 + 预算上限 + 费用审计 |
| 范围蔓延 | 每阶段有 out-of-scope；M1 不做 PDF 导入/双向/摘要 |

---

## 10. 进度日志（截至 2026-11-15 13:xx；之后见 §12.42–§12.47）

- 2026-11-12：立项，D1–D4 拍板；M1 开工。
- 2026-11-12 14:2x：**M1 完成**（提交 `6fe581f`，详见 §11）。
- 2026-11-12 15:00：**决策更新**——D4 确认为 A 方案（DSH 背后的 OpenAI 兼容网关直连），
  并定 D5（M2 前先插 M1.5 装 Zotero 实测）、D6（mock 先行、真实 key 后接）。
  下一步：M1.5（约 0.5 天）→ M2a（无 key 骨架）→ M2b（接真实网关）。
- 2026-11-12 15:5x：**M1.5 完成**（提交 `0bd6cdd`）——Zotero 10.0.8 真机安装；`sqlite`
  （Zotero 关闭）与 `local API`（Zotero 开启）两条路实测通过，auto 在两种现场自动选路；
  真机暴露的锁库 / local API 未启用 / WAL 只读 -shm 三个问题已修；`pytest` **149 passed**。
  详见 §11.7。下一步：M2a（无 key 骨架）。
- 2026-11-12 16:xx：**M2a 后端完成**——迁移 0006（summary / summary_revision）、章节树
  `sections.py`（真实语料 5 本教材 34–69 节；pdfium 的 arXiv 老实退化成 5–6 个按页伪章节）、
  `llm.py` mock provider、prompt registry（内容哈希版本）、证据校验器（非空/存在/属于本节，
  不过不落库）、自动接受状态机（published + auto_accepted）、人工编辑锁定、
  revision/回滚、8 个 API 端点。真实语料生成：02-传热学 60 条 + arXiv 6 条，
  证据 100% 可回溯；`pytest` **172 passed**。前端页签在合并中，M2b 等 key。详见 §12。
- 2026-11-12 17:xx：**M2b 基础设施完成**——用户要求在界面里自己配 API（BYO key），
  新增「模型 API 设置页」：三协议显式选择（OpenAI Chat Completions / Anthropic Messages /
  OpenAI Responses）+ `GET/PUT/DELETE /api/settings/llm` + 测试连接；key 只存服务端
  `.data/llm-config.json`、接口只回掩码；无配置时仍走 mock。`pytest` **187 passed**。
  剩余：真实 key 到位后跑 1 本教材调 prompt、记 tokens/费用、校准阈值。详见 §12.8。
  （后端 `05bdfee`、前端 `b58217b`。）
- 2026-11-12 18:0x：**M3 计划细化（未开工）**——按用户要求把三项 AI 纠错全部排进计划：
  M3a 低置信图号复核（置信 ≥0.95 自动接受，候选集合+证据+归一化三道闸）、
  M3b 图注清洗（只去噪不改写，长度/数字符号约束）、M3c 框复核（多模态、
  默认只提议、IoU+锚点+置信三条同时满足才自动接受）；共用 `agent_task` /
  `review_proposal` / `audit_log`（迁移 0007）+ 任务租约 + 自动接受器 + 人审队列。
  等用户配好 API key 并用真实模型跑通 M2b 后开工。详见 §4 M3。
- 2026-11-12 19:5x：**AI 一键总结文献 + 图提取 AI 纠错轻量版完成并真机验收**——
  整篇速览（`summary.section_key="__paper__"`，一次模型调用、证据校验、编辑/发布/回滚）；
  图纠错建议队列（迁移 0007 `figure_proposal`，多模态 prompt，图号候选集合 + 图注长度约束 +
  裁框枚举三道闸，采纳才改图；顶栏 AI 建议抽屉 / 图详情内联建议 / 文献页「AI 修图」3 个入口）。
  真机费用：整篇速览 $0.0027、视觉纠错 $0.0034–0.0045/张；`pytest` **221 passed**。
  未做自动接受器 / `audit_log` / 租约表，详见 §12.10。
- 2026-11-13 15:xx：**M3c 框复核实现并真机跑通**——`bbox_review.py` 三图多模态（整页 +
  红框标注页 + 裁图）+ 归一化坐标/页内/最小尺寸/IoU/caption 锚点校验；默认只提议
  （`BBOX_REVIEW_AUTO_ACCEPT=0`，打开后需 IoU 极低/极高 + 锚点一致 + 置信 ≥ 0.95）；
  UI 待审队列显示框 diff/IoU/锚点；任务 kind `figure_bbox_review`。真机 6 张全 ok，
  模拟坏框 1 张 wrong→pending→采纳/回滚通过；后端 **297 passed**（新增 10 个）。详见 §12.19。
- 2026-11-13 14:xx：**M3 正式化完成（后端 + 前端）**——迁移 0010（audit_log / agent_task /
  proposal 回滚字段 / figure.caption_source）；自动接受器（白名单 + 阈值 0.95 + 证据 + 人工改过
  永不覆盖）；回滚 + 批量决策 + 审计；agent 任务租约（claim/heartbeat/retry/过期回收/幂等键）+
  内置 worker 执行 figure_review；前端三页签待审队列（before/after/批量/扫描/回滚/审计）。
  真机：10 个 figure_review 任务 → 1 条自动接受（图注 L/D 修复，0.96）、6 条 pending
  （0.90–0.94）、3 条 no_change；后端 **287 passed**，前端 build 通过，截图 `docs/assets/m3/`。
  详见 §12.18。
- 2026-11-13 13:3x：**新测试语料 10 篇（热/流体）+ M4 只读 MCP 出口交付**——按用户要求自选
  10 篇英文 arXiv 论文导入空库：10 parsed / **5,695 block / 140 图 / 19 表**、2 条整篇速览；
  同时交付 `mcp/` 只读 MCP 薄层（8 工具 / stdio + Streamable HTTP / token+scope / 限流 / 审计 /
  示例 agent），**MCP 102 tests + 后端 272 tests**。联调中修了 Docling 表格 HTML 全空与手工上传
  无 `paper.cite_key` 两个 bug。详见 §12.17。
- 2026-11-13 19:5x：**顶栏换 LitBench 徽标**——用户提供 JPG logo（书本 + 放大镜 + 字标），
  去白背 unmatte 成透明 PNG，顶栏「墨」字位置换成徽标（27px 高）+ 亮/暗两套主题校正
  （暗色下 brightness 1.5）；favicon 一并换；原图放大镜下的细线在小尺寸会糊，
  已用 smoothstep 清掉 JPEG 振铃。产物：`web/public/brand/`（emblem / 全 logo / favicon），
  截图 `docs/assets/brand/01-topbar-light.png`、`02-topbar-dark.png`、`03-brand-zoom-light.png`。
- 2026-11-13 20:5x：**M3 收尾 + M5 写侧交付**——迁移 0011 `claim`；写作台（claim 卡片 /
  派生对比矩阵 / LaTeX+refs.bib / BibTeX / Markdown / 确定性引用校验）；MCP 写侧 8 工具
  （proposal.create|list|get、task.list|claim|heartbeat|complete|fail）+ 多 scope 签发；
  M3 任务面板（状态/租约/费用/错误/重试）+ `POST /tasks/{id}/retry`；提案阈值校准脚本
  （以人工决策为 ground truth，样本不足如实输出）。后端 **311 passed**、MCP **110 passed**、
  前端 build 通过；写作台/任务面板截图 `docs/assets/m5/`。详见 §12.23 / §12.24。
- 2026-11-15 13:xx：**团队共享第一步：账号/会话/成员管理地基**——迁移 0013
  （app_user / user_session）、scrypt 密码、服务端 session + HttpOnly cookie、首次初始化管理员、
  管理员建号/停用/重置密码、登录页与顶栏用户菜单（成员管理浮层）。
  认证默认关闭（`AUTH_ENABLED=0`），`AUTH_ENABLED=1` 时所有 `/api/*` 先登录；
  后端 **325 passed**（+6 认证测试），前端 build 通过；截图 `docs/assets/team/01–05`。
  工作组/共享/数据归属尚未开始（下一步）。详见 §12.27。
- 2026-11-15 12:xx：**M5 写侧首次真机闭环（外部 agent + 真实模型）**——新增示例 agent
  `mcp/examples/agent_task_loop.py`（MCP stdio：领任务 → figure.get/evidence.get → 真实模型
  判图注 OCR 噪声 → proposal.create → task.complete）；模型给出 0.98 置信的
  「o fthe → of the」图注清洗，8 个证据块；UI 待审队列人审采纳后图注更新，审计含
  `proposal.create(actor=stdio)` 与 `proposal.accept(actor=human)`，回滚后图注恢复。
  顺带修掉待审卡片的「图号 →（无）」假 diff。详见 §12.26，截图 `docs/assets/m5/09–10`。
- 2026-11-13 22:xx：**References 条目生成**——13 种格式预设（GB/T 7714、APA 7、IEEE、MLA 9、
  Chicago、Nature、Science、ACS、Elsevier、Vancouver、BibTeX、RIS、自定义模板）；
  `paper` 补 volume/issue/pages（迁移 0012，Zotero 导入带上、可手动补全）；
  写作台新增 References 页签（逐条复制 / 全部复制 / 下载 / 缺字段提示）。
  后端 **319 passed**（+8）、MCP 110、前端 build 通过；截图 `docs/assets/m5/06–08`。详见 §12.25。

---

## 11. M1 实施记录（2026-11-12 14:2x）

### 11.1 数据

- 迁移 `0005_knowledge_base.py`：
  - `paper` 加 `abstract` / `tags_json` / `reading_status` / `zotero_item_key`(unique) /
    `metadata_source` / `imported_at`；
  - 新表 `collection`（parent_id 自关联、`zotero_key` 唯一、source=manual|zotero）、
    `paper_collection`（联合主键）、`import_run`（导入审计）。
- dev SQLite 已手工执行同样 DDL（没有 alembic 版本表，见 docs/09 §6）。

### 11.2 Zotero 只读导入（`server/litbench/zotero.py`）

三种来源，统一输出集合/条目结构：

| 来源 | 实现 | 说明 |
| --- | --- | --- |
| `json` | `load_json_source` | Zotero local API 形状或 `{collections, items}`；离线/测试 |
| `sqlite` | `load_sqlite_source` | `file:...?mode=ro` 只读 URI，不复制、不写用户库；读 collections/items/itemData/fields/values/itemCreators/tags |
| `api` | `load_api_source` | Zotero 7 local API（`http://127.0.0.1:23119/api/users/0`），分页拉取 |

- `auto`：先找 `~/Zotero/zotero.sqlite`（或 `ZOTERO_SQLITE`），没有则尝试 local API；
- 幂等：集合按 `zotero_key`、条目按 `zotero_item_key` upsert；重复导入只更新；
- 元数据以 Zotero 为准（标题/作者/年份/DOI/摘要/标签），不动解析产物；
- 集合关联只同步 `source=zotero` 的集合，**手动集合不会被删掉**；
- 失败写 `import_run.error` 并抛 `ZoteroImportError`（HTTP 422 带可读原因）。

### 11.3 API（知识库）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/collections` | 平铺目录树（含 `paper_count`） |
| POST | `/api/collections` | 新建集合（可指定 parent） |
| PATCH | `/api/collections/{id}` | 改名/移动（禁止挂到自己的子孙下） |
| DELETE | `/api/collections/{id}` | 删集合：子集合上提、文献关联删除、文献本身不动 |
| POST/DELETE | `/api/collections/{id}/papers` | 手动归档/移出 |
| POST | `/api/import/zotero` | 只读导入（auto/json/sqlite/api） |
| POST | `/api/import/zotero/file` | 上传 Zotero 导出的 JSON 导入 |
| GET | `/api/import/runs` | 导入审计 |
| GET | `/api/papers?collection_id=` | 按集合过滤；**点父集合包含整棵子树** |

### 11.4 前端

- 侧栏新增「知识库」：目录树（展开/折叠、父子层级、每级显示**子树**文献数）、
  「导入 Zotero」「JSON 导入」「＋ 新建集合」；
- 点集合 → 主区变成文献卡片视图（标题/年份/期刊/tags/图表数/来源）；
- 顶栏总数不再受集合过滤影响；点「全部文献」回到全库图表视图。

### 11.5 验收

- `pytest` **143 passed**（+5 知识库测试：collections CRUD/移动保护、JSON 导入幂等、
  手动集合不被同步删、上传 JSON、sqlite 只读字节不变、父集合子树过滤）。
- dev 库手工建了示例树「专业课复习 → 传热学(1) / 自动控制原理(1)」，点父级出 2 篇；
- 截图：`docs/assets/kb/01-knowledge-tree.png`、`02-collection-paper-list.png`。
- 本机没装 Zotero，所以 auto 源未对真实 Zotero 实测；json/sqlite 源用 fixture 覆盖，
  local API 代码路径按 Zotero 7 Web API 形状实现。**这一缺口已排进 M1.5（先装 Zotero 实测，
  再开 M2）**。→ 2026-11-12 15:5x 已补，见 §11.7。

### 11.6 已知限制（留给 M1.5 / M2）

- Zotero 附件 PDF 自动导入：2026-11-12 已完成（B 方案，见 §12.9）；更早只同步元数据/集合，
  期间先支持了手工补传 PDF（`POST /api/papers/{id}/pdf`）；
- `json` 来源（Zotero 导出文件）拿不到附件文件，仍只导元数据；
- 导入是同步请求，库很大时慢；后续可改成 job 后台跑；
- 没有「撤回一次导入」的 UI（数据里有 import_run 可追溯）；
- 阅读状态/标签只在库里，UI 还没有编辑入口（M2 摘要时一起做文献详情页）。

---

## 11.7 M1.5 实施记录：Zotero 真机实测（2026-11-12 15:5x）

### 11.7.1 环境与安装

- 官方包里最新是 **Zotero 10.0.8**（`Zotero-10.0.8_x64_setup.exe`，约 110MB）。
  这个 exe 是 **7z 自解压包 + 内层 NSIS 安装器**；本机是标准用户（在 Administrators 组但
  UAC 未提权），`/S` 静默安装返回 2（需要管理员），计划任务提权也被系统拒绝。
- 解法：用 `tar.exe`（bsdtar）解开 7z 外层，拿到 `core/` 应用文件，放到
  **纯 ASCII 路径 `D:\litbench-zotero\`** 运行（便携式）。**非 ASCII 安装路径会报
  “Profile Missing”**；`C:\litbench-zotero` / `D:\litbench-zotero` 正常。
- 数据目录仍按 Zotero 默认 `C:\Users\<user>\Zotero`；`zotero.sqlite` 使用 WAL。
- **Zotero 10 的 local API 默认关闭**：直接请求 `http://127.0.0.1:23119/api/...` 返回
  `403 Local API is not enabled`。在 profile 的 `user.js` 写入
  `user_pref("extensions.zotero.httpServer.localAPI.enabled", true);` 重启后 200。
  → 文档/README 要写这一句，不然用户开箱即报 403。

### 11.7.2 真实库内容（用于实测）

- 条目 3 条：`conferencePaper` Attention Is All You Need（2017，DOI）、
  `journalArticle` Deep Residual Learning（2016，DOI）、中文 `纳米流体强化传热研究进展`
  （2021，《工程热物理学报》，标签「传热学/纳米流体」）；
- 集合 2 个：`文献综述`（根）→ `纳米流体`（子）；条目 1 归父、条目 3 归子、条目 2 不归档；
- 条目通过 Zotero Connector 的 `/connector/saveItems` 接口写入（官方接口，不用 GUI）。

### 11.7.3 两条路实测结果

| 现场 | 实际来源 | 集合/条目 | 幂等（第二次） | 耗时 |
| --- | --- | --- | --- | --- |
| Zotero 关闭，`auto`（落 sqlite） | `sqlite` | 2 / 3，父子与归属全对 | 新建 0、更新 0 | ~1.0s |
| Zotero 开启 + local API，`auto`（落 api） | `api` | 2 / 3，`collections` 字段全对 | 新建 0、更新 0 | **206ms** |

- 元数据核对：标题、作者（含中文姓名）、年份、DOI、摘要、标签、期刊/会议名都按 Zotero 落库
  （`paper.authors_json` 等）；LitBench 集合按 `zotero_key` 建树，父集合过滤包含整棵子树。
- **不写用户库**：sqlite 导入前后 `zotero.sqlite` 的 sha256 一致
  （`CB7FFE18…9B7444`）；local API 本来就是只读接口。
- auto 选路：先 0.8s 探 `127.0.0.1:23119` —— Zotero 开着就优先 API（**7.4s → 0.2s**，
  省掉 sqlite 锁等待）；关着就先把 sqlite 读完；两边都失败才报错，并把两个原因都列出来。

### 11.7.4 真机暴露并修掉的问题

1. **Zotero 运行时 sqlite 被锁**：WAL 库 `mode=ro` 报 `database is locked`。
   → `_friendly_sqlite_error` 给出可读提示（先退出 Zotero，或启用 local API 用 api/auto）；
   auto 改为「API 试一次 → sqlite 试一次」的互补顺序。
2. **local API 未启用时 403 不可读**：→ 明确提示到 Zotero 设置 → 高级 → 配置编辑器里
   把 `extensions.zotero.httpServer.localAPI.enabled` 设为 true。
3. **只读环境里 WAL 库建不了 `-shm`**（本机 harness 的受限进程能复现）：`mode=ro` 报
   `unable to open database file`。→ 当 `-wal` 为空/不存在（Zotero 正常退出、主库即最新）时，
   回退 `immutable=1` 只读主库；`-wal` 有内容则不回退，避免读到旧快照。仍不写用户库。
4. auto 探活优化（上表 7.4s → 206ms）。

### 11.7.5 测试与验收

- `pytest` **149 passed**（较 M1 的 143 +6：auto 回退 API、API 优先、双源失败信息、
  锁库提示、local API 未启用提示、immutable 回退；另有用例改用共享 fixture）。
- 验收对照：集合树与 Zotero 一致 ✓；重复导入幂等 ✓；Zotero 侧数据不进 LitBench 的解析产物 ✓；
  不写 Zotero 库 ✓（哈希）；错误提示可读 ✓；界面「导入 Zotero」按钮走 `auto`，
  端到端已由 HTTP 导入覆盖（本次未重跑 UI 截图）。
- 遗留：Zotero 附件 PDF 自动导入已补（B 方案，见 §12.9）；local API 需要用户手动开；
  **分页在 >100 条的真实库未实测**
  （本次只有 3 条；`_get_all` 的分页逻辑仍只有 mock 覆盖），后续用大库或 120 条生成库补测。

---

## 12. M2a 实施记录：无 key 章节摘要骨架（2026-11-12 16:xx）

### 12.1 交付清单

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 数据 | `migrations/versions/0006_summary.py`、`models.py` | `summary`（唯一 `(paper_version_id, section_key)`）、`summary_revision`；dev SQLite 手工执行同款 DDL |
| 章节树 | `litbench/sections.py` | 从 block 启发式切章：章/节/编号/一、/1. 级别识别、running header 过滤、章标题跨块合并、习题/参考文献收成尾章节、pdfium 退化成按页伪章节 |
| LLM | `litbench/llm.py` | `LLMClient` 协议 + `MockLLM`（确定性、可伪造坏 JSON/假证据/低置信/失败）+ `DeepSeekLLM`（M2b 占位，显式报「还没接」） |
| Prompt | `litbench/prompts.py`、`prompts/section_summary.md` | prompt 文件入库；`prompt_version` = 内容 sha256 前 12 位 |
| 证据 | `litbench/evidence.py` | 证据非空、必须存在于本节正文、去重保序；summary/key_points 基本校验；不过 → `EvidenceError`，**拒绝落库** |
| 服务 | `litbench/summaries.py` | 生成（带证据快照→校验→自动接受）、编辑锁定、发布、revision 列表/回滚；每节独立提交，失败不阻塞其它节 |
| API | `routers/summaries.py` | 见下表 8 个端点 |
| 前端 | `web/src/ChapterSummary.tsx`、`App.tsx`、`api.ts`、`types.ts`、`styles.css` | 文献详情「图表 / 章节摘要」页签：左章节树（状态标签/分组）、右摘要详情（正文/要点/证据）、证据点回原文高亮、编辑锁定/发布/重新生成/历史回滚；截图 `docs/assets/summaries/` |
| 测试 | `tests/test_sections.py`、`tests/test_llm_evidence.py`、`tests/test_summaries_api.py` | 23 个新测试（章节树 8 / llm+证据 7 / API 端到端 8） |

### 12.2 API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/papers/{id}/sections` | 章节树 + 每节摘要状态（`kind=heading|page`，`block_count=0` 是纯父节点） |
| POST | `/api/papers/{id}/summaries/generate` | `{section_keys?, force?}`；返回 created/updated/skipped_existing/skipped_locked/skipped_empty/failed + 每节结果 |
| GET | `/api/papers/{id}/summaries` | 该文献最新版本的摘要列表 |
| GET | `/api/summaries/{id}` | 摘要全文 + 证据块（block_id/page/type/text/bbox，可点回原文） |
| PATCH | `/api/summaries/{id}` | 人工编辑：`source=human_edited`、`human_locked=true`、published，并留 revision |
| POST | `/api/summaries/{id}/publish` | draft → published |
| GET | `/api/summaries/{id}/revisions` | 历史版本（reason: edited/regenerated/restored…） |
| POST | `/api/summaries/{id}/revisions/{rid}/restore` | 回滚到某个历史版本（当前内容再压一份 revision） |

### 12.3 章节树在真实语料上的结果（dev 库，2026-11-12）

| 文献 | 解析器 | blocks / 页 | 章节数（其中纯父节点） |
| --- | --- | ---: | ---: |
| 01-机械设计基础 | docling | 456 / 39 | 60（4） |
| 02-传热学 | docling | 652 / 46 | 69（9） |
| 03-自动控制原理 | mineru | 552 / 47 | 34（1） |
| 04-燃烧学 | mineru | 366 / 29 | 32（3） |
| 05-电子技术 | mineru | 458 / 39 | 60（3） |
| 9 篇 arXiv（pdfium） | pdfium | 434–1747 / 13–33 | 5–6（0，`kind=page`） |

- 教材的章（第 7 章）/节（7.1）/小节（7.1.1）/一、/1. 层级基本正确；02 的「第 7 章」被拆成两块
  时按同页相邻合并；每页重复的章名（running header）只保留第一次当入口；
  后段「复习题/参考文献」之后不再切章节，统一收成一个尾章节。
- **pdfium 不做标题猜测**：pdfium 只有逐行 text、两栏交错，从行里猜标题会产生一堆假章节
  （实测 arXiv 的「57.4 Abot-M0」这类表头数字都能命中编号模式），因此老实退化成 5–6 个按页
  伪章节，UI 里 `kind=page` 标明。这是 M2a 的已知取舍，M2b 再考虑用模型辅助切章。
- 已知噪声：教材 OCR 噪声标题会进树（如「7,3 …」「I. 不凝结气体」），m2a 接受。

### 12.4 生成链路与真实语料结果（mock provider）

1. `GET sections` → 章节树（02：69 节，含 9 个空父节点）；
2. `POST generate`（不传 section_keys）→ 02：**created 60 / skipped_empty 9 / failed 0，0.13s**；
   arXiv（33 页，6 个按页伪章节）：created 6 / failed 0；
3. 每条摘要：`evidence_block_ids` 非空、全部属于本节正文 block（SQL 复核 0 条越界）、
   `provider=mock`、`prompt_version=48219a77b7f8`、mock 置信度 0.95 ≥ 阈值 0.9 →
   服务端自动接受为 `published`、`auto_accepted=true`；
4. 人工编辑一条（7.2 膜状凝结）→ `source=human_edited`、`human_locked=true`、留 1 个 revision；
   再次 `generate`（不 force）→ **skipped_locked=1 + skipped_existing=59**，人工文本没被覆盖；
5. 生成是同步请求（M2a mock 很快；M2b 接真实模型后需要 job 化/预算限制）。

### 12.5 测试与验收

- `pytest` **172 passed**（149 + 23）。
- 覆盖三条硬约束：证据不合格不落库；人工编辑后不被自动覆盖（force 才覆盖）；生成/编辑前留
  revision 可回滚。API 端到端用 SQLite 临时库跑「建文献版本 + block → 章节树 → 生成 → 编辑 →
  重生成 skip → 部分失败 → 回滚」全链路。
- dev 库已有演示数据：02-传热学 60 条、arXiv cs.CV 6 条 mock 摘要，其中 1 条人工编辑。

### 12.6 前端「章节摘要」页签（M2a）

- 文献详情主区加子页签「图表 / 章节摘要」，默认图表；切到摘要后：
  - 左栏章节树：按 level 缩进、显示 `pX-Y` / 图数 / 状态标签（无摘要 / 草稿 / 已发布 /
    人工 / 分组），`block_count=0` 的纯父节点显示「分组」且不可生成；
  - 右栏摘要详情：状态（published/draft、agent/human_edited、自动接受、provider/model/
    prompt_version）+ 正文 + 要点 + 证据块（`p.页码` + 文本前 120 字）；
  - 证据块点击 → 复用 `PdfViewer` 打开原文对应页并高亮 bbox（实测截图见下）；
  - 操作：生成全部 / 生成本节 / 编辑（文本+要点，保存即 human_edited + 锁定）/ 发布 /
    重新生成（force，二次确认）/ 历史版本（reason+时间+回滚）；
  - 失败可见：生成结果里 failed 的章节标题+原因以 banner 列表展示，不静默。
- 截图：`docs/assets/summaries/m2a-01-chapter-summary.png`（章节树+摘要+证据）、
  `m2a-02-evidence-viewer.png`（点证据回原文第 1 页并高亮）。
- 构建：`npm run build`（tsc --noEmit + vite build）通过，产物由 4173 preview 直接服务。
- 已知小限制：重新生成后右侧详情由前端手动再拉一次（章节树刷新但 summary_id 不变）；
  章节很多时是一次性渲染（教材 69 节实测无卡顿，更大库后续再虚拟化）。

### 12.7 已知限制（留给 M2b / 后续）

- mock 摘要是**占位文本**（「本节共 N 个证据块…」），只用于验证链路，不代表摘要质量；
- 真实 provider 三协议已用真实 key 实测（§12.13）；prompt 在真实模型下**未改**（试跑证明
  主要瓶颈是推理模型输出预算，已从 1200 提到 3000 + 自动翻倍重试）；
- prompt 只有中文一版；token/费用字段在真实 provider 下取自 API `usage`；**结果缓存、任务化**
  仍未做（M2b 已补输出预算、推理耗尽重试、失败记账，见 §12.13.6）；
- 章节树对 pdfium 文档只有按页伪章节；教材 OCR 噪声标题会进树；
- 生成时一次最多送 `SUMMARY_MAX_CHARS=6000` / `SUMMARY_MAX_BLOCKS=300`（字符预算为主；
  pdfium 行级块需要更多条数才填得满，2026-11-13 审查修正），长章节会截断（摘要
  证据仍只来自本节，不会串章节）。

---

## 12.8 M2b 基础设施：模型 API 设置页 + 三协议适配（2026-11-12 17:xx，后端 `05bdfee` / 前端 `b58217b`）

### 12.8.1 为什么改决策

原 D4/§6.1 写的是「key 只放服务端环境变量」。用户 2026-11-12 明确要求：
**直接在 LitBench 界面上人工配置 base_url / model / API key，更符合用户自己的使用环境，
并且必须注明支持哪种协议（anthropic messages / chat completion / responses）**。
因此改成「界面 BYO 配置优先，环境变量兜底」，同时保留「key 不进仓库/DB/前端」的底线
（只写服务端 `.data/llm-config.json`，GET 只回掩码）。

### 12.8.2 交付

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 配置 | `litbench/llm_config.py` | 协议登记表（3 种）、`LLMConfig`、load/save/delete（原子写 + 0600）、`endpoint_url` 归一化、掩码、校验 |
| 适配 | `litbench/llm.py` | 异步 `LLMClient`；`HttpLLMClient` 三协议（Chat Completions / Anthropic Messages / OpenAI Responses）；`get_client()` 优先级 ui > env > mock；`test_connection()` |
| API | `routers/settings.py`、`schemas.py` | `GET/PUT/DELETE /api/settings/llm`、`POST /api/settings/llm/test`；GET 只回掩码；PUT 的 api_key 留空=保留；测试可先用表单值（不落库） |
| 前端 | `web/src/LlmSettings.tsx` + `api.ts/types.ts/App.tsx/styles.css` | 顶栏「设置」浮层：三协议完整说明、base_url/model/key/超时、key 掩码、测试连接、清除、安全说明；截图 `docs/assets/settings/` |
| 测试 | `tests/test_llm_protocols.py`、`tests/test_settings_api.py` | 三协议的 URL/headers/body 与响应解析（`httpx.MockTransport`）、错误可读、配置保存/掩码/清除/校验、测试连接不落库 |

### 12.8.3 协议与端点（后端真实请求形状）

| 协议 | 请求 | 响应取文本 | 备注 |
| --- | --- | --- | --- |
| `chat_completion` | `POST {base}/v1/chat/completions`，`Authorization: Bearer`，body `{model, messages[], max_tokens}` | `choices[0].message.content` | OpenAI / DeepSeek / DSH 兼容网关 |
| `anthropic_messages` | `POST {base}/v1/messages`，`x-api-key` + `anthropic-version: 2023-06-01`，system 拆到顶层、messages 只留 user/assistant | `content[].text` | Claude 原生 |
| `openai_responses` | `POST {base}/v1/responses`，system→`instructions`，user/assistant→`input[]`（`input_text`） | `output[]` 里 `type=message` 的 `output_text` | OpenAI 新版 Responses |

`endpoint_url()` 兼容用户填 `https://api.deepseek.com`、`https://api.openai.com/v1`、或完整端点；
usage 分别读 `prompt_tokens/completion_tokens` 或 `input_tokens/output_tokens`；费用暂记 0。

### 12.8.4 验证

- `pytest` **194 passed**（新增：三协议/OpenCode 兼容 10 + 配置接口 11 + 补传 PDF 1；mock 改异步、删旧
  deepseek 占位）。
- API 实测（无真实 key，用不可达地址）：PUT 保存后 GET 返回 `configured=true / source=ui`,
  `api_key_masked=sk-d…cdef`，响应里**没有 key 原文**；文件只在服务端
  `.data/llm-config.json`（285B）；测试连接返回可读错误
  `连不上 http://127.0.0.1:9/v1/chat/completions：ConnectError…`；DELETE 后回到 `source=mock`、
  文件删除、摘要重新走 mock（04 的 no-force 生成仍 `skipped_existing=6`，未写库）。
- 前端 `npm run build` 通过；设置浮层截图（`docs/assets/settings/01-llm-settings.png` 三种协议
  都可见、`02-llm-settings-test-error.png` 测试连接失败的可读错误），对应前端提交 `b58217b`。
- **未验证**：真实第三方 key 的连通性、真实摘要质量、各家 usage/error 细节差异。
- **2026-11-12 晚更新（本节以上是当时记录）**：设置页收口后 `source` 已改为
  `ui | env | none`（不再有 `mock` 来源），DELETE 清除配置后 AI 功能禁用并引导去设置页，
  不会再回 mock；顶栏状态灯只有「测试连接成功」才显示绿点。测试数已增至 **200**
  （见 §12.9 与 `docs/09`）。真实 key 已配好并测试连接成功（OpenCode pay-as-you-go），
  但还没跑真实章节摘要。

### 12.8.5 安全边界

- key 只写服务端文件 `<storage_dir>/llm-config.json`（默认 `.data/`，`.gitignore`，0600）；
  GET 永远只回掩码；日志/异常信息不含 key；可在页面随时清除/轮换。
- 前端不会把 key 存 localStorage；表单里的 key 只随 PUT/POST 请求体走一次。
- 服务端不做「替用户猜协议」：协议必须显式选择，端点由协议 + base_url 决定。
- 仍是本地单用户工具：无鉴权，部署到公网前需要加认证（已在技术债里）。

### 12.8.6 实测目标之一：OpenCode Go / Console 怎么填（2026-11-12）

用户实际要接 **OpenCode Go 订阅**（[官方 Go 文档](https://opencode.ai/docs/console/go/) /
[Inference API](https://opencode.ai/docs/console/api/inference/) /
[Harnesses Base URLs](https://opencode.ai/docs/console/harnesses/)）。填法（三选一，按模型选）：

| 模型族（Go 模型 ID 举例） | 协议 | Base URL 填 | 端点由后端拼成 |
| --- | --- | --- | --- |
| GLM / Kimi / DeepSeek / MiMo / MiniMax M3.1 / LongCat / Hy / Step（如 `glm-5.3`、`kimi-k3.1-pro`、`deepseek-v4-pro`） | OpenAI Chat Completions | `https://opencode.ai/inference/go/openai/v1` | `/v1/chat/completions` |
| GPT / Grok / Muse（如 `gpt-6-luna`、`grok-4.7`） | OpenAI Responses | `https://opencode.ai/inference/go/openai/v1` | `/v1/responses` |
| Claude / Qwen / MiniMax M3（如 `claude-haiku-5-5`、`qwen3.8-max`、`minimax-m3`） | Anthropic Messages | `https://opencode.ai/inference/go/anthropic/v1` | `/v1/messages` |

- API Key 用 Console 的 **personal API key**（形如 `oc_sk_...`），不是 OpenCode 登录 token。
- Console 会把任意文本模型翻译到请求的 API 族；因此**最省事是统一选 Chat Completions**
  （base `.../go/openai/v1`），Claude/GPT 也能出结果（原生协议保真度更高，可选）。
- 我们已按官方要求补了两个兼容点：① 对 `opencode.ai` 主机自动带
  `x-opencode-session`（保存配置时生成、之后复用，满足路由/prompt cache 要求）；
  ② Anthropic 协议路径同时发 `x-api-key` 和 `Authorization: Bearer`（Console 认 Bearer，
  原生 Anthropic 认 x-api-key）。
- **重要限制（如实说）**：
  - Go 订阅官方定位是 **coding agent 流量**（"designed for OpenCode and other coding agents",
    "traffic is monitored for abuse"）；LitBench 的整章文献摘要不是典型编码 agent 请求，
    用 Go 可能被判滥用或限流；要长期跑建议用 **Console pay-as-you-go**
    （base 去掉 `/go`：`https://opencode.ai/inference/openai/v1` 或 `.../anthropic/v1`）。
  - **免费档模型只在 OpenCode 客户端内可用**，其它客户端（含 LitBench）即使有 key 也可能
    403；请选 Go 的付费模型（如 `glm-5.3-flash`、`kimi-k2.7-code`、`deepseek-v4.1-flash`）。
  - 模型列表/计价会变，以 Console 的 `GET /inference/go/v1/models` 为准。


---

## 12.9 纯元数据文献补传 PDF（2026-11-12 18:4x）

用户反馈：Zotero 导入的文献点开什么都没有、「人工增图」点了没反应。

**原因**：Zotero 导入按 D1 只镜像元数据，PDF 附件不在导入范围 → 没有 `paper_version` →
没有 PDF/解析结果 → 增图打开 PDF 查看器时 `/api/papers/{id}/pdf` 404，前端没拦。

**A 方案（本次完成）**：

- 后端 `POST /api/papers/{id}/pdf`：给已有文献补传 PDF——流式落盘 + sha256 去重，建
  `paper_version` + 排队 parse job；若该 PDF 已属于别的文献返回 409（不替用户合并）。
- `PaperOut` 新增 `has_version`（列表/详情都带）；有版本才算「有 PDF」。
- 前端：
  - 侧栏文献行加「仅元数据」标记；
  - 打开这类文献时主区显示「这篇文献还没有 PDF」+「上传 PDF 并解析」；
  - 顶部「＋ 增图」换成「＋ 上传 PDF」；上传后复用原来的 job 进度条，解析完成自动刷新
    （has_version 变 true → 出图表）；
  - 章节摘要页签在无 PDF 时禁用并提示「需 PDF」。
- 实测：给 `Attention Is All You Need`（Zotero 导入、原 0 版本）补传一个 1 页 PDF →
  job 1s 内 done、`has_version=true`、解析出 1 张图；重复上传幂等、跨文献 409 有单测。

**B 方案（本次完成，2026-11-12 19:0x）**——Zotero 附件 PDF 自动导入：

- 读取：sqlite 源读 `itemAttachments`（stored = `storage/<attachmentKey>/<filename>`，
  **DB 里存的是 `storage:<filename>`**；linked = 绝对路径，`attachments:` 前缀已处理）；
  local API 源另拉 `/items?itemType=attachment`，按 `parentItem` 挂到条目上。
- **真机踩坑（2026-11-12）**：local API 的 `linkMode` 是字符串（`imported_file` 等），
  不是 sqlite 里的整数；真实文件路径优先取 `links.enclosure.href`（`file:///...`），
  再退回 `storage/<key>/<filename>`。两者都已按真机实测修正并有单测。
- 导入：每个条目取**第一个可读 PDF**，`shutil.copyfile` 复制到 `pdf_dir/<sha2>/<sha>.pdf`
  （**只 copy 不 move**，Zotero 仍是文件主人），建 `paper_version(kind="imported")` +
  parse job；按 sha256 幂等，同一 PDF 已属于别的文献则跳过（不替用户合并文献）。
- 统计（`ImportRun.stats_json` 新增，导入完成 toast 也会显示）：
  `pdfs_imported` / `pdfs_skipped` / `pdf_files_missing`（DB 有附件记录但磁盘没文件）。
- 新增配置 `ZOTERO_STORAGE`（附件目录；留空时 sqlite 源用 db 同级 `storage/`、
  api 源用 `~/Zotero/storage`）。
- 测试：+6 个（stored/linked 路径解析、API linkMode/enclosure、导入建版本+排任务、
  重复导入幂等、跨文献不合并、文件缺失统计）；全库 **200 passed**。
- 真机验收：给测试库条目 `2UNTSCST`（Deep Residual）挂上 arXiv 1512.03385 的真实 PDF
  （备份 `zotero.sqlite.bak-battach`）→ `auto` 走 local API → `pdfs_imported=1` →
  pdfium 解析 12 页/930 blocks（0 图）触发增强档 docling → **7 图 15 表**；
  界面 `docs/assets/papers/03-zotero-pdf-imported.png`。
- 边界：`json` 来源（导出文件）拿不到 PDF，仍只导元数据；local API 需 Zotero 运行中；
  linked 文件的相对基目录（`attachments:` 的 base dir）尚未支持。

---

## 12.10 AI 一键总结文献 + 图提取 AI 纠错（2026-11-12 晚，轻量版）

用户要求：在已有「章节摘要」之外，增加 **AI 一键总结整篇文献** 和 **对图片提取结果纠错**
的功能，并且要有入口、界面、按键。两个功能都按「所有 AI 产物必须有证据 / 可回溯 /
可人工推翻」的既有原则实现。提交见 `git log -1`；测试 **221 passed**。

### 12.10.1 整篇速览（AI 一键总结文献）

- **接口**：`POST /api/papers/{id}/summary`（生成；`{force?}`）、
  `GET /api/papers/{id}/summary`（读；没生成过 404）。
- **落库**：复用 `summary` 表，`section_key="__paper__"`、`section_title="整篇速览"`、`level=0`；
  因此证据校验、自动接受（置信 ≥0.9）、人工编辑锁定、revision 回滚全部免费复用。
  章节摘要列表接口会过滤掉 `__paper__`，不会混进章节树。
- **材料与成本**：整篇**只调一次**模型。prompt `litbench/prompts/paper_summary.md`；
  输入 = 标题/作者/年份/DOI + 作者摘要 + 章节大纲 + 引言/结论附近的正文证据块
  （`paper_summary.py` 里按首尾预算挑，跳过参考文献）。
- **前端**：文献页新增页签「AI 速览」——空态是「一键总结这篇文献」按钮（生成中显示
  「AI 正在通读全文…」）；生成后显示正文 + 要点 + 可点击回原文的证据块，支持
  编辑/保存/发布/重新生成/历史版本回滚。没配模型或没 PDF 时页签禁用并引导。
- **真机**（Deep Residual, 12 页，deepseek-v4.1-flash）：12.5s、1771/1792 tokens、
  **$0.0027**、置信 0.78 → draft（未到 0.9 自动接受线）；摘要包含 58.8% mAP / 62.1% /
  ILSVRC 2015 等数字且都能落到证据块。截图 `docs/assets/ai/01-overview.png`。

### 12.10.2 图提取 AI 纠错（建议队列）

- **表**：迁移 **0007** `figure_proposal`（`proposed_json` / `confidence` / `status` /
  `mode`(vision|text) / provider/model/prompt_version / tokens / cost / decided_*）。
  状态：`pending | accepted | rejected | superseded | no_change`。
- **多模态能力升级**：`llm.py` 支持图片 content——内部统一
  `{"type":"image","media_type","data"}`，三种协议各自转换（Chat: `image_url` data URI /
  Anthropic: `image` base64 source / Responses: `input_image`）；新增单次 `max_tokens`
  覆盖、`usage.cost` 记录（OpenCode 网关回传真实费用）、单条消息 `timeout_s` 覆盖。
- **接口**：`POST /api/figures/{id}/ai-review`（对一张图跑一次）、
  `GET /api/figures/{id}/proposals`、`GET /api/proposals?status=&paper_id=`、
  `POST /api/proposals/{id}/decide`（`accept|reject`，返回更新后的图）。
- **三道闸（不让模型自由发挥）**：
  1. 图号建议**必须来自候选集合**：同页 + 阅读顺序前后 + 当前/邻图编号 ±1
     （实测「图 HM4」这种 OCR 错号，靠邻图 10-13/10-15 的 ±1 覆盖到真实图 10-14）；
     越界建议丢弃并在 `validation_notes` 写明原因；
  2. 图注只允许去噪：清洗后长度须在原值 55%–140%，否则丢弃；空字段不允许；
  3. `crop_issue` 只接受枚举（cut_off/too_large/too_small/not_figure/none/unknown）。
- **采纳语义**：建议**默认不自动改图**。采纳时：图号 → `label_raw/label_norm`、
  `label_source=review`、`label_confidence=1.0`；图注 → 直接替换；裁框问题 →
  `review_status=wrong` + `review_note`（**不自动改 bbox**，用户仍去原文拖框）。
  拒绝也留记录；同一张图只保留最新 pending（旧的置 `superseded`）；
  「检查后没问题」记 `no_change`，不占待审队列。
- **前端入口（三个）**：
  1. 图详情面板「AI 纠错：AI 检查这张图 / 重新检查」+ 内联建议（当前值 → 建议值，
     采纳 / 忽略）——`docs/assets/ai/03-figure-review.png`；
  2. 顶栏「**AI 建议 N**」徽标 → 抽屉式待审队列（缩略图、视觉/文字模式、置信度、
     逐条采纳/忽略、「采纳高置信（≥0.9）」批量）——`docs/assets/ai/02-proposals.png`；
  3. 文献图表页「**AI 修图（N）**」与待确认队列页「AI 复核这 N 张」——串行最多 8 张
     （每张一次请求，进度按钮实时显示 `done/total`，单张失败不拖累其它）——
     `docs/assets/ai/04-ai-fix-button.png`。
- **稳定性（真机踩出来的）**：
  * 推理模型（deepseek-v4.1-flash）会把 token 预算先花在 reasoning 上，`content` 为空：
    `llm.py` 给可读错误「预算被推理耗尽」，服务层自动**翻倍 max_tokens 重试一次**
    （超时也翻倍）；仍失败则**降级为纯文字判断**（图号/图注仍可用，裁框标 unknown），
    不让一张图把整批拖死；
  * 图纠错默认输出预算 `FIGURE_REVIEW_MAX_TOKENS=4000`、整篇速览 2400（均可在环境变量覆盖）。
- **真机验收（2026-11-12 晚，真实模型、真实课本图）**：
  * 视觉纠错单张 5–28s、**$0.0034–0.0045/张**（约 950 in / 1100–3600 out，推理模型）；
  * 实测抓到真实错误：图注「图 HM4 带传动时轴的作用力」→ 建议「图10-14 …」，
    采纳后卡片与引用数据立即更新（截图 04 里 p19 已是「图10-14」）；另有裁框 `too_large`
    （框里混进下方另一幅图）与 `not_figure`（解析器把正文块当图）的真实建议；
  * 测试 +21（整篇速览 5、图纠错 11、多模态协议 5）：全库 **221 passed**。
- **明确边界（诚实记录）**：没有自动接受器与 `audit_log`/`agent_task` 租约表（原 §4 计划）；
  裁框只标记不自动修正；表格结构复核、跨文献引用核对未做；M3「正式版」仍待后续。



---

## 12.11 工作台改版：知识库操作台 + 正文速览 + 侧边页签 + 宣纸/古墨双主题（2026-11-12 晚）

用户验收核心功能后，提出「布局/交互改版 + 全套美术重做」：左侧知识库要有专门操作界面（自由建组分组、
备注、收藏夹）；内容区在图表/速览/摘要之外加「文献正文速览」；四个页签挪到内容区侧边；两个侧栏可收起；
整体做减法；配色按「现代极简中式纸墨书卷风」重做，亮色（宣纸书卷）+ 暗色（古墨简卷）跟随系统。

### 12.11.1 后端（小改）

- 迁移 **0008_paper_workspace**：paper.note（用户备注，Text，可空）/ paper.favorite（布尔，默认 false）。
  备注不进检索/AI prompt；收藏是虚拟视图，不新开集合表。
- PATCH /api/papers/{id}：只改 
ote / favorite / reading_status，Zotero 镜像元数据仍只读。
- GET /api/papers?favorite=true|false：收藏夹过滤。
- GET /api/papers/{id}/text：把最新版本的正文块按章节组织成只读正文流（lock_id/page/type/text/bbox），
  供「正文速览」对照原文；没有解析版本返回 404。
- PaperOut 增 
ote / favorite / collection_ids（知识库操作台显示分组勾选）。
- 测试：+4（备注/收藏 PATCH、收藏筛选、正文分段、无版本 404）；全库 **225 passed**。

### 12.11.2 前端布局/交互

- **知识库操作台**（侧栏「知识库 → 管理」进入，占内容区）：
  - 左栏分组树：全部 / ★收藏夹 / 手动分组与子组；选中手动分组可改名、删除（删组不删文献、子组上提）；
    选中分组点「＋」建子组；Zotero 镜像分组只读。
  - 右栏文献列表：搜索标题/备注/期刊；每行 ★收藏、所属分组 chips（点击加入/移出，多对多）、
    备注行内编辑保存、标签、打开文献。
- **收藏夹**：侧栏虚拟视图 + 文献行星标；收藏计数实时更新；空态提示去点星标。
- **四个页签挪到内容区左侧纵栏**：图表 / AI 速览 / 章节摘要 / 正文速览；纵栏在左、内容在右，
  未配置模型或无 PDF 的页签置灰并提示。
- **新增「正文速览」**：左栏章节大纲（标题 + 页码），右栏按节摊开正文段落（宋体、宽行距）；
  点任意段落打开原文对应页并高亮 bbox；标题/图注/正文分样式。只读、不调模型。
- **两个侧栏可收起**：知识库树、文献列表各自一个收起按钮，状态存 localStorage；收起后只剩细头部。
- **做减法**：顶栏统计合并为「17 篇 · 393 图」、检索模式 chip 只在有查询时出现、提示语收敛到需要处、
  品牌字由符号改为「墨」。

### 12.11.3 纸墨双主题（CSS 变量 + prefers-color-scheme）

- styles.css 重写为变量驱动：固定色卡 --bg-page/--bg-card/--bg-hover/--bg-active/--text-*/--border-line/
  --accent/--accent-hover/--shadow，暗色在 @media (prefers-color-scheme: dark) 整组替换；
  旧变量名（--bg/--panel/--ink/--muted/--line/--good/--warn/--bad）保留为别名，旧选择器全部沿用。
- 组件按规范：卡片 8px + 1px 细边框 + 墨晕阴影，hover 只换底色（不放大/上浮/发光）；
  主按钮 accent 实底白字、次按钮透明 + 细边框；输入框聚焦 accent 边框、无蓝色 focus ring；
  表格表头 card 底 + 隔行淡底；滚动条 8px 窄条、无蓝轨道；进度条/任务条 accent 填充；
  遮罩层用低透明墨色、去掉 glassmorphism blur。
- 字体：正文/标题宋体栈（Source Han Serif → Noto Serif CJK → 宋体/SimSun），UI 小字思源黑体栈，
  英文/数字/编号 Inter + tabular-nums；代码用等宽栈。
- 宣纸肌理：顶栏「纸」按钮切换（localStorage 记忆），低强度 SVG feTurbulence 叠加 + multiply；
  暗色模式自动不显示纸噪。
- **亮/暗一键切换（2026-11-12 补）**：顶栏「◐/☀/☾」三态按钮——自动（跟随系统）→ 宣纸亮色 →
  古墨暗色 → 循环；选择存 `localStorage['litbench.theme']`。实现方式：`:root[data-theme="light|dark"]`
  提供完整变量组（特异性高于 `prefers-color-scheme` 媒体查询），`auto` 时移除属性走系统媒体查询；
  暗色的纸纹/图片底特例也补了 data-theme 版本，系统暗色 + 手动亮色同样成立。
- 图标沿用原字符/SVG 语义，仅统一为细线、跟随文字色；未替换任何图标含义。

### 12.11.4 验证

- pytest **225 passed**；	sc --noEmit && vite build 通过（CSS 53.5 kB）。
- CDP 亮/暗截图 9 张：docs/assets/workspace/01-light-home / 02-light-paper-rail / 03-light-reader /
  04-light-kb-manager / 05-light-collapsed / 06-dark-home / 07-dark-paper-rail / 08-dark-reader /
  09-dark-kb-manager.png。
- UI 功能验收（headless Edge 真实点击）：备注编辑保存 toast「备注已保存」✓；星标切换 ✓；
  收藏夹过滤 ✓；收起状态刷新后保持（localStorage）✓。
- dev 库演示数据：收藏 2 篇（Attention / Deep Residual）、Attention 有备注、新增手动分组「精读候选」。
- 边界：主题三态（自动跟随系统 / 宣纸 / 古墨，手动按钮）；分组不做拖拽排序；备注不参与检索/AI；Zotero 镜像分组只读；
  纸纹在暗色强制关闭。

---

## 12.12 工作台交互修订第二轮（2026-11-12 晚，按用户验收反馈）

用户验收后提出 4 个交互问题，逐条修正：

1. **「全部文献」先看到文献，而不是零碎图片**：非文献视图默认 `browseMode='papers'`，展示文献卡片；
   内容区右上新增「文献 / 图表」切换（`view-toggle`），图表视图保留全局图片浏览；对全部文献 /
   收藏夹 / 知识树某集合都生效。图表检索接口增 `favorite=true` 与 `collection_id=`（含子集合）
   过滤，收藏夹/知识树看到的图只来自对应文献，不再借用全局结果。
2. **收藏夹为空时留空**：`favoriteOnly && papers.length===0` 只显示「收藏夹还是空的」空态；
   图表模式也是收藏夹专用空态，不串其它栏内容。
3. **两栏折叠改为水平收起**：左栏拆成「知识库列（216px）+ 文献列（244px）」，各自 ◀ 收起成
   **34px 竖条**（竖排文字 + 计数），内容区获得释放宽度（1440 屏实测 980px → 1372px）。
4. **亮色下图号/图集图标饱和度提高**：新增 `--accent-mark`（亮色 `#B2432B`，暗色 `#D8704F`），
   只用于卡片「单图/图集」图标与图集角标；实测计算色 `rgb(178, 67, 43)`。主强调 `--accent`
   保持低饱和，不大面积使用。

顺带：文献卡片升级为「标题 + 年份/期刊 + 摘要（无摘要显式标注）+ 标签 + 备注 + 图/表数 +
仅元数据/收藏标记 + 来源徽标」，星标独立按钮（不嵌套卡片按钮）。

**验证**：`pytest` **226 passed**（+1：图表列表按 favorite/collection 过滤）；前端 build 通过；
CDP 实测：默认 17 张文献卡、切图表、收藏夹空态只显示空态、收藏后收藏夹仅 1 篇且图表仅其 1 张、
两列收起宽度各 34px、图标色 `rgb(178,67,43)`。截图 `docs/assets/workspace/20…28-*.png`。

**边界**：实现的是「文献优先 + 顶部切换」，没有做左右同时并排的固定分栏；如需并排可再调。

---

## 12.13 M2b 收尾：章节摘要真实模型全量 + 置信度落库 + 阈值校准 + UI 点击验收（2026-11-12 深夜）

**目标**：M2a 的章节摘要一直只有 mock 数据（曾生成 66 条，已清理），真实模型下 prompt 质量、
证据绑定率、失败率、tokens/费用、自动接受阈值都还是空白。这一步拿 1 本真实教材
（02-传热学第 7 章，46 页 / 69 个章节节点）跑完整链路，并把「真实模型才知道的问题」修掉。

### 12.13.1 试跑先暴露根因：不是 prompt，是输出预算

先挑 6 个有代表性的章节（章标题 / L3 / L4 短 / L4 长 / L3 / 复习题）试跑，**3 失败**：

| 失败 | 现象 |
| --- | --- |
| 1 条 | `模型只输出了推理过程、没有正文（输出预算被推理耗尽）` |
| 2 条 | 返回了文本但 JSON 被截断 → `模型输出不是 JSON 对象` |

原因：`generate_summaries` 没显式传 `max_tokens`，用了全局默认 **1200**；`deepseek-v4.1-flash`
是推理模型，reasoning 先吃掉预算，正文就没地方了。整篇速览/图纠错早已踩过同一个坑并分别给了
2400 / 4000，章节摘要一直漏了。**prompt 本身在真实模型下表现良好（低置信度会主动承认证据不足），
这次没有改 prompt**（`prompt_version` 仍为 `48219a77b7f8`）。

### 12.13.2 代码改动

1. **输出预算 + 重试**：新增 `settings.section_summary_max_tokens = 3000`；
   `generate_summaries` 改走 `complete_with_budget_retry`（推理耗尽时自动翻倍预算重试一次）。
2. **失败也记账**：模型返回了内容但没过 JSON/证据校验的调用，也把 tokens/cost 写进本次结果；
   生成接口新增 `tokens_in_total / tokens_out_total / cost_total`，每条 result 带
   `confidence / tokens_in / tokens_out / cost`。失败错误里带模型原始输出前 200 字，方便判断
   是「截断」还是「没按 JSON 输出」。
3. **置信度落库**（阈值校准的前提）：`summary.confidence` + `summary_revision.confidence`
   （迁移 `0009_summary_confidence`，可空）；创建/重生成/编辑快照/回滚都带上；整篇速览同样落库。
   API 的 `SummaryOut`、revision、详情都回传 `confidence`。
4. **前端**：章节摘要与整篇速览详情显示「置信度 0.XX」标签；「生成全部章节」完成 toast 显示
   本次 tokens 与花费（如 `token 53297/65839 · 花费 $0.0893`）。
5. **顺带修 UI 文案 bug**（验收中发现）：原来提示「人工编辑过的摘要不会被覆盖」，但每节的
   「重新生成」走 `force=true`，实际会覆盖（覆盖前留 revision）。已改成一致的说法：
   「会覆盖当前摘要（包括人工编辑过的）；旧内容会先留一条历史版本，可在『历史版本』里回滚」；
   「生成全部章节」才是会跳过人工锁定的入口。

### 12.13.3 全量真机结果（02-传热学第 7 章，69 个章节节点）

| 指标 | 数值 |
| --- | ---: |
| 章节节点 / 请求 / 实际生成 | 69 / 69 / **60**（54 新建 + 6 重跑，9 个纯分组标题无正文跳过） |
| 失败 | **0**（预算修好后；同批 500.8s 跑完） |
| tokens | 53,297 in + 65,839 out |
| 费用 | **$0.0893**（约 ¥0.64；平均 $0.00149/节） |
| 置信度 | 0.38–0.95，均值 **0.831**；分布：0.3×1 / 0.5×2 / 0.6×2 / 0.7×6 / 0.8×32 / 0.9×17 |
| 自动接受（阈值 0.9） | **17 条 published**；43 条 draft 留给人工 |
| 证据绑定 | 60/60 条都有非空证据（1 条 22 条、2 条 3 条、3 条 7 条…最多 16 条） |

**质量抽检**：低置信度确实是证据不足的章节，模型会明确写「证据未给出三个区域名称/未给最终结果」
而不是编；高置信度（0.9）是 L1/L3 核心节（凝结/沸腾/热管/Stefan 问题），公式号、数字、物性
都来自证据块。0.8–0.88 档多数是 L4/L5 小标题节，常只有 1 条证据块、正文覆盖不全。

### 12.13.4 阈值校准结论：**保留 0.9，不降**

- 0.9 档 17 条抽检全部合格，是「读起来能直接当正文摘要」的核心章节；
- 若降到 0.86，会多放行约 19 条小标题节，其中不少只有 1 条证据块——正是最需要人看一眼的那批；
- 置信度分布在真实模型下区分度可用（0.38 的分区证据缺失 vs 0.95 的完整结论），说明让模型
  自评 + 服务端阈值是有效的；**结论：`summary_auto_accept_confidence` 保持 0.9**。

### 12.13.5 UI 点击验收（CDP，截图 `docs/assets/m2b/01…09-*.png`）

真实数据下走了一遍：章节树 69 项（已发布 17 / 草稿 43 / 分组 9）→ 点开发布摘要看「置信度 0.90 +
证据 7 条」→ 选草稿（置信度 0.65）点「发布」→ 变已发布 → 「编辑」改正文 → 保存后变「人工编辑 /
已锁定」→ 打开「历史版本」→「回滚到此版本」内容回退 →「生成全部章节」的跳过语义与文案一致。
验收用的那条草稿在测试后已用备份恢复（dev 库里不留测试痕迹）。

**验证**：`pytest` **228 passed**（+2：置信度/费用落库、章节预算与推理耗尽重试）；前端 build 通过；
迁移一致性测试覆盖 0009。

### 12.13.6 边界与遗留

- 生成仍是**同步长请求**（60 节约 8.3 分钟占着 HTTP 连接）；60 节以上应改为 job 化，
  这次先不扩（个人库规模可接受，且前端已有「生成中…」）。
- 没拿到响应的失败（超时/连不上）无法计价，费用统计口径是「拿到响应的调用」。
- 没有结果缓存（同一节重复生成会重复花钱）；`M2b` 规划里的缓存留到 M3 一起做。
- 未改 prompt，因此也没有 A/B prompt 版本；若后续要提摘要密度/要点数，先加 prompt 版本对比再换。

**下一步**：M4 MCP 只读出口（`mcp/` 薄层、read 工具、token scope/审计、DSH 跑通「查证据→回答」）。

---

## 12.14 全项目代码审查 + 修复批 A/B/C（2026-11-13 凌晨）

用户要求对整体项目做一次代码审查（接口虚接 / 未实现功能 / 冗余残留 / 写法 / 隐藏 bug）。
审查结论与完整清单见当次会话；这里只记录**已修的三批**，以及修完后的行为和边界。

### 12.14.1 批 A：部署链（不修的话 Docker 全栈根本起不来）

| 问题 | 修法 |
| --- | --- |
| Alembic 链断：0004 `down_revision="0003_label_quality"`，0003 实际 revision 是 `"0003"` | 改 0004 的 down_revision 为 `"0003"`；`test_migration_matches_models.py` 新增修订链校验（父节点都存在、唯一根、唯一 head、无环、所有文件在链上），不靠文件名排序 |
| compose 的 `DATABASE_URL` 是同步 `postgresql://`，代码用 `create_async_engine` + 只装 asyncpg | 改 `postgresql+asyncpg://` |
| 部署路径从不执行迁移（镜像 CMD 直接 uvicorn，main lifespan 不建表） | 新增 `server/docker-entrypoint.sh`：`RUN_MIGRATIONS=1` 时先 `alembic upgrade head` 再 `exec "$@"`；api 设 1，worker 设 0 且 `depends_on: api: service_healthy`（避免并发迁移）；web 同样等 api |
| `.env` 只参与 compose 插值、不进容器 | `x-app-env` 显式注入 17 个 LLM/OCR/升级/上传/CORS 配置（默认值与 `config.py` 一致；bool/int/float 不给空字符串），新增 `test_deploy_config.py` 做静态一致性守卫 |
| api healthcheck 打 `/health`（不查库），worker 继承同一探针但没监听 8000 | 改打 `/api/ready`（查一次库）；compose 给 worker `healthcheck: { disable: true }` |
| 没有 `.dockerignore`；默认镜像无条件装了 docling，与 `INSTALL_DOCLING` 分层矛盾 | 新增根 `.dockerignore`（排除 .git/.venv/.data/node_modules/dist/docs/assets 等）；`docling` 从 `requirements.txt` / `pyproject.toml` 默认依赖删掉，只在 `requirements-docling.txt` 里由 build arg 安装 |

**边界**：本机没有 Docker、也没有 alembic（未安装），所以镜像构建、`docker compose config/up`、
entrypoint 实际跑迁移、`/api/ready` 健康判定都**没有实跑**；只做了静态断言 + YAML 解析 +
pydantic 默认值实解析。在真实 PG 上跑 0001–0009 仍待下次有环境时验证。

### 12.14.2 批 B：重解析不再让 AI 产物悬空 + SQLite 外键 + golden 过滤

**问题**：`workers/run.py` 的 `persist` 重解析时先物理删掉该版本全部 Block/Figure 再插入新 UUID；
`summary.section_key` / `evidence_block_ids_json` / `figure_proposal.figure_id` 全绑旧 id。重复上传同一
PDF、按需升级重解析之后，摘要证据点不开、图纠错提案指向不存在的图。

**修法**：新增 `server/litbench/reparse.py`：

- `snapshot_ai_products()`：删旧行前把摘要/历史版本/图纠错提案快照成纯 dict，并记录每条证据的
  `(page, 归一化文本)` 与提案图的 `cite_key`；同时显式删除该版本的 `figure_proposal`（不依赖 CASCADE 时序）。
- `reconcile_ai_products()`：新 Block/Figure 插入并 flush 后——证据按 `(page, 归一化文本)` 重映射；
  章节先按 `(标题, level)` 匹配，匹配不到按「证据重叠最多的新章节」兜底；提案按 `cite_key` 找新图后
  以新 `figure_id` 重建（沿用 status/decided_*/tokens 等字段与 created_at）。`__paper__` 整篇速览只迁证据、不动键。
- 人工内容永不删：证据映射不上时 `human_locked`/`human_edited` 摘要保留（证据写空），
  agent 摘要证据全失配才整条删除（连同历史）；提案对应图号消失则跳过并 warning。
- `db.py` 的 SQLite connect 事件补 `PRAGMA foreign_keys=ON`（此前 CASCADE 在 dev 全是摆设）。
- `golden.py` 的导出加 `Figure.deleted_at IS NULL`（软删除的图不再进 golden）。

**真机验证**：把 Attention（pdfium）重解析后，它的整篇速览与「第 1 页」章节摘要都自动重锚到新
section/证据上（`valid=3/3`），页面/图数不变；新测试 `test_reparse_ai_products.py` 覆盖同内容重解析、
证据全失配 drop、human 保留、`__paper__` 例外、FK pragma。

**边界**：dev 库已有的 22,888 个孤儿 block 不会被 FK 开关自动清掉（只对之后的操作生效，后续单独清理）；
从 pdfium 升级到 docling/MinerU 时 block 粒度变化会导致 agent 摘要被删（可在新解析上重新生成）、
人工摘要保留但证据可能为空；Postgres 下未实测（代码无 SQLite 特有 SQL）。

### 12.14.3 批 C：pdfium 坐标 / 阅读顺序 / 证据预算

**问题**：pypdfium2 的 charbox 是 PDF 原生左下原点（y 向上），但 block bbox 原样落库、阅读顺序按
`y0` 升序 → 整页段落倒序（第一条是页脚）、证据 bbox 在 PDF.js 里垂直镜像，且与 figure 的左上原点
bbox 混在一列；标题/页脚判据也是按左上原点写的，语义正好反。

**修法**（`adapters/pdfium_parser.py`）：

- 内部几何（图框生长 / 扫描页空白带 / caption 合并）**继续用原生左下原点**算，出口处统一
  `_to_top_left()` 后再交给上层（`adapters/base.py` 契约）。
- `_reading_order` 改为原生坐标下按行上边 `y1` 降序（自上而下），双栏插回判据同步改。
- 标题/页脚判据按原生坐标修正：页脚 `y1 < 15% 页高`、标题 `y1 > 65% 页高`。
- 章节摘要证据块上限 `SUMMARY_MAX_BLOCKS` 40→300（字符预算仍是主约束）：pdfium 是行级块，
  4 页窗口 200+ 条，旧上限只送进不到一页正文。

**真机验证**：9 篇 arXiv + Attention（共 10 个 pdfium 版本）已用新代码重解析：page1 第一个 block
从「页脚邮箱」变成论文标题，所有 bbox 在页面内且 `y0<y1`，正文速览首段从标题开始（截图
`docs/assets/fixes/01-pdfium-reader-topdown.png`）；新增 `test_pdfium_parser.py` 断言 bbox 左上原点、
阅读顺序自上而下、行级块能填满字符预算；`test_paper_pdf` / `test_pipeline` 等 26 个解析相关测试全过。

**边界**：双栏近似算法本身仍是启发式（短行/公式标签会和跨栏块轻微交错）；这次只把「方向反了」
修正，不重写分栏算法；已有解析数据必须重解析才生效（已做）。

### 12.14.4 顺手修复：重复上传时 `os.replace` 在 Windows 上 500

用同一 PDF 触发重解析时，内容寻址的目标文件已存在，`_stream_to_disk` 仍 `tmp.replace(final)`；
Windows 上目标若被查看器/杀毒短暂占用会抛 `PermissionError`（实测重解析批量上传时复现）。
现在：**目标文件已存在且大小一致直接复用**（内容寻址，内容必然相同），只在大小不一致时才尝试覆盖，
覆盖失败给 409 可读错误；新增回归测试 `test_reupload_uses_existing_content_addressed_file_when_locked`。

### 12.14.5 验证与提交

- `pytest`：**251 passed**（+23：A 的修订链/部署静态断言、B 的重解析/golden/FK、C 的 pdfium 坐标与证据预算、上传去重锁定）。
- 前端 `tsc --noEmit` 不受影响（本批未改前端）；API/worker 已用新代码重启，`/api/ready` 正常。
- dev 库：papers 17 / versions 16 / blocks 34,576 / figures 392 / summaries 63 / proposals 6，全部摘要证据有效。
- 未跑：Docker 构建与 compose 起栈、真实 PG 迁移、PostgreSQL 上的重解析并发（本机无 Docker/alembic/PG）。

**下一步**：审查里剩余的批 D/E/F——前端三个竞态与详情面板状态、接口语义（`paper` 过滤、citekey 搜索、
真实 total/offset、published 锁定）、CI 与清理（pytest/tsc job、README/docs 过期、死配置）。

---

## 12.15 审查修复批 D/E/F（2026-11-13 凌晨，续）

批 A/B/C 见 §12.14；本批修完审查里剩下的前端、接口语义与清理。**A–F 至此全部完成。**

### 12.15.1 批 D：前端竞态 / 详情状态 / 收藏夹 / 错误与时间 / 分页

- **请求竞态**：`openPaper` 用自增请求号，切文献先清空 `paperFigs`，旧请求（成功/失败/loading）一律丢弃；
  搜索/AI 单图请求同样校验发起时的目标，避免「A 的结果套在 B 上」。
- **详情面板**：`<Detail key={detail.figure_id}>`，切图重置图号草稿/审核状态；`refreshKey` 改为**静默刷新**
  （只更新 paper/figures/详情数据），不再 `openPaper()` 重置 `paperTab` 或关闭详情——点「定位正确/保存图号」
  后详情留在原地、页签不被踢回图表。
- **收藏夹**：取消收藏立即从收藏夹列表移除、计数重新拉取；知识库操作台改收藏后计数同步。
- **错误与资源**：422 的 Pydantic `detail` 数组解析成「字段: 原因」；`testLlmSettings` 无参不再发 `{}`；
  `addPaperToCollection` 字段改为后端实际返回的 `added`；localStorage 读写包 try/catch + 顶层 ErrorBoundary。
- **保存语义**：摘要 PATCH 成功但刷新详情失败时提示「已保存…刷新详情失败」，不再误报保存失败；输入加
  maxLength（摘要 8000 / 要点 ≤8 / 图注 2000 / 图号 200 / 备注 20000）。
- **时间与分页**：新增 `format.ts`，用 `Intl.DateTimeFormat('zh-CN')` 按本地时区显示；图表列表标题用真实
  `total`、底部「已显示 X / N 张 + 加载更多（+60）」。
- **PdfViewer**：`renderTaskRef` 取消上一个渲染任务，快速翻页不再把查看器打进错误态；error 态有重试；
  `page` prop 变化同步页码；只读证据查看器不再显示「在页面上拖框可修正」。

### 12.15.2 批 E：接口语义

- `GET /api/figures?paper=` 解析 paper_id / cite_key / DOI / arXiv（裸 id 不再走到 ILIKE 得 0）；
  `q` 与 `paper` 分开；`offset` 参数 + `total` 为真实匹配数（limit/offset 前计数）；非法 `review`/`figure_type` 422。
- `GET /api/papers?q=` 同时搜 title 与 cite_key；collection 过滤改为 `paper_id` 子查询去重
  （避免整行 DISTINCT 遇到 Postgres JSON 列）。
- figures/tables 接口与 `figure_count/table_count` 统一只反映**最新版本**。
- `publish` 置 `human_locked=true`（发布=定稿）；整篇速览在已发布/锁定时无 `force` 返回可读 422，force 仍可覆盖并留 revision。
- 所有响应 datetime 加 `UTCDateTime` 处理（naive→UTC，输出带 `Z`），前端按本地时区显示不再差 8 小时。
- LIKE 查询统一 escape `\ % _`；`/papers/{id}/figures` 补 `response_model`。

### 12.15.3 批 F：清理与 CI

- **死配置接线**：`text_quality_min` 传入 `needs_text_layer_audit`（原先硬编码 0.8）；`docling_ocr_lang` 从
  `get_parser` 传给 `DoclingParser`（原先永远默认 chinese）。
- **删除空转设施**：`valkey_url` + compose 的 valkey 服务 + `VALKEY_URL` + `WORKER_CONCURRENCY`（队列实际是 DB job 表，
  并发配置无人读取）；`pgvector` 依赖与 compose 的 pgvector 镜像改为 `postgres:17-alpine`。
- **seed_dev 安全化**：只允许写 `.data/dev.db`（拒绝其它 `--db`/环境变量残留），按外键依赖顺序清空全部 12 张表，
  年份 2611→2026；新增 `scripts/gc_orphans.py`（默认 dry-run，`--apply` 才删）并实际清掉 dev 库
  **22,888 孤儿 block + 7 孤儿 tab**（旧 seed 只清 4 张表留下的）。
- **CI**：修正 `license.yml` 里 job 级 `hashFiles`（GitHub Actions 不支持，移除外层 if）；新增 `ci.yml`：
  后端 `pytest`（SQLite，无外部服务）+ 前端 `npm ci && npm run build`（含 `tsc --noEmit`）。
- **文档**：README 状态/测试数/技术栈（未实现的 S3/Valkey/pgvector/citeproc 标注规划中）、seed/worker 命令纠正；
  `docs/04`、`docs/07` 加“历史快照”横幅。
- **CSS**：删除确认无 JSX 使用的死选择器（`.stats/.hintline/.paper-tabs/.ptab/.settings-note/.settings-security/
  .proto-hint/.kb-block/.papers-block/.source-badge.mock/.cite-key/.exception-tip` 与 `.flag.*` 未用变体），
  修复 `--bg-primary` 未定义、`km-group.on` 少点号；主题 token 三份重复与整体重排**未动**（风险高于收益）。

### 12.15.4 验证

- `pytest`：**267 passed**（+16：E 的 10 个接口语义测试、F 的 6 个清理/护栏测试）。
- 前端：`npm run build`（tsc strict + vite）通过。
- 真机 UI（CDP，截图 `docs/assets/fixes/02…08-*.png`）：图表分页 60→120/392；快速切文献只显示后点的一篇；
  详情切图后草稿重置；查看器快速翻页无渲染错误；只读证据查看器无拖框提示；设置页时间本地化；
  收藏夹取消收藏即消失且计数归零、随后恢复；详情面板在「定位正确」触发刷新后保持打开且页签停在章节摘要。
- 真机 API（只读）：`/figures?paper=<id>` 68 张（原 0）、`paper=<citekey>` 可用、`q=<citekey>` 命中、
  `limit=3` 时 total=392 且 offset 不重叠、时间带 `Z`、非法枚举 422、按文献计数与接口一致。
- dev 数据：papers 17 / versions 16 / blocks **11,688**（清孤儿后）/ figures 392 / summaries 63 / orphan=0；
  收藏 1、摘要证据全部有效。

**边界**：本机仍无 Docker/PostgreSQL，Docker 构建、compose 起栈、真实 PG 迁移与并发未实跑（E 的 SQL 已用
Postgres 方言编译核对，规避了 JSON 整行 DISTINCT）；GitHub CI 未实跑；`mcp/`、`templates/`、`schemas/` 仍是 M4/M5 规划；
重复实现（`_evidence_lines`/`_snapshot`/IoU 三份等）与 CSS 主题 token 重复**未合并**，属纯重构、风险高于收益，留待后续。

**下一步**：M4 MCP 只读出口；M3 正式化（自动接受器 / audit_log / 任务租约）；表格进界面、待确认队列清理。

---

## 12.16 清库与当前交接（2026-11-13 中午）

- **审查修复 A–F 已完成**（§12.14/§12.15），测试 **267 passed**，前端 build 通过；HEAD 以 `git log -1` 为准。
- **2026-11-13 01:48 清空 dev 库与文件**：papers / paper_version / block / figure / table / summary /
  summary_revision / figure_proposal / job / collection / paper_collection / import_run 全部 0；
  `.data/pdfs` 与 `.data/images` 清空；`.data/mineru-work` 旧 OCR 临时产物清空；`.data/golden.json` 删除。
- **备份**：`.data/backups/pre-clean-20261113-014748/`（旧 dev.db + 21 PDF + 817 缩略图，166.6 MB / 840 文件），
  确认新样本可用后可删；`.data/samples`（13 文件 / 56.7 MB）、`.data/real-corpus`（5 文件 / 34.8 MB）、
  `.data/mineru-home`（模型）、`.data/llm-config.json` 保留；**Zotero 本机库未动**。
- **服务状态**：API 8000、preview 4173、worker 当前均未运行；启动命令见 `docs/09` §16（API/worker 指向
  `.data/dev.db`，`STORAGE_DIR=.data`，`PYTHONPATH=server`；前端 build 后 preview 4173）。
- **下一步**：导入新文献样本并复跑「解析 → 图号校核 → 图表检索 → AI 摘要/图纠错 → UI 验收」；
  然后 **M4 MCP 只读出口**；之后 M3 正式化、表格进界面、待确认队列清理（清库后队列为 0，新样本导入后重新累积）；
  M5 写作侧仍为空规划。
- **环境**：本机无 Docker/PostgreSQL，Docker compose 与真实 PG 迁移仍未实跑；CI 已加 pytest+前端 build；
  截图验收用 headless Edge + CDP（`.data/tmp/cdp.py`）。

---

## 12.17 新测试语料（10 篇热/流体）+ M4 只读 MCP 出口（2026-11-13 下午）

### 12.17.1 选语料与导入

- 用户要求：自己找 **10 篇「热与流体」方向的英文论文**放进库做测试，然后开跑 M4。
- 来源 arXiv（开放获取）。按 10 个主题抓搜索页候选（heat transfer / nanofluid / convective /
  microchannel / pool boiling / heat exchanger / turbulent / natural convection /
  lattice Boltzmann / electronic cooling），去重后 417 条，人工挑 10 篇（实验 / 数值 / 综述混合）：

  | arXiv | 方向 | 页 | 图 | 表 |
  | --- | --- | ---: | ---: | ---: |
  | 2611.14297 | 高超声速空腔换热实验 | 22 | 12 | 0 |
  | 2610.31151 | 纳米流体膜态沸腾（颗粒形状） | 45 | 10 | 4 |
  | 2609.12344 | 液冷板对流换热优化（场协同/分形） | 48 | 18 | 8 |
  | 2609.22621 | 多孔格栅湍流强制对流摩擦/换热建模 | 25 | 16 | 3 |
  | 2608.29240 | 流动沸腾局部干涸→汽结构转变 | 36 | 21 | 0 |
  | 2603.09607 | 微通道旋流/滑移壁强化传热 | 15 | 11 | 0 |
  | 2606.13505 | Rayleigh–Darcy 对流的 ultimate regime | 24 | 9 | 0 |
  | 2512.17876 | 核态池沸腾多尺度自动机（加热器物性） | 28 | 11 | 0 |
  | 2608.26811 | 芯片热设计模拟/测量综述 | 45 | 22 | 4 |
  | 2402.09931 | 微通道热沉建模考量 | 14 | 10 | 0 |

- 上传：`POST /api/papers`（multipart + title/arxiv_id/year，sha256 去重），10 个 PDF 共 **65.0 MB**；
  建集合「热与流体 · 测试样本 2026-11」。
- 解析：**10/10 parsed**，**5,695 block / 140 图 / 19 表**。pdfium 先解析，5 篇因「图注平均字数低 /
  图号未锚定」自动升级 Docling（单篇 40–50 s）；19 张表全部来自 Docling。
- 整篇速览 2 篇（2608.26811 综述、2610.31151 模型）：**$0.0052**。注意：直接上传 PDF 的
  `paper.abstract` 为空，整篇速览的输入只有标题 + 章节大纲 + 14 个证据块（模型在输出里如实写了
  「摘要片段被截断」）——这是既有输入预算限制，不是这次引入的 bug；走 Zotero 导入会有摘要。
- 图号待确认 4 张（unlabeled 4 / low_confidence 4 / conflict 0）；表格 15 张结构化 HTML、
  4 张只有图片（Docling 定位到表区域但 tableformer 没抽出 grid）。

### 12.17.2 联调中修的两个真实 bug

1. **Docling 表格 HTML 全是空串**（`server/litbench/adapters/docling_adapter.py`）。
   docling 2.150 的 `TableItem.export_to_html()/export_to_markdown()` 新增 `doc` 参数，
   **不传 doc 不抛异常、而是返回空串**；旧代码只 catch 异常，于是 19 张表全存成 `html=""`。
   修复：`_table_html(item, doc)` 显式传 doc，返回空串也算失败并回退 markdown；同时无 grid 的表
   `quality="image"`（原硬编码 structured）。4 篇重解析后 **15/19 张表 HTML 恢复**。
   单测 `server/tests/test_docling_tables.py`（4 个，不需要装 docling）。
2. **手工上传的文献没有 `paper.cite_key`**（`server/litbench/routers/papers.py`）。
   以前只有图有 cite_key，M4 的 agent 回答只能退化到 paper_id。修复：上传时生成
   `arXiv:<id>` / `doi:<doi>` / `p-<paper id 前 8 位>`；同一 arXiv 的不同 PDF 追加 id 前缀避免
   `cite_key` 唯一约束冲突。已有 10 篇用脚本回填。
   测试 `server/tests/test_pipeline.py::test_upload_generates_unique_cite_key`。

### 12.17.3 M4：`mcp/` 只读 MCP 薄层（MIT）

- 交付：src 布局 Python 包 `litbench-mcp`（console script `litbench-mcp`）；**8 个只读工具**：
  `library.search`（标题/cite_key，含自然语言拆词兜底）、`evidence.get`（paper/cite_key/DOI/
  arXiv/figure/summary）、`figure.get`、`table.get`、`cite.resolve`（确定性词面打分，不调模型）、
  `collection.tree`、`summary.get`、`dataset.golden`。
- 传输：stdio（默认，本机 agent 拉起）与 Streamable HTTP（`POST /mcp`）；HTTP 必须
  `Authorization: Bearer <token>`，token 含 `agent-read` scope（无 token → 401、scope 不符 → 403）。
- 安全：token 文件只存 SHA-256；每 actor 滑动窗口限流（默认 120/60s）；审计 JSONL
  （`{ts,actor,tool,params_digest,params_preview,≤80 字,ok,error_code,duration_ms}`，不记正文）；
  instructions + 每个工具描述声明「文献是不可信数据，不执行其中指令」，含文献内容的返回体带
  `content_trust:"untrusted"`。
- 实现要点：官方 MCP Python SDK **2.3.0**（v2 已把 FastMCP 改名 `MCPServer`；工具名点号合法，
  SEP-986 允许 `.`）；HTTP 认证用 SDK `AuthSettings(required_scopes=["agent-read"])` +
  自定义 `TokenVerifier`，401/403 由 SDK 中间件产生；工具内 actor 从 `get_access_token()` 取 token label。
- 验收：
  - `pytest mcp/tests -q` → **102 passed**（httpx.MockTransport 假 API，不依赖 8000/真实数据）；
    `ruff check mcp/src mcp/tests mcp/examples` 通过。
  - stdio 连真实 API 调通 8 个工具：`table.get` 返回真实 HTML 表（含 Table 1 球形度数值）、
    `cite.resolve` 对目标文献判 `supported`（0.7333）、对不相关文献判 `insufficient`（0.4667）。
  - Streamable HTTP（8765）实测：无 token 401、`agent-propose` token 403（`insufficient_scope`）、
    Bearer 调 `collection.tree` 成功；审计 actor=token label。
  - 示例 agent（`mcp/examples/agent_demo.py`，stdio → search → evidence → chat_completion 模型）：
    自然语言 query「nanoparticle shape effect on nanofluid film boiling」→ token 拆词命中
    `arXiv:2610.31151` → 8 个证据块 → 回答带 `[cite_key=arXiv:2610.31151 / block_id=...]`。
- 自然语言 query 的坑：服务端 `GET /api/papers?q=` 只做 LIKE 子串匹配，整句几乎必然 0 命中；
  M4 壳层先按关键词拆（最多 6 个 token）再搜，仍空才降级图注检索（`degraded=true`）。
  这是薄层职责，服务端契约不变；代价是长 query 多几次 GET（本地可忽略）。
- 边界：只读（propose/task/写库留给 M5）；`cite.resolve` 是词面启发式，同义改写/跨语言会判
  insufficient；HTTP 是单进程内存会话，不适合公网多副本；stdio 默认不校验 token（本机进程）。


## 12.18 M3 正式化：审计 / 自动接受器 / 回滚 / agent 任务租约（2026-11-13 下午）

### 12.18.1 交付

- **迁移 `0010_m3_governance`**
  - `audit_log`：actor/action/target_type/target_id/before_json/after_json/evidence_json/note；
    正式字段写操作全部留痕（不记文献正文，长文本只留 sha256+长度）；
  - `agent_task`：kind/target/payload/status/priority/idempotency_key/attempts/max_attempts/
    actor/lease_until/heartbeat_at/result/error/cost；支持 claim / heartbeat / retry / 过期回收；
  - `figure_proposal` 补 `before_json`（采纳前快照，回滚用）、`auto_accepted`、`rolled_back_at/by`；
  - `figure` 补 `caption_source`（`parser|agent|human`），保证用户改过的图注永不被自动覆盖。
- **模块**
  - `litbench/audit.py`：`write_audit`（同步 add，随调用方事务提交）+ `list_audit`；
  - `litbench/auto_accept.py`：快照/恢复、`apply_proposal`（人工/自动共用）、`evaluate_auto_accept`
    （返回阻止原因，便于 UI/日志解释）、`auto_accept_proposal`、`sweep_auto_accept`、
    `rollback_proposal`、`reject_proposal`；
  - `litbench/tasks.py`：任务租约（`create_task`/`claim_task`/`claim_task_by_id`/`heartbeat_task`/
    `complete_task`/`fail_task`/`recover_expired_tasks`/`list_tasks`）；条件 UPDATE + rowcount
    保证并发 claim 只有一个赢家；
  - `litbench/workers/tasks.py`：内置 worker 执行 `figure_review`（调 `ai_review_figure`）与
    `auto_accept_sweep`；失败按 attempts 重试，超次数 failed。
- **API（全部有测试）**：`GET/POST /api/tasks`、`POST /api/tasks/{id}/claim|heartbeat|complete|fail`、
  `POST /api/tasks/enqueue-figures`、`POST /api/tasks/recover-expired`、
  `GET /api/proposals/stats`、`POST /api/proposals/auto-accept`、`POST /api/proposals/decide-batch`、
  `POST /api/proposals/{id}/rollback`、`GET /api/audit`。
  人工改图号（`POST /figures/{id}/label`）与图定位纠错（`POST /figures/{id}/review`）也接审计。
- **worker 主循环**：每轮先消费解析 job，队列空再领一个 agent 任务；启动时恢复过期租约。

### 12.18.2 自动接受规则（宁严勿滥，docs/15 §5 D3）

自动接受只在**全部条件满足**时生效（`evaluate_auto_accept`）：

1. `kind == figure_review`、proposal 仍 pending、`needs_change=true`；
2. 置信 ≥ `AUTO_ACCEPT_THRESHOLD`（默认 **0.95**）；
3. 证据 ≥ `AUTO_ACCEPT_MIN_EVIDENCE`（默认 1 条，图号候选/邻图注/图片模式都算）；
4. 裁框问题（`cut_off/too_large/too_small/not_figure`）→ 永远留人工（M3c 默认只提议）；
5. 目标未被人工改过：图号 `label_source != "review"`；图注 `caption_source == "parser"`；
6. 建议图号能归一且仍在候选集合内（在提案创建时 `_validated` 已校验）。

通过后：actor=`auto-accepter`，图号来源 `label_source=agent`、置信写模型自评值；
图注 `caption_source=agent`；写 `before_json` + `audit_log(action=proposal.auto_accept)`。
人工采纳走同一 `apply_proposal`（`label_source=review`、置信 1.0），拒绝也写审计。
回滚按 `before_json` 恢复并写 `audit_log(action=proposal.rollback)`，proposal 状态 `rolled_back`。

### 12.18.3 真机验收（10 篇新语料）

- 用 `POST /api/tasks/enqueue-figures {label_state: "unreviewed_all"}` 排序 10 个 `figure_review`
  任务（4 张 unlabeled + 6 张 label_norm 为空/低置信；10 篇论文语料）；
- 3 个 worker 并行消费（验证多 worker 抢租约）：10/10 done；其中 1 个任务两次
  `ReadTimeout` 后第 3 次成功（attempts=3），说明重试与租约回收真实生效；
- 结果：**1 条自动接受**（`Fig. 6` 图注 OCR 噪声修复：`for the / = 0.5` → `for the L/D = 0.5`，
  置信 0.96，`auto_accepted=true`）；**6 条 pending**（置信 0.90–0.94，都低于 0.95，自动接受器
  如实拒绝并在 `skipped_items` 写明原因）；**3 条 no_change**（AI 检查通过，不入队）；
- 审计：1 条 `proposal.auto_accept`，before/after 完整（图号未变、图注旧值→新值）；
  无人工改过的目标被覆盖；
- 单测：`server/tests/test_m3_governance.py` **15 passed**（自动接受高置信/人工改过跳过/
  低置信与裁框跳过/关闭开关/扫描/回滚/批量/审计/任务生命周期/重试/过期回收/幂等/批量排任务/
  worker 执行），后端总计 **287 passed**。

### 12.18.4 前端待审队列（同批交付）

- `web/src/Proposals.tsx` 重写为三页签（待审 / 已自动接受 / 全部历史）：before/after 图号与图注
  差异、置信度、多选批量采纳/拒绝、自动接受扫描、AI 复核待确认图（排任务）、已接受项回滚、
  每条展开审计；`api.ts` / `types.ts` 增加 M3 接口与类型；顶栏徽标改用 `/api/proposals/stats`。
- 验收：`npm run build`（tsc strict + vite）通过；用打包后的前端 `api.ts` **直连 8000 真实数据**
  （非 mock）实跑单条采纳 / 批量拒绝 / 回滚 / 自动接受扫描 / 排任务；回滚后重新取图，label 与
  caption 均恢复为 before 快照值。
- 截图（headless Edge + CDP，`docs/assets/m3/00–04-*.png`）：顶栏「AI 建议 3」→「待审 3 ·
  已接受 1 · 已拒绝 2 · 已回滚 1」；待审卡片显示 before/after（Fig.7 图注旧值划掉 → 新值）、
  置信度 93%；「已自动接受」页签显示 Fig.6 96% + `自动接受 (auto-accepter)` + 回滚按钮；
  展开审计可看到记录（pending 项显示「还没有审计记录」）。

### 12.18.5 边界与后续

- 前端已同批交付（见 §12.18.4）；
- 裁框（M3c）仍默认只提议不自动写；`figure.group` 未接自动接受；
- 内置 worker 同步调模型（单任务、可重试）；外部 agent 领任务走 `/api/tasks`，
  MCP 的 propose/task 工具留给 M5；
- 任务表 cost 字段已接（`proposal.cost` 写回 task.result/cost），本轮前 10 条任务由
  旧代码执行、cost 为 0，之后的任务会记录。


## 12.19 M3c 图定位框复核（2026-11-13 下午）

### 12.19.1 交付

- `server/litbench/bbox_review.py`：对一张图跑多模态框复核，产出
  `figure_proposal(kind="figure_bbox")`，**默认只提议、不改图**；
- `llm.py` 新增 `vision_message_multi(text, [(png, media_type, label), ...])`：三种协议
  （chat_completion / anthropic_messages / openai_responses）都能发**多图**；
- 三张输入图：整页原图 + 在原页画红框的标注页 + 当前框裁图（都在线程池里渲染/缩放，
  单张 ≤ `BBOX_REVIEW_MAX_IMAGE_DIM=1100`）；
- 输出 JSON：`{verdict: ok|wrong, bbox: [x0,y0,x1,y1] 归一化 0–1, confidence, reason}`；
- 服务端四道校验后落 proposal：
  1. 归一化/点数均可，越界 clamp、宽高 < `MIN_BOX_PT=20` 直接 422；
  2. 当前框与新框 IoU；
  3. caption 锚点：新框与同页 caption block 的垂直间隙 ≤ `CAPTION_GAP_MAX=110pt` 且水平重叠；
  4. 模型说 wrong 但新框 IoU ≥ 0.9 → 视为无需改动（status=no_change）。
- 自动接受：`BBOX_REVIEW_AUTO_ACCEPT=0`（默认关）；打开后还需同时满足
  「verdict=wrong + IoU ≤ 0.2 或 ≥ 0.9 + caption 锚点一致 + 置信 ≥ 0.95」；
- 采纳（人工或自动）：写 `figure.bbox_override_json` + `review_status=corrected` + 审计，
  API 随后**重裁缩略图**；回滚按 `before_json` 恢复 bbox_override/image_path/review_*。
- API/任务：`POST /figures/{id}/bbox-review`（同步单图）、
  `POST /tasks/enqueue-bbox-review {paper_id?,figure_ids?,limit?}`（批量排
  `figure_bbox_review` 任务，worker 执行）；租约/重试/幂等与 M3 共用一套。
- 前端：待审卡片增加「框复核」badge；`定位框（pt）旧值→新值`、`与现框 IoU`、`caption 锚点`
  三行差异；采纳/拒绝/回滚/审计复用 M3 已有交互。

### 12.19.2 真机验收（10 篇新语料）

- 批量排 6 个 `figure_bbox_review` 任务（PDF 现场渲染三图）：6/6 done，
  成本 **$0.0011–$0.0033/张**（比 M3a 单图纠错更省，因为 prompt 更短）；输出全为
  `verdict=ok`、置信 0.95–0.98、caption 锚点 5/6 一致——说明这批 arXiv 图的 pdfium 框
  基本可用（也解释了为什么没有 pending）。
- 为验证 wrong 分支，把 Fig.4 的框人为改成「只留上半部分」（bbox 高度砍 45%）后重跑：
  模型判 `verdict=wrong`、置信 0.92、IoU 0.38、给出扩展后的框
  `[183.6, 87.1, 428.4, 356.4]`（reason 明确指出漏掉下半部分/坐标轴/轴标签）；
  默认进待审 → UI 截图 `docs/assets/m3/06-bbox-detail.png`（显示旧框→新框、IoU 0.38、
  锚点不一致会拦自动接受）→ 采纳写 `bbox_override` + 重裁缩略图（版本号变化）→ 回滚恢复
  `bbox_override=None`、`review_status=pending`。
- 单测：`server/tests/test_bbox_review.py` **10 passed**（坐标归一化/太大太小 422、
  caption 锚点、pending/ok/invalid、自动接受开关、采纳+回滚写读、worker 任务、批量排任务），
  后端总计 **297 passed**。
- 边界：**50 张人工复核框测试集尚未建立**（当前语料没有人工 bbox 标注），所以 M3c 验收
  口径里的「自动建议与人判一致率 ≥ 90% / 误改率 < 2%」暂时无法量化；本轮只能证明
  链路正确 + 模型在人为坏框上能识别并给出可采纳提议。下一步要用真实「人工复核过的图」
  累积 golden bbox 后再校准自动接受开关。

### 12.20 M6 欢迎页 · M6a 交付（2026-11-13）

- 定位：欢迎页是**独立支线**，不阻塞 M5 写作侧；只新增 `web/src/welcome/`，主应用只改
  `main.tsx` 的入口分流（默认首访播一次，之后直接进工作台，`?welcome=1|0` 可强制/跳过）。
- 已交付 M6a：程序化桌案/隔扇窗/手卷/油灯/窗外剪影 + 四盏灯（含 2048 窗棂阴影）+
  34s 关键帧相机 + 窗扇开合 + 薄雾尘埃 + 竖排题字/朱印排版 + WebAudio 环境音。
  规格、验收与已知问题见 `docs/16-欢迎页设计规格.md` §15，截图 `docs/assets/m6/`。
- 技术：原生 three（不上 R3F/GSAP，理由见 `docs/16` §10.1）；Welcome 懒加载 chunk
  **131KB gz**，工作台首屏体积不变；WebGL 失败退回同构图静态渐变。
- 未做（M6b）：真实材质/资产、体积光与后处理（Bloom/DOF/颗粒）、窗外 3D 化与鸟的扇翅、
  字体子集、移动端静态帧与 `saveData`/reduced-motion 降级。

### 12.21 M6b 欢迎页质感重做（2026-11-13 同日第二轮）

- 触发：用户验收 M6a 后指出「火苗不动、窗外是 2D、没有太阳光与景深、细节太少」，要求按专业前端标准重做。
- 已做：`EffectComposer` 后处理链（屏幕空间光轴 / Bokeh 景深跟焦曲线 / Bloom / 胶片颗粒与暗角 / ACES 输出）；
  窗外改成真三维（4 层山脊视差 + 树 + 扇翅的鸟 + 雾带 + 太阳球与光晕）；平行光入射角改到 42°，
  光带真正落在书卷上并在纸面投出冰裂纹影子；桃枝改为 Tube 枝条 + 150 朵花簇 + 飘落花瓣；
  火苗与青烟改为噪声着色器；金属/瓷/漆加环境反射与 bump；补接触阴影与茶盏/砚/笔/叠书道具。
- 数据：Welcome chunk 556KB raw / 142KB gz；headless 五帧无报错；**真机 GPU 帧率未测**（软件渲染环境）。
- 已知不足与下一步见 `docs/16` §16.3（山体做高度场、天空散射、真体积光、书页真字形、字体子集、移动端裁剪）。

### 12.22 M6c 场景增强（2026-11-13 第三轮）

- 触发：用户点名「地形、天空散射、太阳更亮、桃枝占比更大、砚台与镇尺建模更精细」。
- 已做：真高度场地形（180×130m / 220×150 段 / fbm + 顶点色空气透视 + 假朗伯受光）；天空散射着色器
  （天顶-地平渐变 + 太阳盘 + 地平暖带，太阳方向按相机实时重算）；直射光 3.2 + 曝光 0.87 + HDR 太阳盘，
  光轴阈值抬高避免整屏泛白；桃枝改为斜穿窗景（主枝 + 4 细枝 + 1 低横枝，300 花簇，Standard + 自发光）；
  砚台/镇尺改倒角挤出并加墨池、池沿、笔舔槽、刻线、翘头；修掉墙面回弹光平面边缘的漏光条。
- 数据：Welcome chunk 575KB raw / 148KB gz；headless 四帧 + 实播无报错；**真机帧率仍未测**。
- 仍未做：书页真字形、字体子集、桃枝分叉的自然弯曲、移动端静态帧、物理大气散射。

### 12.23 M3 收尾：任务面板 + 人工重试 + 提案阈值校准（2026-11-13 晚）

M3 正式化（§12.18）与 M3c（§12.19）交付后留了三个尾巴，本轮一次收掉：

1. **任务只存在于 API，人看不见**。`GET /api/tasks` 早就有，但界面只有「AI 复核进行中 N 条」。
   本轮在「AI 建议」抽屉加第 4 个页签「任务」：kind/状态/目标/执行者/attempts/租约/费用/错误
   全部展开；failed / canceled 可点「重试」，另有「回收过期租约」按钮
   （`POST /api/tasks/recover-expired`）。
2. **failed 任务没有人工重试入口**。新增 `POST /api/tasks/{id}/retry`：
   只允许 failed / canceled → queued，清空错误/租约/started_at/finished_at，`attempts=0`；
   queued/running/done 返回 409（不抢正在跑的 worker）。测试 4 条（生命周期/重试/冲突）。
3. **自动接受阈值拍在 0.95，没有数据支撑**。新增 `scripts/calibrate_bbox.py`：
   - 以「人已经采纳/拒绝/回滚」的提案为 ground truth，按 0.90–0.98 阈值模拟
     precision / coverage，输出推荐阈值；
   - 样本 < 5 时明确写「样本不足，保持默认 0.95」，不装出结论；
   - `--export-golden` 把人判提案（含 proposed/before）导出成
     `litbench.proposal-golden/v1` JSON，之后可当 50 张人工 bbox 测试集的第一批种子。
   - 真机现状（dev 库）：`figure_bbox` 7 条（6 no_change + 1 人判后回滚），
     `figure_review` 11 条（1 accepted / 2 rejected / 1 rolled_back / 3 pending / 4 no_change）。
     两者都「样本不足」；`figure_review` 在 ≥0.95 区间 1/1 正确（precision 100%，coverage 25%），
     与默认阈值一致——**不改阈值，继续攒人判**。

边界（如实记）：

- **50 张人工 bbox 标注集仍未建立**。脚本只解决「有决策就能算」，不解决「还没有足够决策」；
  真实一致率/误改率指标仍待人工过一遍待审队列。
- `figure.group` 自动接受仍未接：当前 AI 纠错 pipeline 不产出 group 提案，
  只登记在计划里，等真有 group 建议再谈白名单。
- 任务面板没有「取消」按钮：queued 任务取消需要 worker 侧协作，预计在 M7 任务治理里一起做。

### 12.24 M5 写侧：写作台 + MCP 提案/任务（2026-11-13 晚）

**M5 分两条腿：让 agent 能「写回提案/领任务」，让人能「把证据写成稿子」。**

#### A. 写作侧（人）

- 迁移 `0011_writing_claim`：`claim`（id / paper_id / paper_version_id / text / section /
  status=draft|approved / source=human|agent / evidence_json / tags_json / order_idx / 时间戳）。
  设计延续「证据是一等公民」：**claim 的 evidence 至少 1 条，且必须能在本库解析到**
  （block/figure/table/summary），并且都属于**同一 paper_version**；解析失败不落库。
- `server/litbench/writing.py`：
  - claim CRUD + 过滤（paper/section/status/q）；
  - `build_matrix`：1–8 篇，派生「题目 / 作者 / 年份来源 / 摘要 / 关键发现 / 图·表 / 我的论断」，
    **不落库**（派生结果随时会过期）；
  - `export_writing`：LaTeX（`ctex` + `natbib` + `refs.bib` 成对返回，claim 自动带
    `\cite{cite_key}`，LaTeX 特殊字符转义）、BibTeX（`@article`，authors/doi/arXiv/venue）、
    Markdown；`include_drafts` / `with_evidence_comments` 可选；
  - `check_citations`：只认确定性匹配（cite_key 精确 → DOI/arXiv 裸值 → `arXiv:`/`doi:` 前缀
    → 相似度 ≥0.6 建议），不调模型；返回 resolved/unknown + 建议键。
- API：`GET/POST /api/claims`、`GET/PATCH/DELETE /api/claims/{id}`、`GET /api/matrix`、
  `POST /api/writing/export`、`POST /api/writing/check-citations`。
- 前端 `web/src/Writing.tsx` + 顶栏「写作」入口，三页签：
  1. **素材**：选文献 → 自动列出图/表/整篇速览当证据（没有证据时保存按钮禁用）；
     卡片按章节分组，可改文字、切草稿/已认可、删；
  2. **对比矩阵**：按标题过滤 + 勾选 1–8 篇 → 表格（第一列维度，其余列每篇）；
  3. **导出**：标题/格式/是否含草稿/证据注释 → 预览 + 复制 + 下载（LaTeX 额外给 refs.bib）；
     底部引用校验粘贴框，可直接用导出的 LaTeX 校验。
- 验证：
  - `server/tests/test_writing_api.py` **9 passed**（create/list/update/delete、证据缺失/跨版本 422、
    矩阵派生与缺文献、LaTeX 转义 + refs.bib、BibTeX/Markdown、只导出已认可、引用校验三种写法）；
  - 真机 UI（CDP，`docs/assets/m5/01-writing-light.png`）：选 1 篇真实文献 → 证据列表 4 张图 +
    表/速览 → 保存卡片 → 切「已认可」；导出 417 字符 LaTeX（含 `\cite{arXiv:2603.09607}`）；
    引用校验「全部引用都能在文献库里找到」。
  - 边界：没有 claim 编辑历史/版本；矩阵维度固定 7 行（自定义维度留给后续）；导出不生成
    正文段落，只给骨架；`refs.bib` 用库内 cite_key，尚未接 Crossref 补全。

#### B. MCP 写侧（agent）

- scope 语义：HTTP 传输仍要求 `agent-read`（读基线）；写工具在**工具层**校验
  `agent-propose`（proposal.*）/ `agent-task`（task.*）。token `--scope` 支持逗号多选，
  写 token 用 `--scope agent-read,agent-propose,agent-task`。stdio 由用户拉起，视为已授权。
- 新工具 8 个：`proposal.create`（必须带 evidence_block_ids；复用内置 AI 的
  `figure_review._validated` 三道闸；`needs_change=false` 直接 422）、`proposal.list/get`、
  `task.list/claim/heartbeat/complete/fail`。`actor` 取自 token label，调用方不能伪造。
- 服务端新增 `POST /api/proposals`：外部提案落 pending + 作废旧 pending + 写
  `audit_log(action="proposal.create")`；只接受 `figure_review`（框复核的四道校验在多模态链里，
  外部提案走不了，明确拒绝）。
- 验证：
  - `server/tests/test_m5_agent_write.py` **4 passed**（外部提案落库/审计、空证据 422、
    无改动 422、旧 pending superseded、任务重试）；
  - `mcp/tests/test_m5_tools.py` **8 passed**（schema/提案创建-列表-取、空证据 schema 拒绝、
    任务生命周期、冲突 error_code、失败重排、多 scope 签发、HTTP 只读 token 写 → insufficient_scope、
    写 token → pending）；
  - 全量：后端 **311 passed**、MCP **110 passed**、`ruff check mcp` 通过、前端 `tsc + vite build` 通过。
  - 截图：`docs/assets/m5/01-writing-light.png`、`02-matrix.png`、`03-export.png`、
    `04-citation-check.png`、`05-task-panel.png`。

边界：

- MCP 写侧**还没有真正被一个外部 agent 在真实库上跑一遍** proposal→人审→落地闭环：
  本轮验证到「工具 + API 契约 + 单测 + HTTP scope」。下一步用 DSH/Claude 跑一条真实任务，
  过一遍人审。
- 写侧没有自动接受：外部提案一律 pending（比内置 AI 更保守），符合「agent 不能直接改事实表」。

### 12.25 References 条目生成：13 种格式预设（2026-11-13 晚）

用户点名的功能：**根据文章信息和格式预设一键生成 references 条目**。写作台从 3 页签扩到 4 页签
（素材 / 对比矩阵 / 导出 / **References**）。

#### 后端

- 新模块 `server/litbench/references.py`：
  - **13 种预设**：GB/T 7714-2015（顺序编码制）、APA 7、IEEE、MLA 9、Chicago（作者-年份）、
    Nature、Science、ACS、Elsevier/Harvard、Vancouver、BibTeX、RIS、自定义模板。
  - 作者姓名按格式规则处理：GB/T 姓氏大写 + 名缩写（`ZHANG W`）、APA 串列逗号（`Zhang, W., & Li, M.`）、
    MLA/Chicago 第一作者全名、IEEE 名在前、Vancouver 名缩写无点；中文姓名原样保留。
  - 缺字段**省略 + warning**（缺出版年 / 期刊名 / 卷号 / 期号 / 页码 / DOI·arXiv / 作者），
    不补假值；`_tidy` 清掉空括号、重复句点、悬空 `DOI:`。
  - BibTeX 输出 `@article`（title/author/journal/year/volume/number/pages/doi/eprint）；
    RIS 输出 `TY/AU/TI/JO/PY/VL/IS/SP/EP/DO/ER` 多行记录。
  - 自定义模板占位符：`{authors} {authors_short} {title} {journal} {year} {volume} {issue}
    {pages} {doi} {arxiv} {url} {cite_key} {type}`。
- 著录字段：`paper` 新增 `volume / issue / pages`（迁移 **0012**），Zotero（json / sqlite / local API）
  导入时带上；`PATCH /api/papers/{id}` 允许手动补全/清空这三项（不动 Zotero 镜像的标题/作者/DOI）。
- API：`GET /api/references/styles`（13 种 + 样例 + 适用说明）；
  `POST /api/references/format`（paper_ids + style + numbered + template → 逐条 entry / warnings / 合并文本）。
  只读派生，不落库；顺序按传入、重复 id 去重。

#### 前端

- 写作台新增 **References 页签**：格式下拉（13 种，带样例与规则说明）、文献多选（默认全选、
  可按标题过滤、全选/清空）、带序号开关、自定义模板输入、**补全卷期页**折叠面板（选中文献 → 填卷/期/页 →
  保存后重新生成）；右侧逐条预览，可单条复制 / 全部复制 / 下载 `.txt`（BibTeX / RIS 分别存 `.bib` / `.ris`）。
- 截图：`docs/assets/m5/06-references-gbt-light.png`（GB/T）、`07-references-apa-light.png`（APA）、
  `08-references-ieee-dark.png`（IEEE，暗色）。

#### 验证

- `server/tests/test_references.py` **8 passed**：13 种格式的片段断言（作者顺序/卷期页/DOI）、
  自定义模板、缺字段 warning、中文作者、顺序去重、未知格式 422、缺文献 404、
  preprint → arXiv 落款、卷期页 PATCH 后立即生效；
- 全量后端 **319 passed**、MCP **110 passed**、前端 `tsc + vite build` 通过；
- 真机 UI（CDP）：插入一条完整著录的示例文献 → 生成 GB/T / APA / IEEE 三种条目，
  卷期页与 DOI 均正确，亮/暗两套主题无报错，验收后已删掉示例文献（dev 库仍 10 篇）。

#### 边界

- 只实现「单篇期刊/会议/预印本」的常用写法；译者、版次、学位论文 `[D]`、专利、标准、
  页码合订等边角规则没做（需要时按格式加分支，不硬凑）。
- 不生成斜体/粗体：输出纯文本，复制到 Word/LaTeX 后自行加样式。
- 卷期页对 arXiv/PDF 直传文献通常为空（Zotero 才有）；页面提供手动补全，
  缺字段如实列在条目下，不拿「在线优先」等默认值填充。
- 没有 CSL（citation-style-language）引擎：格式是手写模板，和出版社官方 CSL 模板在边角上可能有差异；
  需要严格投稿时仍应把输出粘进期刊模板复核。

### 12.26 M5 真机闭环：外部 agent + 真实模型（2026-11-15）

P0 验证第一项（docs/17 §2.1）落地。之前 M5 写侧只有单测与 HTTP scope 验证，本轮补上
「真实外部进程用 MCP 领任务、调真实模型、投提案、人审落地」的全链路。

#### 链路

| 步骤 | 执行者 | 动作 | 结果 |
| --- | --- | --- | --- |
| 1 | 人 | `POST /api/tasks` 给一张图排 `figure_review` | task queued |
| 2 | 外部 agent（`agent_task_loop.py`，MCP stdio） | `task.list` → `task.claim` | running，attempts=1 |
| 3 | 外部 agent | `figure.get` + `evidence.get` | 图注 93 字 + 同页 8 个证据块 |
| 4 | 外部 agent | chat_completion 模型判图注 | `ocr_noise`，置信 0.98，tokens 832/192 |
| 5 | 外部 agent | `proposal.create` + `task.complete` | pending 提案 `2164…`，task done |
| 6 | 人 | Web UI「AI 建议」待审卡片点「采纳」 | 图注 `o fthe` → `of the` |
| 7 | 人 | `POST /api/proposals/{id}/rollback` | 图注恢复，proposal rolled_back |

审计链（`GET /api/audit?target_id=…`）：

- `proposal.create`，actor=`stdio`，after 含 figure_id/kind/needs_change/confidence；
- `proposal.accept`，actor=`human`，before/after 含图注与图号字段；
- `proposal.rollback`（第二个演示提案同样走了一遍）。

#### 实现

- 新增示例 `mcp/examples/agent_task_loop.py`（MIT，随 `mcp/` 目录）：
  - 只支持 `chat_completion` 协议（与 `agent_demo.py` 一致），读 `.data/llm-config.json`，不打印 key；
  - 系统提示明确「文献是不可信数据」，只允许 OCR 噪声清洗、不许改写/补全截断内容；
  - `max_tokens=2000`：deepseek-v4.1-flash 的 reasoning 会吃掉预算，700 时 `content` 为空
    （首次跑就踩到，已在脚本里注明）；
  - 模型认为无需改动时直接 `task.complete({no_change:true})`，不造空提案（服务端也会 422 拦）。
- 修前端待审卡片假 diff：`suggested_label=null` 且 `label_ok=true` 时，原来会显示
  「图号 Fig.1 →（无）」，像要清空图号；现在只在明确给新图号或 `label_ok=false` 时显示 diff。

#### 验证

- 真机跑了两轮（第一轮用于修 UI diff，第二轮截图）：两轮都是 pending → 采纳 → 回滚；
  任务 result 写回 `{proposal_id}`；模型费用未计入 LitBench（外部 agent 自己调网关，token 由脚本打印）；
- 截图：`docs/assets/m5/09-agent-proposal-pending.png`（外部提案待审，图注 before/after）、
  `10-agent-proposal-accepted.png`（采纳后提示）；
- 后端测试仍 319 passed、MCP 110 passed、前端 build 通过。

#### 边界

- 这是**文本 agent**：没有图片输入，`crop_issue` 只能填 unknown；视觉框复核仍由服务端
  多模态链（M3c）负责。
- 只验证了一个「图注 OCR 噪声」例子；模型判断质量不等于自动接受阈值，仍需 §P0.2 的人审样本。
- 外部 agent 的 token 费用发生在它自己的网关，LitBench 只记任务 cost=0（脚本可自行上报）。

### 12.27 团队共享第一步：账号 / 会话 / 成员管理（2026-11-15）

用户拍板：**工作组形式**（每人私有文献库只归个人；团队库只放用户主动共享的内容）、
本地账号 + 管理员建号、局域网 Docker/Postgres 部署、学位论文只做 LaTeX。
团队共享按「身份 → 工作组/共享 → 数据归属 → 部署」分步做，本轮完成身份层。

#### 交付

- 迁移 `0013_team_auth`：
  - `app_user`（username 唯一 / display_name / password_hash / role=admin|member / is_active）；
  - `user_session`（token_sha256 唯一 / expires_at / last_seen_at）。
- `server/litbench/auth.py`：
  - 密码 `hashlib.scrypt`（N=2^14，存 `scrypt$N$r$p$salt$hash`）＋常量时间校验；
  - 服务端 session：浏览器 cookie 只放随机 token，库里只存 sha256，退出/停用立刻失效；
  - 登录失败不区分「用户不存在/密码错」，避免枚举账号。
- API：
  - `GET /api/auth/status`（认证开关/是否初始化/当前用户）；
  - `POST /api/auth/setup`（仅零用户时创建第一个管理员并登录）；
  - `POST /api/auth/login|logout`、`GET /api/auth/me`；
  - `GET /api/users`、`POST /api/users`、`PATCH /api/users/{id}`（管理员建号/停用/重置密码）。
- 中间件：`AUTH_ENABLED=1` 时除登录/探针/文档外所有 `/api/*` 要求登录；未登录 401。
  认证默认关闭，单机开发与既有 319 个测试不受影响（测试 conftest 自动关闭）。
- 前端：`AuthGate`（初始化管理员 / 登录）、顶栏用户菜单（角色、退出）、成员管理浮层
  （建号、停用/启用、重置密码）；登录页/菜单截图 `docs/assets/team/01–05`。

#### 验证

- `server/tests/test_auth.py` **6 passed**：初始化仅一次、哈希不落明文、登录/登出、
  管理员/成员权限、停用后会话失效、短密码 422；
- 全量后端 **325 passed**、前端 `tsc + vite build` 通过；
- 真机 UI（CDP）：首次初始化管理员 → 工作台显示用户菜单 → 建成员「张三」→ 退出登录 →
  成员登录成功；无 JS 报错；验收后清空演示账号（dev 库回到未初始化状态）。

#### 边界 / 下一步

- **数据还没有归属**：`paper/collection/claim` 上还没加 owner，工作组/共享表也还没建；
  所以现在只是「能登录的同一个库」，不是真正的私有库 + 团队库。这是下一步（迁移 0014）。
- 认证关闭时接口不做任何隔离（沿用旧行为）；打开认证后 MCP/HTTP 还没有服务令牌，
  需要随工作组/数据归属一起补（否则 MCP 调用会被 401）。
- 部署仍是本机 uvicorn；Docker/Postgres 安排在数据归属之后。

### 12.28 团队共享第二步：工作组 / 私有库 / 共享（后端，2026-11-15）

用户拍板的数据模型见 `docs/17` §8.1：**每个用户私有库只归个人；团队库只是成员主动
共享到工作组的文献视图**。本轮把「身份」接到数据上——后端从「同一个全局库」变成
「私有库 + 工作组共享」，前端切换/共享界面留到下一步。

#### 交付

- 迁移 `0014_team_workgroups`：`workgroup` / `workgroup_member` / `paper_share` /
  `user_paper_state` 四张表，`paper/collection/claim.owner_user_id` 三个归属字段
  （旧数据 NULL = legacy，仍可见；owner 只做逻辑外键，原因见迁移文件头：SQLite 不支持
  `ALTER TABLE ADD CONSTRAINT`，而 dev 库是手写 DDL）。
- `server/litbench/team.py`（判定层）：
  - `can_access_paper` / `can_edit_paper`：own / legacy / 共享到我所在组 / 管理员；
  - `private_paper_condition` / `visible_paper_condition` / `workgroup_paper_condition`：
    列表与检索复用同一套 SQL 条件，避免「列表过滤了、详情忘了过滤」；
  - `require_workgroup_member` / `require_workgroup_manager`：非成员按 404 处理，不暴露组存在；
  - `paper_states` / `set_paper_state`：认证打开后笔记/收藏/阅读状态走
    `user_paper_state`（B 改备注不影响 A）；
  - `ensure_paper_access` / `ensure_paper_edit` / `ensure_figure_*` / `ensure_summary_*`：
    路由层统一翻译成 404/403。
- API `server/litbench/routers/team.py`：
  - `GET/POST /api/workgroups`、`GET/PATCH/DELETE /api/workgroups/{id}`（建组自动 owner）；
  - `POST /api/workgroups/{id}/members`（按用户名加人）、
    `DELETE .../members/{user_id}`（移除/退出；最后一个 owner 不能走）；
  - `POST /api/papers/{id}/share`（幂等）、`DELETE .../share/{workgroup_id}`、
    `GET .../shares`；全部要求登录（认证关闭 → 503，不悄悄放宽）。
- 数据归属接入：
  - `papers.py`：上传/Zotero 导入写 `owner_user_id`；列表默认私有库，`?workgroup_id=`
    切团队库；详情/正文/PDF/图/表按可见性 404；`PATCH` 的卷期页仅 owner/管理员可改，
    笔记/收藏/阅读状态写当前用户私有状态；同一 PDF 属于别人库时 409 且不回显标题；
  - `figures.py`：列表按个人/工作组范围过滤（收藏也按用户），按 id 的详情/图片/复核/
    改号/归组/删除全部过可见性/可写性；
  - `knowledge.py`：目录按 owner 过滤与校验，Zotero 导入把新文献/目录归当前账号；
  - `writing.py`：claim 列表/导出按 owner 过滤，claim 增删改校验 owner，矩阵/导出校验
    文献可见性；
  - `summaries.py`、`agent.py`、`references.py`：按 paper / summary / proposal 的入口
    都补了可见性（至少详情与写操作）；看不见的文献不能从「按 id」的接口绕进去。
- 接口字段 `PaperOut.owner_user_id/owner_username/can_edit/shared_workgroup_ids` 就绪；
- 前端（`web/src/Team.tsx`）：顶栏 `WorkspaceSwitcher`（个人库 / 工作组下拉、新建/管理入口）、
  文献卡片与详情页 `ShareMenu`（按工作组勾选共享/取消共享，显示「已共享 N」）、
  `WorkgroupPanel`（建组、改名、成员名单、按用户名加人、移除/退出、解散；owner/管理员才可见管理动作）；
  团队库左栏显示组名/成员头像/共享篇数与只读说明；共享文献在 B 的界面按只读处理
  （隐藏补 PDF/增图/AI 修图，顶部显示「由 X 共享，只能阅读」）；`listPapers / searchFigures`
  支持 `workgroup_id`，切换工作区会重拉文献/图表/统计；收藏夹与知识树只在个人库出现。

#### 验证

- 新测试 `server/tests/test_team_workgroups.py` **6 passed**：建组/成员权限/最后一个
  owner、A/B 私有库隔离、共享后 B 只读可见、取消共享立刻 404、`user_paper_state`
  互不影响、上传归属与跨用户 PDF 409、目录/claim/导出/references/摘要/提案越权、
  认证关闭时团队接口 503；
- 全量后端 **331 passed**、MCP **110 passed**、前端 `tsc + vite build` 通过；
- dev SQLite 已按迁移文件手写同样 DDL（表/字段/索引全部就位，可重复执行）；
- 本机 API 已重启加载新代码，`/api/openapi.json` 共 77 个 path，工作组/共享 7 个入口在线。
- **A/B 真机 UI 验收（headless Edge + CDP，独立验收库，不污染 dev 库）**：
  - A（李雷）个人库 2 篇 → 卡片共享菜单勾选「传热与流体课题组」→ 卡片显示已共享 1；
  - 工作组管理浮层显示 2 位成员（李雷 owner / 韩梅梅 成员）与 1 篇共享；
  - B（韩梅梅）个人库 0 篇（看不到 A 未共享的文献）→ 切团队库只看到被共享的那篇，
    卡片标「由 李雷 共享」→ 打开后顶部显示只读提示、无增图/修图入口；
  - API 级补测：A 取消共享后 B 团队库立即为空、`GET /papers/{id}` 404；
  - 截图 `docs/assets/team/06-alice-personal.png`、`07-alice-share-menu.png`、
    `08-workgroup-panel.png`、`09-bob-personal.png`、`10-bob-team-library.png`、
    `11-bob-shared-detail.png`；JS 报错为空。验收后删掉独立库，dev 库仍是 0 用户（未初始化）。

#### 边界 / 下一步

- 前端已完成；A/B 真机验收已过（截图 `docs/assets/team/06–11`）。
- `GET /api/jobs`、`/api/tasks`、`/api/proposals/stats`、`/api/audit`、
  `/api/dataset/golden` 仍是全局视图（只给可信 agent/管理员用；成员能看提案但计数/任务
  未按用户裁剪）。这是 v1 的已知边界，不是「已隔离」。
- 共享是文献级：图/表/摘要随文献可见；团队库 v1 是平铺列表，不做团队目录/团队笔记；
  不做逐篇权限、不做 owner 转让。
- 认证打开后 MCP 仍会 401（内部服务令牌还没做，`docs/17` §11.1 紧接下一步）。

### 12.29 MCP 内部服务令牌（2026-11-15）

认证打开后，MCP 没有浏览器 cookie，调 `/api/*` 会被中间件 401——团队共享上线后这条
立刻变成真问题。本轮给「API ↔ MCP」加一条服务间令牌，MCP 侧的客户端 Bearer token
（`litbench-mcp token issue`）保持不动：**前者是服务身份，后者是 agent 身份**，两者不能合并。

#### 交付

- API 配置 `INTERNAL_API_TOKEN`（`server/litbench/config.py`）：空 = 不启用；部署时与 MCP
  进程的 `LITBENCH_MCP_API_TOKEN` 注入同一个随机串（建议 `secrets.token_urlsafe(32)`）。
- 中间件（`server/litbench/main.py`）：认证打开时，若请求头
  `X-LitBench-Internal-Token` 与配置**常量时间相等**，则把 `request.state.user` 设为
  ``system`` 主体（不落库、不能登录），直接放行；错误/缺失令牌仍按未登录 401。
- `system` 语义（`server/litbench/auth.py` + `team.py`）：`is_superuser` = `admin` 或
  `system`；`system` 没有个人库，`private_*_condition` 直接全库，`visible_*`/`can_*` 全放行；
  `admin` 保持「详情可看全部、列表默认自己的库」的旧语义不变。
- MCP（MIT 薄层）：`Settings.api_token` 读 `LITBENCH_MCP_API_TOKEN`（校验 ≥16 字符、
  只允许可见 ASCII，避免头注入）；`LitBenchAPI` 自动带 `X-LitBench-Internal-Token`；
  `serve` 与 `create_server` 都传入该值；日志只打印「已配置/未配置」，不打印值。
- 部署：`compose.yaml` 的 `x-app-env` 注入 `INTERNAL_API_TOKEN` 与 `AUTH_ENABLED` /
  `AUTH_COOKIE_SECURE` / `AUTH_SESSION_DAYS`；`.env.example` 增加生成命令与说明；
  `mcp/README.md` 写清认证打开后的两个环境变量必须一致。

#### 验证

- 后端新增 `server/tests/test_internal_token.py` **3 passed**：无令牌 401、错令牌 401、
  正确令牌可读他人文献/管理接口、服务端未配置令牌时即使带对字符串也 401；
- MCP 新增 `mcp/tests/test_internal_token.py` **2 passed**（MockTransport 抓请求头：
  有令牌就带、没令牌不带）+ 配置侧 3 条（默认空、环境变量、短/含空格拒绝）；
- 全量后端 **334 passed**、MCP **114 passed**；前端无改动。

#### 边界

- 令牌是**服务级**的：任何拿到它的人/进程都等于 system 全库读取，必须只放部署环境，
  不写进仓库、日志、前端。MCP HTTP 的客户端侧仍必须带 Bearer token（`agent-read` 起）。
- 长期（`docs/17` §11.1）要把 MCP token 与用户绑定（`mcp_token.user_id`），按用户权限裁剪
  返回；当前 system 是过渡方案，MCP 能看到全库（和认证前行为一致）。
- 本轮只做「API ↔ MCP」；MCP 容器化/Docker 服务编排随 §11.2 一起做。

### 12.30 Docker + Postgres 首次真跑（2026-11-15）

此前 compose 从未实际启动过（Docker Desktop 不在本机）。本轮在 **WSL2 Ubuntu 22.04**
里装 Ubuntu 源提供的 `docker.io` + `docker-compose-v2`（Docker Hub 直连超时，daemon 配
`docker.m.daocloud.io` / `docker.1ms.run` 两个 registry mirror），在 WSL 内构建并启动
`compose.yaml`，宿主机经 18000/18080 访问，完成一次端到端部署验收。

#### 实测到的三个部署 bug（已修）

1. **缺 greenlet**：`server/requirements.txt` 写的是 `sqlalchemy>=2.0`；容器里没有
   `greenlet`，`migrations/env.py` 的 `async_engine_from_config` 直接 ImportError，
   api 容器起不来。改为 `sqlalchemy[asyncio]>=2.0`。
2. **Postgres 不认布尔列的数字默认值**：`migrations 0010/0013/0014` 里
   `server_default=sa.text("1"/"0")` 对应 boolean 列时 Postgres 报
   “default expression of type integer”；改成 `true`/`false`（SQLite 也支持）。
3. **健康探针被认证拦住**：`/api/ready` 不在 `_PUBLIC_API_PATHS`，`AUTH_ENABLED=1` 时
   Docker HEALTHCHECK 拿到 401，api 永远 unhealthy、worker/web 起不来。已加进公开路径，
   并在 `test_auth.py` 增加断言。

#### 验收结果

- postgres/api/worker/web 四容器 healthy；`alembic upgrade head` 0001→0014 全过；
- 上传真实 PDF → worker 在 Postgres 上解析出 1 block / 1 图（JSON 列、timestamptz、
  任务领取都正常）；内部服务令牌无/错 401、正确 200；
- 认证 + 工作组共享在 Postgres 上走通；`docker compose down`（保留卷）→ `up -d` 后
  账号、文献、解析结果、共享关系都在；
- 截图 `docs/assets/deploy/01-docker-web.png`（18080 的 nginx 前端 + 已登录工作台）。
- 验收后已 `down -v` 清掉测试卷；本机 dev 库/服务不受影响。

### 12.31 部署收尾：局域网 HTTPS / 备份恢复 / MCP 容器 / 并发验收（2026-11-15）

`docs/17` §11.2 剩下的四项在本轮一次做完，全部在 WSL2 Docker + Postgres 栈上实测。

#### 交付

- **局域网 / HTTPS**
  - `deploy/nginx-lan.conf`：TLS 终止（TLSv1.2/1.3）+ 反代到 web（`/api` 仍由 web → api）；
  - compose 增加 `proxy` 服务（profile `lan`），证书挂 `.data/certs`；
  - `scripts/gen_selfsigned_cert.sh`：按传入的 IP/域名生成带 SAN 的自签证书；
  - `api/web` 端口默认只绑 `127.0.0.1`（`API_BIND/WEB_BIND`），局域网只从 HTTPS 端口进；
  - `.env.example` 增加 `AUTH_COOKIE_SECURE=true` 的说明与 binding/port 变量。
- **备份 / 恢复**
  - `scripts/backup.sh`：`pg_dump -Fc` + storage 卷 + `mcp_state` 卷 + compose/git/校验和；
  - `scripts/restore.sh`：交互确认或 `--yes`；停应用容器 → DROP/重建库 → pg_restore →
    清空并解包 storage/mcp_state → 拉起基础服务 + 原来启用的 profile。
- **MCP 容器化**
  - `mcp/Dockerfile`：python:3.12-slim，只装 MIT 薄层，非 root；
  - compose `mcp` 服务（profile `mcp`，`mcp_state` 卷，默认只绑 `127.0.0.1:8765`）；
  - token 签发命令改为 `docker compose --profile mcp run --rm mcp token issue ...`
    （镜像 ENTRYPOINT 已是 `litbench-mcp`）。

#### 验证

- HTTPS：Windows `https://127.0.0.1:18443`、WSL `https://172.27.243.81:18443`（非回环）
  都 200；Setup 的 `Set-Cookie` 带 `Secure`（`AUTH_COOKIE_SECURE=true`）；
- 备份/恢复：备份 → 新增一篇文献 → `restore --yes` 后新增消失、原有 4 篇/工作组/共享
  关系回来；MCP Bearer token 随 `mcp_state` 卷恢复后仍能调工具；proxy/mcp 自动重新拉起；
- MCP 容器：无 token 401；`token issue` 后 16 个工具可用，`library.search` 经内部令牌
  读到 Postgres 里的文献；
- 并发验收：Windows 27 个请求（含 3 个并发上传）+ WSL 120 个请求同时打 HTTPS 反代，
  全部 2xx、无 5xx，两个账号的私有/团队库隔离正确；
- 截图 `docs/assets/deploy/02-lan-https.png`（HTTPS 入口已登录工作台）。

#### 边界

- 本机没有第二台物理机：并发验收用 **Windows 宿主 + WSL** 两个网络端模拟；
  WSL2 的端口转发默认只到 Windows localhost，真·局域网其它电脑应在 Linux 服务器
  （proxy 绑 0.0.0.0）或装了 Docker Desktop 的机器上补验一次。
- 自签证书只适合内网；正式给同事用要换内部 CA / Let's Encrypt 并设置
  `AUTH_COOKIE_SECURE=true`；HTTP 到 HTTPS 的 80 跳转未做（只开 443）。
- 备份是「整库回滚」不是增量；备份目录要单独保管（不含 `.env`，密码/令牌不在里面）。
- MCP 默认只绑本机；局域网 agent 需要 `MCP_BIND=0.0.0.0` 并自己套 TLS/反代。

### 12.32 学位论文 LaTeX 包（M8，2026-11-15）

用户拍板只做 LaTeX、不碰 Word/学校模板。实现按 `docs/17` §11.3 清单一次做完：

#### 数据与条目

- 迁移 0015：`paper.entry_type`（journal|conference|preprint|thesis|book|report|other，
  默认 journal）+ `thesis_school/degree/place`；dev SQLite 用 `.data/tmp/apply_0015.py` 手工应用。
- Zotero 导入把 `itemType` 映射到 entry_type，`university/thesisType/place` 落库；
  重复导入按上游覆盖，但 Zotero 没有的字段不会清空手工值。
- `references._entry_type` 优先用 `paper.entry_type`，旧数据/journal 保留 venue/arXiv 推断
  （否则老库里的 Proceedings 会退回 `[J]`）；13 种预设都补了 thesis 分支；
  `gbt7714` 输出「作者. 题名[D]. 出版地: 学校, 年份.」，缺字段只 warning；
  BibTeX 博士 `@phdthesis` / 硕士 `@mastersthesis`，RIS `TY - THES` + `PB/CY/M3`；
  book/report/other 也给 `[M]/[R]/[Z]` 与 `@book/@techreport/@misc`，不再冒充 `[J]`。

#### 导出与检查

- `thesis.py`：默认章节「摘要→绪论→相关工作→方法→实验与结果→结论」；
  claim.section 按别名归章（引言→绪论、实验结果→实验与结果、文献综述→相关工作…），
  对不上的 section 作为额外章节保留；`chapter_order` 可整体覆盖；
  产物 `.tex`（封面占位/摘要/目录/章节/`\cite`/`\bibliography{refs}`）+
  `refs.bib` + `references_gbt.tex`（`\input` 即改用 GB/T 文本版，`\bibitem` 键与 `\cite` 一致）。
- `check_thesis`：确定性扫描 `\cite{}`（复用写作侧解析与建议）、`\ref{fig:*}`、
  `\ref{tab:*}`；图按 cite_key/图号归一比对，表按表号归一比对；返回 `missing/unused/warnings`
  与逐条 resolved 详情；不调模型。
- 可见性：认证打开只导出可见文献 + 当前用户 claim（MCP system 令牌全库）；
  导出/检查都只读派生，不落库。

#### 验收

- `server/tests/test_thesis.py` 11 条；后端全量 345 passed、MCP 114 passed、前端 tsc+vite build；
- 真机：dev 库副本 + 3 篇真实 arXiv 文献 + 5 张 claim（1 草稿）→ 导出 → MiKTeX
  xelatex + bibtex 编译 10 页；截图 `docs/assets/thesis/01-export.png`、`03-check.png`，
  编译产物 `docs/assets/thesis/02-compiled.pdf`；
- 已知：源数据缺作者时 BibTeX 会告警（不是骨架问题）；cite_key 含 LaTeX 特殊字符会在
  warnings 提示；plainnat 只为开箱可编译，GB/T 顺序编码制用 `references_gbt.tex` 或换 bst。

#### 边界（不做）

学校定制页边距/封面/页眉字体、Word、PDF 直接排版、自动写正文、图表自动排版；
交叉检查只到词面，不保证语义正确；缺字段不编造。

### 12.33 表格进界面（P0，2026-11-15）

「图与表是一等公民」里表这一半一直是断的：`/api/papers/{id}/tables` 从 M1 起就能返回
Docling 抽出的结构化 HTML，但前端只在侧栏显示「N 表」。本轮补上：

- 文献详情新增「表格」页签（`web/src/PaperTables.tsx`），rail 徽标显示 `table_count`；
- 每张表显示表题 / 页码 / 结构化标记；HTML 经白名单清洗后渲染（只留表格结构与
  `colspan/rowspan/scope/class`，剥掉 script/iframe/on* 等），无 HTML 的图片表明确提示；
- 「原文」用现有只读 PDF 查看器跳到表所在页，不画框、不改数据；
- 「复制引用」生成 `\ref{tab:表号}`；无表号（旧解析数据）退化为 `\ref{tab:<table_id>}`，
  学位论文 `/api/thesis/check` 认 table_id，所以检查仍能通过；
- 「写进卡片」打开写作台并预选当前文献，表格会出现在 claim 证据候选里（此前入口藏在
  写作台内，用户得先知道）；`Writing` 新增 `initialPaperId`，默认行为不变；
- 空态说明轻量档抽不到表、需要 Docling 重解析，不把「没有表」当错误。

验收：dev 库副本真实文献 8 张表 / 138 单元格 / 3 个合并单元格与原文一致，清洗后
unsafe 节点 0；截图 `docs/assets/tables/01–03`。已知限制：dev 库旧表没有表号
（解析器只留 caption），界面显示「表（未编号）」；当前 worker 也还没有把表号写进
`TableBlock.label_raw/label_norm`，所以重解析同样不会带号——这是后续解析增强，
不在本轮 UI 范围；`复制引用` 先用 `table_id` 兜底。

### 12.34 写作台小修：claim 编辑历史 + 矩阵自定义维度（P0，2026-11-15）

用户明确「暂时省去人工审核，直接小修写作台」。两件都按「不落库的派生/只读」边界做：

#### claim 编辑历史

- 迁移 0016 `claim_revision`：`claim_id`(FK CASCADE) + 旧状态快照（text/section/status/
  source/evidence_json/tags_json/order_idx/paper_version_id）+ `reason`(edited|restored) +
  `edited_by` + `created_at`；dev SQLite 用 `.data/tmp/apply_0016.py` 手工应用。
- `update_claim` 先记旧状态，只在**内容真正变化**时压一份（反复保存同一份不算编辑，
  不涨历史、不抖 updated_at）；恢复版本时先把当前状态压一份（reason=restored），
  再把快照写回 claim，历史链不会因回滚而断。
- API：`GET /api/claims/{id}/revisions`（新→旧）、
  `POST /api/claims/{id}/revisions/{rid}/restore`（返回恢复后的 claim）；
  权限沿用 `can_access_claim` / `can_edit_claim`。
- UI：写作台卡片头部加「历史」；展开可见每条的时间、操作者、章节、旧文字、恢复按钮，
  恢复后自动刷新卡片并 toast。

#### 矩阵自定义维度

- `MATRIX_DIMENSIONS` 定义 10 个维度：原有 7 行（题目/作者/年份来源/摘要/关键发现/图表/
  我的论断）+ 可选 3 行（引用键/条目类型/标签）；`GET /api/matrix/dimensions` 返回清单
  （带 default 标记），`GET /api/matrix?dimensions=` 按请求顺序派生子集，默认仍是原 7 行
  （向后兼容）；未知维度 422。
- UI：矩阵页显示维度 chips（全选/重置默认），勾选后重新生成；空选择前端拦截。

验收：`test_writing_api.py` 新增 2 条（历史创建/幂等/恢复/404、维度清单/子集/顺序/未知 422），
后端全量 **347 passed**、MCP 114、前端 tsc+vite build；截图 `docs/assets/writing/01–03`。
边界：历史只记用户可见编辑字段，不记 owner/时间戳；恢复旧证据时保留其 paper_version_id
（旧版本对象仍在库，可回溯）；矩阵不落库、不跨用户保存维度偏好。

### 12.35 M9.1a：检索从 LIKE 升级为 BM25（2026-11-15）

M9 的第一期。M9 完整清单有四项（检索升级 / 精度量化 / 表格结构增强 / MCP 质量），
本期只做检索升级里的 **BM25** 部分；向量召回留 M9.1b。

- `server/litbench/retrieval.py`：纯函数 BM25（k1=1.2、b=0.75），中英文混合 tokenize
  （英文 `[a-z0-9]+`；中文单字保召回 + 二元组保精度，不依赖 jieba）、字段权重
  （题名/图注/图号 3、作者/刊名 1）、查询连续子串固定加分（保留旧 LIKE 片段命中）。
  不建 FTS 表、不改迁移：SQLite 与 Postgres 行为一致，个人库规模下先取候选再排序。
- `search_figures` 语义路径：从 `caption/title ILIKE` 换成 BM25；返回新增
  `engine`（bm25/like/provenance/browse），`degraded=false`；`semantic_engine="like"`
  只留给评估脚本复现旧基线。出处检索（图号/DOI/citekey/作者+年份）路径完全不动。
- `GET /api/papers?q=`：从「标题/cite_key 子串」升级为 BM25（标题/摘要/作者/cite_key/
  标签），旧子串命中保留；MCP `library.search` 直接走这条，自然语言查询不再需要
  「先拆关键词再搜」的兜底才命中。
- 评估：`scripts/eval_retrieval.py` 固定 20 条真实查询（dev 库 10 篇英文热/流体论文，
  长短句 + 关键词各半），旧 LIKE 基线 vs BM25：

  | 指标 | 旧 LIKE | BM25 |
  | --- | --- | --- |
  | figure hit@1 / hit@5 / MRR | 5% / 5% / 0.05 | 95% / 100% / 0.975 |
  | paper hit@1 / hit@5 / MRR | 5% / 5% / 0.05 | 100% / 100% / 1.0 |

  0 条查询在 BM25 下从「命中」变成「不命中」（`regressions` 为空）。原始明细
  `.data/tmp/eval_retrieval.json`，脚本可复跑。
- 边界：这是**词面**排序，不是 embedding；同义词/跨语言仍不命中。候选集上限
  5000（图）/2000（文献），大库要换 FTS/tsvector 或 ANN。接口契约不变：
  `mode=provenance|semantic`，`engine` 只是新增说明字段。
- M9.1b 前置探测：本机 OpenCode 网关 `/models` 有 47 个 chat 模型、0 个 embedding 模型，
  向量召回需要另配 embedding 端点/本地模型，暂无可验收 provider。后续处理：M9.1b-lite 用 chat 模型做查询改写（§12.40），真正的 embedding 向量召回仍未做。

### 12.36 M9.3a：表格结构化 HTML → LaTeX（2026-11-15）

M9 第三项「表格结构增强」的第一期。M9.3 原计划还包含 MinerU standard / Docling
tableformer 的结构一致性对比（M9.3b），已由 §12.37 完成。

- `server/litbench/table_latex.py`：标准库 `HTMLParser` 解析表格 HTML（不用 bs4），
  `colspan → \multicolumn`、`rowspan → \multirow`；rowspan 的后续行按列留空，
  避免后面的单元格错位；单元格文本做 LaTeX 转义；`ℓ/𝜃/β` 等 Unicode 数学字符
  NFKC + 数学命令（否则默认字体缺字形）；表题优先库里 caption，缺表题不生成
  `\caption`、只 warning，不编造；图片表（无 HTML）422。
- `GET /api/papers/{paper_id}/tables/{table_id}/latex`：返回 `tabular` 片段 +
  可直接粘贴的 `table` 浮动体 + columns/rows/warnings；表必须属于该文献，
  跨文献 table_id 404。
- 前端：文献详情「表格」页签每张表加「复制 LaTeX」，转换后复制并 toast
  （含合并单元格/缺表题提示）。
- 验收：`test_table_latex.py` 7 条（简单表/合并列/合并行+列位/Unicode 数学/缺表题/
  无 HTML/API 404·422）；真机取 dev 库 3 张真实表（含 colspan）+ 1 张合成 rowspan
  表，用 xelatex + ctex + multirow 编译 2 页：`docs/assets/tables/04-latex-export.pdf`
  （`.tex` 同目录），表格页签 UI 截图 `04-latex-copy.png`。
- 边界：不做列宽自动测量/长单元格换行，不生成学校模板；图片表不导出；
  表号缺失时用 table_id 兜底生成 `\label{tab:<id>}`，导出后可在库里补 caption/label
  再重新复制。

### 12.37 M9.3b：表格结构一致性对比（2026-11-15）

M9.3 的第二期。M9.3a 已经能把结构化表格导出 LaTeX，但「结构化」本身来自解析器；
这一期量的是 **Docling TableFormer 与 MinerU `standard` 档对同一张表的结构还原差多少**。
只做测量，不改产品解析路径。

- 模块 `server/litbench/table_struct.py`：HTML 第一张表 → 网格（`colspan/rowspan`
  1..50、展开后行高宽/锚点/占用），3-gram shingle F1（与 `docs/11` OCR 对比同口径）、
  格位占用 IoU、单元格文本完全一致率；配对按「同页 + bbox（IoU 或交集/较小面积 ≥ 0.3）」贪心。
- 脚本 `scripts/eval_table_structure.py`：同一批 PDF 分别跑两个引擎，结果按文件 sha
  （MinerU 再加 tier）缓存，支持 `--engines docling` / `--engines mineru` 分两段跑
  （避免两个引擎同时占内存）和 `--report-only` 重算报告；JSON 明细 + Markdown 报告都在
  `.data/tmp`（不入库）。样本里有两个文件名不同、内容相同的 PDF，按 sha 去重。
- `MineruParser` 增加显式 `tier` 参数：评估传 `standard`，生产默认仍是配置里的 `flash`。
  实测 `standard` 需要约 2GB 模型（`mineru-kit models download --tier standard`），
  middle_json 的 `extensions.mineru.tier` 可核对档位。
- 结果（10 个样本文件去重后 9 篇，逐篇全页）：Docling 27 表 / MinerU 25 表 / 配对 25 对；
  行列完全一致率 0.680、合并结构（span 计数）一致 0.720、格位 IoU 0.912、
  单元格文本完全一致 0.711、3-gram 文本 F1 0.974、完全一致 8 对；Docling 表被配对
  覆盖率 0.926，MinerU 1.000。
- 差异形态（抽查确认是解析器差异，不是指标 bug）：
  - MinerU 会把长单元格按行拆开：CV 论文某表 Docling 6×2、MinerU 16×2（文本 F1 0.971，
    格位 IoU 0.375）；
  - MinerU 会补空列：q-bio 某表 Docling 3 列、MinerU 5 列（MinerU 侧 145 个格子里 70 个是空的）；
  - 漏表：physics.chem-ph 某篇 Docling 出了 1 张 10×2 表，MinerU 同一页没识别成 table；
    CV 某篇 Docling 多出 1 张 2×2 小表，MinerU 没有；
  - 8 对表两边完全一致（cond-mat 1 对、CV 5 对、chem 2 对）。
- 结论：**文本层面高度一致（F1 0.974），结构层面中等一致（行列一致 68%）**。
  Docling 的表格结构更适合作为导出/LaTeX 的来源（本产品现状）；MinerU standard 的
  价值在「同一路径能同时出文本+表格」，但长单元格拆行/补空列会让 LaTeX 列数偏多。
  暂不切换解析路径；列宽自动测量与长单元格换行已由 §12.38（M9.3c）完成。
- 边界：指标只衡量两个解析器**互相一致**，不是准确率——两边同时错会得高分；真值要
  人工标注（M9.2 的 bbox 集是图框，不是表格）。语料是 arXiv 论文（多三线表），
  不能外推到扫描教材。复现与坑见 `docs/09` §0.5/§0.6、`docs/11` 末节。



### 12.38 M9.3c：宽表按页宽自动换行（2026-11-15）

M9.3 的第三期。M9.3a 能导出 LaTeX 了，但 `l` 列不换行：长单元格/多列表格会把表撑出
版心（验收时 dev 库一张 4 列表就溢出 46.5pt，合成宽表溢出 1717pt）。这期让导出自己
决定列格式，用户不用再手改 `\begin{tabular}{lll}`。

- `server/litbench/table_latex.py`：
  - 按单元格文本估算半角宽度（CJK 按 2），每列取内容最大宽度；表格总宽 >78 单位或
    单格 >44 单位时进入 `wrapped` 模式；
  - 列宽按内容比例分配，单列下限取「最长不可断词宽度」（避免 p 列里单词仍然 Overfull），
    总宽给 `\tabcolsep` 留余量（`0.94 - 0.0264×(列数-1)`，封顶 0.55）；
  - 列格式变 `@{}p{0.184\linewidth}…@{}`；`colspan` 用 `p{合并宽}`，`rowspan` 用
    `\parbox{\linewidth}`（p 单元格里 `\linewidth` 就是列宽）；
  - warning：较宽表提示「已按页宽换行」；有超长英文串（>30 字符）提示 p 列不会断词；
    估算「短列/列数太多，p 列也放不下」时建议横排（sidewaystable）、缩小字号或拆表；
  - `table_html_to_latex(..., width="auto"|"plain")`，plain 保留旧行为；返回新增
    `wrapped` / `column_spec`。
- `GET /api/papers/{paper_id}/tables/{table_id}/latex?width=auto|plain`：默认 auto；
  `TableLatexOut` 增加 `wrapped`、`column_spec`；前端「复制 LaTeX」toast 里显示
  「已按页宽自动换行」。
- 验收：`test_table_latex.py` 14 条（短表仍 `ll`、宽表 p 列、比例和留白、plain 强制、
  非法 width、multicolumn/rowspan、放不下提示）；真机取 dev 库「4 列 / 最长 46 字」真实表 +
  1 张合成宽表，xelatex 编译：auto 0 Overfull，plain 分别溢出 46.5pt 与 1717pt；
  产物 `docs/assets/tables/05-width-wrap.pdf/.tex/.png`、`05-width-compare.png`。
- 编译验收脚本 `scripts/verify_table_latex.py`：`prepare --table-id …` 从库里取表生成
  完整 `.tex` + `compile.ps1`/`compile.sh` + meta；agent 在 shell 里执行编译；
  `report` 解析日志报 PDF/页数/Overfull/Underfull/LaTeX 错误，`--max-overfull`
  作为退出码闸门（默认 0）。`--width auto|plain|both` 可同时验收新旧列格式。
- 踩坑：MiKTeX 25 把「未检查更新」记成 major issue，**从 Python `subprocess` 拉起的
  xelatex 会被判失败且不产 PDF**；由 agent/工具侧 shell（PowerShell/bash）直接启动
  则正常。所以验收拆成 prepare（Python 生成 .tex）/ agent 跑编译 / report（Python
  解析）三步，不要试图用 Python 拉起 xelatex。
- 边界：列宽是估算（10pt/A4 版心 455pt），双栏文档或改用大字号时可能仍需手动 `\small`；
  p 列是两端对齐，不做 `\raggedright`；极端宽表（如 14 列短列）无法靠换行解决，只能
  横排/拆表（导出会 warning，不假装成功）；图片表仍 422。


### 12.39 M9.4：MCP 质量——cite.resolve 双档 + golden bbox 集（2026-11-15）

M9 最后一项。之前 MCP 的 `cite.resolve` 只有确定性词面打分：同义改写/跨语言 claim 会
判 insufficient（docs/15 §12.22 已记边界）。这期加一个**可选模型复核档**，并让
`dataset.golden` 能导出图框标注集。

- 服务端：新增 `server/litbench/citation_review.py` + `POST /api/citations/review`。
  请求 `{claim_text, paper_id, block_ids[]}`；先按所属文献校验证据块（跨文献/空块 422），
  取最多 6 块、每块 1200 字、总 6000 字，用已有 LLM 配置（UI > env）让模型判
  supported/insufficient，返回 confidence/reason/provider/model/tokens/latency +
  `prompt_version=cite-review/1`。未配置模型 → 503 `llm_not_configured`，调用失败 → 502。
- MCP `cite.resolve`：新增 `review: bool = false`。词面档逻辑完全不变；`review=true` 时
  对每个有证据块的候选调服务端复核（词面 0 命中也把分数最高的候选块送模型，给同义改写
  一次机会），结果顶层 `tier=lexical|model`；每条带 `verdict_basis`，成功时 `lexical`
  保留词面结论、`model_review` 带 provider/model/confidence/reason；未配置/失败/无证据
  时 `model_review.ok=false` + error_code，退回词面结论，**不假装复核过**。
- `dataset.golden`：服务端新增 `format=bboxes`（`export_golden_bboxes`），MCP 新增
  `bbox_only=true`。bbox 集只保留框回归所需字段：item/paper/version/source_path/page/
  生效 bbox/bbox_source(parsed|override)/parsed+override 原值/image_path/label/review_status；
  人工修正框优先；无框图跳过并计入 `skipped_no_bbox`。schema `litbench.golden.bboxes/1`。
- 验收：server 390 passed（新增 9 条：模型两档/坏 JSON 兜底/未配置 503/调用失败 502/
  跨文献块 422 等 + bbox 导出 1 条）、MCP 123 passed（新增 cite 双档 7 条 + golden bbox 2 条）。
  真机：真实模型复核 supported（confidence 0.99，deepseek-v4.1-flash，438/129 tokens，
  latency 1.4s）；dev 库 `include_pending=true` 的 bbox 集导出 140 条、0 skip。
- 边界：模型复核要花钱/要 key，默认不开；MCP 仍不持有 key（模型调用全在服务端）。
  bbox 集不带 page 尺寸，消费者需按 `source_path` 渲染 PDF 页面自行归一化。
  `include_pending=true` 可导未复核图（做基线/预标注），但正式校准应只用 ok/wrong。


### 12.40 M9.1b-lite：复用 chat 模型做中文查询改写（2026-11-15）

M9.1b 原计划是 BYO embedding 向量召回，但产品决策是「配置一个模型 API 管所有」，
而 chat completion 模型不提供 embedding 接口（本机网关 47 个模型里 0 个 embedding）。
所以这期不做向量，改为**用已配置的同一个 chat 模型做查询改写**：把中文/口语查询改写成
英文论文里会出现的术语，再交给 BM25。它补的是「中文问英文图表」和一部分同义改写，
不是 embedding 相似度，也不改变 ``engine=bm25`` 的事实。

- 新模块 `server/litbench/query_expand.py`：仅当查询含 CJK 时触发（英文 q 不额外烧模型）；
  提示词要求输出 `english_query` + 最多 8 个英文技术词/同义词，保留方法名/材料名/缩写；
  结果按查询文本进程内缓存（默认 256 条），独立 8s 超时；未配置模型/调用失败/输出为空
  一律返回 None，检索静默回退原查询，不抛错、不阻塞。
- `/api/figures`：`classify(pq)=semantic` 且 `should_expand(q)` 时先改写再 BM25；响应新增
  `query_expanded`、`expanded_keywords`、`expansion_provider`、`expansion_model`；hint
  如实写「已改写为英文术语，再走 BM25；不是向量召回」。
- `/api/papers?q=`：同样先改写再 BM25；MCP `library.search` 走该端点，中文查询顺带受益。
- 配置：`QUERY_EXPAND_ENABLED`（默认 true）、`QUERY_EXPAND_TIMEOUT_S`（8）、
  `QUERY_EXPAND_MAX_KEYWORDS`（8）、`QUERY_EXPAND_CACHE_SIZE`（256）。
- 验收：`server/tests/test_query_expand.py` 7 条（中文判定/英文不触发/关键词清洗与缓存/
  mock/异常回退/图检索中英命中对比/文献检索对比）；后端 397 passed。真机 dev 库：
  「多孔介质强化传热」在关闭改写时 figure total 0，开启后 total 131，
  改写词 porous media / heat transfer enhancement / convective heat transfer 等，
  provider openai-chat / deepseek-v4.1-flash。
- 边界：①不是向量召回，英文同义改写（同一英文的换说法）仍不覆盖；②改写词是通用术语，
  BM25 命中会变宽（131/140），排序仍是词面相关度，不等同语义排序；③每次新中文查询多一次
  模型调用（约 1–2s、少量 tokens），重复查询走缓存；④大库候选上限 5000/2000 不变；
  ⑤要真向量仍需另配 embedding provider（或本地多语模型），届时可做 BM25+向量混合排序，
  向量服务不可用回退 BM25 并标 degraded。


### 12.41 M9.2 精度量化放弃，转入 M8（2026-11-15 决策）

用户决定：M9.2（每本抽 10 张图人工核对图号/图框/图注、建 50 张人工 bbox 集、
校准 M3c 自动接受阈值）**不再做**，M9 由此收尾，下一阶段转 M8 写作完整度。
本期只更新进度文档，不动代码。

- 放弃范围：人工抽样标注、`docs/14` 的「有数字」验收、M3c 自动接受阈值校准。
- 保留资产：`scripts/calibrate_bbox.py`（有决策数据就能算）、`/api/dataset/golden`
  完整 golden 与 `format=bboxes` 图框集、`docs/14` 的口径说明；未来若有人工标注可直接复用。
- 影响：M3c 自动接受仍默认关/用拍定阈值，对外不再宣称「图框/图注精度已量化」。
- 下一阶段：`docs/17` §4 M8 写作完整度——CSL 引擎（最终采用 `pandoc --citeproc`，
  见 §12.42）、pandoc 导出 docx/pdf、Crossref/arXiv 元数据补全、草稿大纲与
  引用—图表交叉检查。


### 12.42 M8 第一块：CSL 引用引擎（2026-11-16）

M8 写作完整度的第一块：把「13 种手写模板」升级成可选的 **官方 CSL 样式渲染**，
并打通 CSL JSON 中间格式（下一步 docx/pdf 导出也用它）。

**选型：pandoc --citeproc，不用 citeproc-py。** `docs/17` §4 原计划写的是
citeproc-py，实际对比后改了，原因是 citeproc-py 与 Zotero/Word 用的 citeproc-js
输出有可见差异（同一份数据、同一份官方样式实测）：

| 场景 | citeproc-py 0.12.3 | pandoc 3.14（citeproc-js 行为） |
| --- | --- | --- |
| GB/T 7714-2015，期刊无 DOI | 吞掉 `[J]` 类型标识（只剩「作者. 题名. 刊名, 年…」） | `题名[J]. 刊名…` |
| GB/T 7714-2015，学位论文无 DOI/URL | 吞掉 `[D]` | `题名[D]. 出版地: 学校, 年.` |
| IEEE 引号内标点 | `“Title”, Journal` | `“Title,” Journal` |
| APA 参考文献表排序 | 保持输入顺序（不排序） | 按样式规则排序 |

原因：官方 GB/T 样式把「类型标识 + 载体标识」放在同一个 `cs:group`，citeproc-py
按「组内变量全空就抑制整组」的静态规则处理，没有 DOI/URL 时整组（含 `[J]`/`[D]`）
一起被抑制；citeproc-js 不会。改选 pandoc 还有两个好处：它本来就是下一步
docx/pdf 导出的依赖（GPL-2.0 **只作子进程调用**，不链接，已在 `THIRD_PARTY.md` 登记）；
CSL 输出与 Zotero/Word 一致，用户拿到的条目能直接对上。

**交付内容**

- `server/litbench/csl.py`：Paper → CSL JSON 字段映射（journal/conference/preprint/
  thesis/book/report/other；中文姓名用 `literal` 不拆姓/名；作者直接读原始
  `authors_json`，Zotero 机构作者保持 literal）、pandoc 调用（临时目录 + 固定参数、
  20s 超时）、HTML 参考文献表解析（按 `id="ref-<paper_id>"` 回配，不靠题名字符串猜）、
  `CslError`/`CslUnavailable`、`find_pandoc()`（配置 → PATH → `.data/tools/pandoc/*`）。
- `server/litbench/csl_styles/`：内置 3 个官方样式（GB/T 7714-2015 顺序编码制、
  APA 7、IEEE）+ `README.md`（来源、SHA-256、CC BY-SA 3.0 说明）。template 引擎的
  13 种格式不变。
- API：`POST /api/references/format` 新增 `engine=template|csl`（默认 template）；
  `engine=csl` 只支持上述 3 种样式，其他格式 422；没装 pandoc → 503，
  pandoc 报错/超时 → 502，模板引擎不受影响。新增
  `POST /api/references/csl-json`（同一批 paper_id → CSL JSON，不落库）。
  `GET /api/references/styles` 增加 `csl_available`（每条格式）与
  `csl_engine_available`/`csl_note`（环境状态）。
- 前端（写作台 → References）：格式下方新增「用官方 CSL 样式渲染（pandoc）」开关，
  选中时「带序号」自动禁用并提示「序号由 CSL 样式决定」，结果头显示「CSL 引擎」；
  新增「导出 CSL JSON」按钮（下载 `references.csl.json`）。
- 开发/部署：`scripts/get_pandoc.py`（钉版本 3.14 + SHA-256，下载便携版到
  `.data/tools/pandoc/`，开发机一条命令）；`server/Dockerfile` 用同一版本 +
  SHA-256 装 pandoc（支持 amd64/arm64）。
- 测试：`server/tests/test_csl_references.py` 21 条（字段映射 / CSL JSON / HTML 解析 /
  pandoc 缺失 503 / 未支持格式 422 / 关闭开关 503 / 真实 GB/T、APA 渲染与 id 回配），
  没装 pandoc 的环境自动跳过真渲染用例；全量 **418 passed**（原 397 + 21）。
- 截图：`docs/assets/references/01-csl-gbt.png`（GB/T：`[J/OL]`、`[J]`、`[D]` 都在）、
  `02-csl-apa.png`（APA 按样式排序）、`03-csl-json-toast.png`。

**真机验收（临时库 4 篇：英文期刊 DOI / 中文期刊无 DOI / 中文学位论文 / arXiv 预印本）**

- GB/T：`[1] ZHANG W, LI M. …[J/OL]. … DOI:…`、`[2] 王磊, 张三. …[J]. 工程热物理学报, 2025, 46(2): 321-328.`、
  `[3] 王五. …[D]. 北京: 清华大学, 2025.`、`[4] WANG LEI. …[A/OL]. arXiv, 2026. https://arxiv.org/abs/2603.09607.`
- APA：按样式排序（Wang Lei → Zhang → 王五 → 王磊），DOI/URL 正常。
- IEEE：编号 + `“题名,”` 标点与 Zotero 一致。
- `POST /api/references/csl-json`：4 条，DOI/publisher/类型映射正确。
- UI：勾选 CSL → 生成 4 条 → 切 APA 重算 → 导出 CSL JSON toast「已导出 4 条 CSL JSON」。

**边界（明确不做/不宣称）**

1. 只渲染**参考文献表**，不做正文 in-text citation（`[1]` vs `(Zhang, 2026)` 的插引）；
2. 输出**纯文本**，斜体/小型大写等格式被剥掉（与手写模板一致；Word/PDF 富文本导出
   已在 §12.43 补上）；
3. `engine=csl` 只覆盖 3 种有官方样式的格式；其余 10 种继续走手写模板；
4. 官方样式的行为照单全收：预印本在 GB/T 里是 `[A/OL]`（样式把 CSL `article` 映射为
   档案 A，Zotero 同样）、APA 里中文作者会带 `, &`、学位论文 APA 会带 `[博士学位论文]`；
   这些是样式规则，不是 LitBench 编的；
5. 未装 pandoc 时 CSL 入口 503，不假装渲染成功；Docker 镜像本次**没有重建验证**
   （daemon 未起），随 M8 部署补验一起做；
6. `csl_styles/` 是 CC BY-SA 3.0 数据，不修改内容；换版本要更新 README 的 SHA-256。


### 12.43 M8 第二块：Word / PDF 导出（2026-11-16）

写作台 claim 草稿 → **Word (.docx)** / **PDF**，复用上一块的 CSL 引擎和同一个
pandoc 依赖。目标不是排版器，而是让「素材 → 可交给导师/期刊的草稿」少一次手工搬运。

**链路**

```
claim 卡片（同 /api/writing/export）
  → Markdown（标题 + 章节 + 正文 + [@paper.id] 引用）
  → pandoc --citeproc + 官方 CSL（GB/T 7714-2015 默认）
      → .docx（pandoc 自带 OOXML writer，Word 直接打开）
      → .pdf（--pdf-engine=typst，中文由 typst 字体回退渲染）
```

**PDF 为什么用 typst，不用 xelatex**

不是偏好：API 进程（Python subprocess）拉起 xelatex 时，MiKTeX 25 写不了自己的
`%LOCALAPPDATA%\MiKTeX\miktex\log`（权限/沙箱），直接判失败、不产 PDF；本机实测
pandoc `--pdf-engine=xelatex` 同样失败（缺包/日志权限）。typst 是单文件 Rust 二进制
（MIT），无 TeX 安装/字体数据库依赖，pandoc 3.14 + typst 0.15.1 实测生成中文 PDF：
正文、`[1]` 引用编号、GB/T 参考文献、中文字形都正常。Docker 镜像里同时装
`fonts-noto-cjk`（SIL OFL）提供中文字体。

**交付内容**

- `server/litbench/doc_export.py`：claim → Markdown（标题/章节/引用，引用键统一成
  `[@paper.id]`，claim 里手写的 `[@cite_key]` 会映射；未知键 422 不静默丢引用）
  → pandoc（docx / typst-pdf）；`find_typst()`（配置 → PATH → `.data/tools/typst/*`）、
  `export_capabilities()`、`DocExportError`（422）/`DocExportUnavailable`（503）；
  pandoc 调用放 `asyncio.to_thread`，不阻塞事件循环。
- API：`POST /api/writing/export/document`（二进制响应，中文件名用 RFC 5987；
  允许 `claim_ids`/`paper_ids`/`include_drafts`/`csl_style`，默认 GB/T 7714）；
  `GET /api/writing/export/capabilities`（docx/pdf/pandoc/typst 状态）。
- 写作台「导出」页签：新增「下载 Word（.docx）」和「下载 PDF」两个按钮，
  能力探测失败时置灰并显示原因；标题沿用「标题」输入框。
- 开发/部署：`scripts/get_typst.py`（钉 0.15.1 + SHA-256，GitHub 直连失败自动换
  `ghproxy.net`/`ghfast.top` 镜像）；`server/Dockerfile` 装 typst（amd64/arm64）+
  `fonts-noto-cjk`。
- 测试：`server/tests/test_doc_export.py` 10 条（Markdown 分组/引用改写与未知键 422、
  能力探测、无 claim 422、缺 pandoc/typst 503、不支持的 CSL 样式 422、真实 docx
  内容断言、真实 PDF 文本断言）；全量 **428 passed**（原 418 + 10）。
- 产物/截图：`docs/assets/exports/01-draft.docx`、`02-draft.pdf`（1 页 A4，含标题/
  3 个章节/GB/T 参考文献）、`03-ui-word-export.png`、`04-ui-pdf-export.png`。

**真机验收（临时库：2 篇文献 / 3 张 claim，其中 1 张草稿）**

- `GET /api/writing/export/capabilities` → `{docx: true, pdf: true, pandoc: true, typst: true}`。
- Word：HTTP 200，11,541 字节，`PK` 头；解包 `word/document.xml` 含三段 claim 正文、
  三个章节标题、`ZHANG W` 与 `王五` 的 GB/T 参考文献条目；`X-LitBench-Claims: 3`。
- PDF：HTTP 200，26,606 字节，`%PDF-1.7`；pdfium 抽取文本含正文、`[1]`、`ZHANG W`、`王五`。
- UI：两个按钮可用；点「下载 Word」toast「已生成 'LitBench-写作草稿.docx（Word）」，
  点「下载 PDF」toast 对应 PDF；截图见上。

**边界（明确不做/不宣称）**

1. 只导出 **claim 正文 + 参考文献**：不做图表排版（图/表仍是占位）、不做学校模板/
   封面/页眉页脚/页边距定制；默认 Word 样式和 A4 版心；
2. 正文只来自卡片，不自动写正文；claim 里手写 `[@key]` 必须能在库内解析，
   否则 422（宁可报错也不静默丢引用）；
3. 章节顺序默认按 `claim.section` 排序（当前 DB 里是字母序，和 LaTeX/Markdown 导出
   一致）；要自定义顺序走 `section_order`，UI 暂未暴露（学位论文页签已有章节顺序）；
4. Word/PDF 依赖系统里的 pandoc / typst：缺工具时 503，LaTeX/Markdown/References
   导出不受影响；Docker 镜像已改但**本次未重建验证**（随部署补验）；
5. PDF 用 typst 引擎，不承诺与 xelatex/学校模板的排版完全一致；公式目前按 pandoc
   默认（typst 支持常见数学），复杂 LaTeX 公式可能降级；
6. 生成的是**草稿**，引用编号/参考文献样式由官方 CSL 决定，作者仍需人工校对。


### 12.44 M8 第三块：Crossref / arXiv 元数据补全（2026-11-16）

写作台 References 页签的「补全卷期页」一直纯手填；上传的 PDF 和早期导入经常只有
标题/arXiv id，缺作者、刊名、卷期页、DOI。这一块按 DOI / arXiv id 查回著录字段，
先给 diff、用户确认后才写 `paper` 表。

**链路**

```
POST /api/papers/{id}/metadata/lookup
  auto   → paper.doi → Crossref /works/{doi}
         → paper.arxiv_id → arXiv export API（Atom）
  doi    → 指定 DOI 查 Crossref
  arxiv  → 指定 arXiv id 查 arXiv
  search → Crossref query.bibliographic 题名搜索（最多 5 条候选）
  → 返回字段 diff（current / current_text / suggested / suggested_text / empty / conflict）

POST /api/papers/{id}/metadata/apply
  → 只填空白字段；覆盖已有值必须 overwrite=true；owner/admin 才可写
```

**交付内容**

- `server/litbench/metadata.py`：DOI/arXiv id 清洗、Crossref work → paper 字段映射
  （标题/作者/年份/期刊/卷期页/DOI/entry_type，页码 `54-58` → `54–58`）、arXiv Atom
  解析（标题/作者/年份/journal_ref/doi，venue 兜底 `arXiv`，entry_type=preprint）、
  题名搜索、字段 diff、apply 校验（标题/作者/年份/DOI/arXiv/entry_type 白名单与长度）；
  DOI 不存在 404、网络/解析失败 502、参数错 422、覆盖冲突 409。
- `server/litbench/routers/metadata.py`：`POST /papers/{id}/metadata/lookup`（只读）、
  `POST /papers/{id}/metadata/apply`（写库，需 owner/管理员）；注册进 `main.py`。
- `PaperOut` 增加 `authors_json`（前端要显示作者 diff/补全结果）。
- 前端写作台 References →「补全卷期页」：新增「在线补全（Crossref / arXiv）」、
  「按题名搜索」输入 + 「搜索候选」；diff 行可勾选（缺失字段默认勾选，冲突标
  「已有值」并默认不勾），勾「覆盖已有值」后才允许应用；候选点「用这条看 diff」。
- 配置：`METADATA_LOOKUP_ENABLED`、`CROSSREF_API_BASE`、`ARXIV_API_BASE`、
  `METADATA_TIMEOUT_S`、`CROSSREF_MAILTO`（只进 User-Agent）。
- 测试：`server/tests/test_metadata.py` 15 条（DOI/arXiv 清洗、Crossref/arXiv 映射样本、
  not found、diff、apply 空字段/冲突/非法值/未知字段、HTTP 404/502/422/409/503）；
  全量 **443 passed**（原 428 + 15）。测试用固定 JSON/XML 样本，不依赖外网。

**真机验收（临时库，真实外网）**

- A（`10.1038/nature12373`，标题占位 `Untitled upload`）：auto 查 Crossref 返回
  title/authors/year/venue/volume/issue/pages diff；apply（overwrite=true）后标题变成
  `Nanometre-scale thermometry in a living cell`，8 位作者、2013、Nature、500/7460。
- B（`arXiv:1706.03762`）：auto 查 arXiv → 作者 8 位、2017、venue arXiv；apply 后入库。
- C（无标识、题名可搜）：search 返回 3 条候选（第一条即目标 DOI）；选候选 DOI 再查 diff
  并 apply，字段全部补上。
- UI：打开 References → 补全卷期页 → 选 D（有 DOI、字段空）→ 在线补全显示 7 行 diff
  （标题冲突「已有值」默认不勾，其余 6 行默认勾选）→ 应用选中字段 6 个 → toast
  「已补全 6 个字段」；再选 E 做题名搜索 → 3 条候选 → 「用这条看 diff」正常。
  截图 `docs/assets/metadata/01–04`。

**边界（明确不做/不宣称）**

1. **只读外部 API、不写 Zotero**；lookup 不落库，apply 才写 `paper`，且只有 owner/
   管理员可写（共享文献对成员只读）；
2. 只补著录字段，不抓摘要/正文/参考文献表；Crossref 返回的 `abstract` 暂不使用；
3. 默认只填空白，覆盖人工/Zotero 元数据要显式勾「覆盖已有值」；
4. 依赖外网与第三方服务可用性；Crossref/arXiv 挂了返回 502，不影响本地功能；
5. 题名搜索只查 Crossref 前 5 条，不做消歧/自动选第一条，用户必须自己确认候选；
6. 不自动触发（打开页面不会偷偷写库），也不做批量补全（一次一篇，先验证质量）。


### 12.45 M8 第四块：写作大纲 + 引用/图表交叉检查（2026-11-16）

M8 写作完整度的最后一块：把 claim 卡片按章节整理成**可读大纲**，并做两项
确定性检查——正文提到的图/表是否和卡片证据对得上、手写引用键能否在库内解析。

**交付内容**

- `server/litbench/outline.py`：
  - 复用 `thesis._load_claims`（可见性 + claim 归属 + 排序）与章节别名表，把
    `claim.section` 归到默认章节（摘要/绪论/相关工作/方法/实验与结果/结论；
    「引言」「实验结果」「文献综述」等别名归章，未知 section 作为额外章节追加）；
  - 章节内按 `order_idx` 排序；输出每张卡片的正文/状态/所属文献/cite_key/证据标签；
  - **图表检查**：正则识别正文里的「图 3 / 图3 / Fig. 3 / Figure 5(a) / 表 2 /
    Table 3」以及 `\ref{fig:3}`/`\ref{tab:4}`，与卡片的 figure/table 证据标签比对；
    正文提到但证据里没有 → issue「正文提到图 3，但本卡证据对不上（证据：图 4）」；
    有证据但正文没提 → notes；
  - **引用检查**：`\cite{key}`/`\citep` 等与可见文献库比对，解析不了报 unknown；
  - 生成 Markdown 大纲（章节 + 卡片 + 证据 + ⚠ 检查项），供复制/下载；
  - 只读派生、不落库；没有卡片 422。
- API：`POST /api/writing/outline`（claim_ids/paper_ids/include_drafts/section_order），
  `OutlineOut` 带 sections/issue_count/markdown。
- 前端写作台新增「大纲」页签：首次进入自动生成；可切「包含草稿卡片」，显示
  卡片数/章节数/检查提示数，章节分组 + 卡片 + ⚠ 提示 + notes；「复制大纲」
  「下载 Markdown」。
- 测试：`server/tests/test_outline.py` 5 条（图表/引用正则变体、章节归并与排序、
  图表不匹配 issues、引用 unknown、include_drafts/section_order、空库 422、HTTP）；
  全量 **448 passed**（原 443 + 5）。

**真机验收（临时库：1 篇文献 / 5 张 claim，含 1 张草稿）**

- `POST /api/writing/outline` → 4 个章节：绪论/方法/实验与结果/讨论，5 张卡片；
- 实验与结果章节内按 `order_idx` 排序（图 4 匹配在前、图 3 不匹配在后）；
- 3 条检查提示：草稿「图 5 无图证据」、已认可卡「正文图 3 vs 证据图 4」、
  「引用键 missing-key 找不到」；有证据但正文没提的卡片给 notes；
- `include_drafts=false` → 4 张、方法章消失；
- UI 大纲页签自动生成、取消「包含草稿」自动重算；截图
  `docs/assets/outline/01-outline.png`、`02-outline-approved-only.png`。

**边界（明确不做/不宣称）**

1. 只对**已有卡片**整理，不写正文、不改 claim；排序只是派生视图（要持久改顺序
   仍走 claim 的 `order_idx`）；
2. 检查是**词面确定性比对**，不做语义判断（「图 3 是否支持这个结论」不归它管）；
3. 图表编号识别覆盖常见中英文写法；子图 `图 3(a)` 与 `图 3` 会按不同编号比对，
   可能提示不一致（保守报出，不猜）；
4. 引用检查只认 `\cite` 系列 LaTeX 命令，纯文本「张三等（2020）」不解析；
5. 大纲 Markdown 是给用户复制/继续编辑的骨架，不是成稿；图表不自动排版。


### 12.46 外部审查前自修批 A（2026-11-16）

M8 交付后、外部强模型核验前，先做了一轮独立审查 + 自修（子 agent 只读审查 diff
`138802d..f5addf5`，主 agent 复现/修复/加测试）。修掉的实质问题：

1. **pandoc 本地文件读取 / SSRF（高）**：Word/PDF 导出把 claim 正文原样拼进
   Markdown，pandoc 默认会读取 `![](...)` 引用的本地/远程资源；实测可把服务器
   本地 PNG 嵌进 docx。CSL 渲染与文档导出命令都加 `--sandbox`（只允许命令行显式
   传入的 bibliography/CSL/输出文件），并加回归测试：claim 里嵌本地图片 → 导出
   仍 200，但 docx 里没有 `word/media/`。
2. **`\cite{}` 在 Word/PDF 静默消失（高）**：导出只认 `[@...]`，项目其它地方习惯
   `\cite{key}`，pandoc 会把 raw TeX 丢掉、参考文献也不出现。现在 `\cite{k1,k2}`
   会改写成 `[@paper_id; @paper_id]`；同时支持 `[@a; @b]` 多键与 `[@a, p. 3]`
   locator；未知键仍 422。测试覆盖 docx 里不再出现 `\cite` 且参考文献条目在。
3. **`paper_ids` 额外文献在引用改写之后才加入（高）**：手动引用可见文献仍 422，
   且未 cite 的额外文献不会进参考文献表。现在先合并额外文献再改写引用，
   front matter 加 `nocite: "@*"`，被选进范围的文献都会出现在参考文献表。
4. **大纲引用索引截断（中）**：`thesis._load_visible_papers` 最多 300 篇，库大了
   会把第 301 篇后的 `\cite` 误报 unknown。新增 `writing.visible_paper_index`
   （只查 id/cite_key/doi/arxiv/title，不截断），outline 与 doc export 都用它；
   加 301 篇边界测试。
5. **图表正则误报（中）**：英文 `configure 3` / `stable 3` 和中文 `图表 2` /
   `发表 2` 里的 fig/tab/表会被误判成图表引用；加前后文守卫，`_evidence_number`
   无编号时返回空串；测试覆盖英文/中文复合词与无编号证据。
6. **导出静默截断 500 张（中）**：`_load_export_claims` 多取一条，doc export/
   LaTeX/Markdown 导出超过上限时明确 422（不再静默少内容）。
7. **导出可见性不一致（低-中）**：doc export 的 claim 查询补 `visible_paper_condition`
   （撤共享后不能靠导出拿元数据）；`/writing/check-citations` 原先查全库、
   会把别人私有库的 cite_key/标题解析出来，改为只对照当前用户可见文献，并加隐私
   测试。
8. **metadata 加固（中）**：外部响应 >5MB 拒绝；作者姓名单项 ≤200 字；用
   `httpx.MockTransport` 补 404/500/非 JSON/超时/XML 解析错误/超大响应测试；
   补 auto 优先 DOI、arxiv、无标识 422 的编排测试。
9. **部署/CI（中）**：Dockerfile 的 GitHub 下载改为「直连 → ghproxy.net →
   ghfast.top」+ 校验 SHA-256 + `--connect-timeout/--max-time`（本机 Docker 构建
   实测直连 SSL EOF/卡住）；架构探测改为 `TARGETARCH` 或 `uname -m` 兜底；
   去掉不需要的 `fc-cache`（未装 fontconfig）。CI backend job 缓存并安装
   pandoc 3.14 / typst 0.15.1 + `fonts-noto-cjk`，真实渲染测试不再在 CI 全部跳过。
10. **工具查找顺序**：项目内便携版 pandoc/typst 优先于 PATH（避免系统旧版
    pandoc 2.x 让 `--sandbox` 报 unknown）；`compose.yaml`/`.env.example` 补
    M8 配置项（PANDOC_PATH/TYPST_PATH/导出开关/PDF_MAIN_FONT/CROSSREF_MAILTO）。

**验证**：后端 **467 passed**（原 448 + 19）、ruff E/F/I 新改动文件通过、
`docker compose config` 通过；Docker 镜像本机**未能重建完成**——GitHub 直连与
ghproxy/ghfast 镜像在本机 WSL 网络下多次 SSL EOF/超时（已加镜像回退 + SHA-256 +
连接/总超时 + `uname -m` 架构兜底），留部署环境补验。**仍未做**：GitHub Actions
实跑（无远端触发）、真实局域网第二台机验收；这些留到部署补验。

---

## 12.47 自修批 B：auth 打开时的权限矩阵 + 文档计数一致性（2026-11-16）

> 触发：用户指定「下一轮自修聚焦 auth 打开时的权限测试补全 + 文档计数一致性」。
> 做法与批 A 一致：独立只读审查（两个 agent：路由权限逐条清单 / 文档计数盘点）+ 自己复现、
> 修、补测试；不碰 Zotero，不打印模型 key。

### 12.47.1 审查发现（auth 打开后「登录成员可绕过」的实质缺口）

审查覆盖全部 15 个 router + `team.py`/`auth.py`/`main.py`，按「接收资源 id 的端点是否做
可见性/写权限校验」逐条过。真正要修的不是 401（中间件已统一拦截），而是**登录之后的授权**：

1. **共享只读被写成链绕过（高）**：`POST /proposals` 无 `current_user`、无 `ensure_*`，
   任何成员可对任意 `figure_id` 投递提案（并把 owner 的 pending 置 superseded）；
   `POST /proposals/decide-batch` 直接 `decide_proposal`，不走单条入口的
   `ensure_figure_edit` → 可采纳/拒绝别人的提案、改 owner 的图号/图注/复核状态。
2. **`POST /proposals/auto-accept` 全库扫描（高）**：任何成员能让全库满足条件的 pending
   被自动接受，并拿到别人 `figure_id` 与被写值。
3. **`GET /dataset/golden` 全库导出（高）**：无 user 过滤，golden 条目带 `paper_title`/
   `caption`/`review_note`/`source_path`/`image_path`，成员可绕过私有库拿到全库标注。
4. **`/api/settings/llm*` 无角色校验（高）**：全局单文件配置；成员可 PUT/DELETE 改写
   所有人用的模型 endpoint，`/llm/test` 还会用「已存 key + 请求体里的 base_url」发真实请求
   （可读出/外发管理员保存的 API key + SSRF）。
5. **`/api/tasks*`、`/api/audit` 无 scope（高-中）**：成员可跨用户排队/领取/完成 agent 任务
   （worker 会真的跑模型、写图字段），可读全库审计（含图注/图号/复核备注）。
6. **`POST /import/zotero` 服务端路径/URL 由请求体控制（高）**：成员可让服务器读任意本地
   JSON/sqlite、请求任意 `api_base`（SSRF）；`GET /import/runs` 也是全局。
7. **`GET /jobs`、`/jobs/{id}` 全库（中）**：暴露别人解析任务的版本 id/失败原因。
8. **404-vs-403 不一致（中）**：图写入口（review/label/group）用 `ensure_paper_writable`
   只判写不判可见 → 看不见返回 403（可确认存在）；claims PATCH/DELETE 同样；share/unshare
   先裸 `session.get`；`DELETE /workgroups/{id}/members/{uid}` 未先判组成员（可枚举成员）。
9. **系统主体写入变孤儿（中）**：内部令牌（system）上传的 paper/claim/collection 写成
   `owner_user_id="system"`，普通私有列表只认 owner=我/NULL → 永远看不见，但按 id 又能访问。
10. **审计 actor 取自请求体（中）**：`reviewer`/`actor` 字段可伪造，审计记录不可信。
11. **共享文献重复上传会重解析（中）**：成员对共享只读文献重复上传同一 PDF，会新建 parse
    job，worker 清空该版本图/块并删 pending 提案。
12. **管理员最后一个账号可自停用/自降级（低-中）**：会把团队锁在管理面外。
13. **会话/测试盲区（低）**：`test_auth.py` docstring 声称「停用后旧 session 立即失效」，
    但测试先 logout 再停用，从未验证旧 cookie 被拒；匿名 401 只测了 `/api/papers`。

### 12.47.2 修复

- **可见性/写权限**：
  - `POST /proposals`：加 `current_user` + `ensure_figure_edit`（不可见 404、共享只读 403）。
  - `POST /proposals/decide-batch`：逐条 `get_proposal` + `ensure_figure_edit`，失败进
    `failed` 列表，不整批失败；不再能绕过单条入口。
  - `POST /proposals/auto-accept`、`/tasks*`、`/audit`、`/import/zotero`（服务端路径/API）、
    `/import/runs`、`/settings/llm` 的写/测端点：新增
    `auth.admin_when_enabled` 依赖——认证关闭时放行（单机旧行为不变），认证打开后仅
    admin/system。
  - `GET /dataset/golden`（含 `format=bboxes`）：加 `current_user`，`_figure_rows` 叠加
    `visible_paper_condition`（MCP 内部令牌 system 仍是全库）。
  - `GET /jobs`、`/jobs/{id}`：按 `paper_version → paper` 可见性过滤；不存在/不可见统一 404。
  - `GET /proposals/stats`：计数 join 到可见文献，徽标不再算别人私有库。
  - `/writing/outline`：显式 `paper_ids`/`claim_ids` 先过可见性，与 export/thesis 一致 404。
  - 图写入口（review/label/group）改用 `ensure_figure_edit`（不可见 404 / 可见不可写 403）；
    claims PATCH/DELETE/restore 先 `can_access_claim` 404 再判编辑 403。
  - `POST/DELETE /papers/{id}/share`：先 `ensure_paper_access`（看不见 404）；
    `DELETE /workgroups/{id}/members/{uid}` 先 `require_workgroup_member`（非成员 404）。
  - 图图片响应 `Cache-Control` 从 `public` 改 `private`（与 PDF 一致）。
- **数据归属与审计**：新增 `auth.owner_id_for_write`——system/认证关闭写入的 owner 落
  `NULL`（legacy 公共语料），不再产生「按 id 能访问、列表看不见」的孤儿；图 review/label
  的 `reviewed_by` 与审计 actor 在认证打开时强制取当前用户（body 里的 reviewer 只作单机兼容）。
- **会话/管理员**：停用账号后旧 session 由 `resolve_session` 的 `is_active` 检查立即失效；
  补「最后一个管理员不能自停用/自降级」422；移除 `_PUBLIC_API_PATHS` 里的死项 `/api/redoc`。
- **重复上传**：去重分支只有 `can_edit_paper` 时才排重解析 job；共享只读成员重复上传返回
  `deduplicated=true` 且 `job_id=""`（前端把空 job 视为已完成，不再轮询）。
- **文档计数**：新增 `scripts/check_doc_counts.py`，只校验「当前基线」锚点（docs/09 头部、
  docs/17 头部、README 当前基线/命令注释、下一个迁移号、表数、CSL 样式数），历史小节不碰；
  CI 新增 `docs` job。本次同时修正 docs/09 的旧「当前」措辞/数字、README 的 M9.4 旧基线、
  docs/17 头部 362/114、§16 交接模板等（见 §12.47.4）。

### 12.47.3 测试与验证

- 新增 `server/tests/test_auth_permissions.py`（12 条）：匿名 401 矩阵；私有库派生接口
  404；共享只读写入口全拦截（提案/批量采纳/图号/复核/claim/share）；管理面成员 403；
  golden/jobs/proposal stats 可见性；审计 actor 强制当前用户；共享文献重复上传不重解析；
  最后管理员保护；metadata apply owner；文档导出 owner happy path。
- `test_auth.py` +3：cookie 属性（HttpOnly/SameSite/Secure 开关）、停用账号旧 session
  立刻 401、成员不能 PATCH 用户；`test_internal_token.py` +1：system 上传落 legacy 且
  管理员列表可见。
- 修正 `test_team_workgroups.py` 一条旧断言（不可见 claim 现在 404，不是 403）。
- **验证**：后端 **483 passed**（467 + 16）、MCP **123 passed**、前端 `tsc + vite build`
  通过、改动文件 ruff E/F/I 通过、`scripts/check_doc_counts.py` 通过。
- **仍未做/留部署补验**：Docker 镜像重建（本机网络仍卡 GitHub/镜像 SSL）、GitHub Actions
  实跑（含新 docs job）、第二台物理机 LAN 验收、正式证书 80→443。

### 12.47.4 边界与已知取舍

- **管理面收紧是产品决策**：auth 打开后，agent 任务页签/审计/自动接受/导入历史/模型配置
  写操作只给管理员；普通成员仍可用图级 AI 复核（走 `ensure_figure_edit` 的单图入口），
  但批量任务面板会 403（前端提示即可，后续可做「按可见范围」的成员版队列）。
- **admin 的列表语义**：`/api/papers`、`/api/figures`、`/api/collections` 仍是「只看自己+
  legacy」（管理面单独收紧）；`/api/proposals` 保持全局（人审队列是管理面）。这条契约写进
  `test_auth_permissions.py` 的断言与本节，避免以后误改。
- **未修的低风险项**：内部令牌无最小长度校验（部署配置约束）；登录无速率限制（反代层做）；
  cookie 只有 `SameSite=Lax` 无 CSRF token（同站前端没问题，跨站前端要补）；`import/runs`
  与 golden 的「按用户维度」尚未模型化（v1 用可见性/管理员近似）。

---

## 12.48 自修批 C：工具链收敛与版本门槛 + 审查残留项（2026-11-16）

> 触发：用户要求「打包分发先等 SOTA 审查，继续推进自修/修复」。本批处理前两批审查
> 留下、且能在外部强模型审查前稳定消化的残留项，不新增产品功能。

### 12.48.1 改了什么

1. **工具查找去重**：`csl.py` 与 `doc_export.py` 原来各有一份「配置 → 项目内钉版本 →
   PATH」的 pandoc/typst 查找；收敛到新模块 `server/litbench/toolchain.py`
   （`find_tool` / `tool_version` / `MIN_PANDOC_VERSION`），两边共用。以后改查找优先级
   只改一处，避免「一个改了另一个没改」。
2. **pandoc 版本门槛**：新增 `csl.pandoc_version()` / `pandoc_is_supported()`；
   `find_pandoc` 仍只负责定位，CSL 渲染与 Word/PDF 导出在入口检查 **pandoc ≥ 3.0**
   （`--sandbox` / 现行 CLI 行为），版本过低或读不出版本时给可读的 503/说明，
   13 种手写模板与其它导出不受影响；`/references/styles`、`/writing/export/capabilities`
   的可用性说明也带上「版本过低」原因。版本号读取按「路径 + mtime」缓存，不重复 fork。
3. **删除死字段**：`CslStyle.locale` 从未被使用（locale 由 pandoc 内置样式处理），
   去掉字段，避免「看起来有本地化但实际没生效」。
4. **summary 审计 actor**：`PATCH /summaries/{id}`、`publish`、`revisions/{id}/restore`
   在认证打开时强制 `edited_by = 当前登录用户`（此前 PATCH 直接收 body.actor，
   可伪造他人名字）；单机模式仍兼容 body 里的 actor。
5. **内部令牌强度**：`INTERNAL_API_TOKEN` 非空但少于 16 字符时启动即 ValidationError
   （它等价于全库 system 权限，不能静默接受弱令牌）；空字符串仍表示不启用。
6. **MCP 回归**：`test_internal_token.py` 补 system 内部令牌访问任务/审计/golden/
   自动接受的断言——批 B 把这些面收紧为管理员后，MCP 的 `task.*` 与 `dataset.golden`
   仍然可用（system 是超管），防止下次收紧时误伤 MCP。

### 12.48.2 测试与验证

- 新增 `server/tests/test_toolchain.py` 4 条：版本解析与缓存、坏输出/非零退出、
  旧 pandoc 被 CSL/导出拒绝、现代 pandoc 放行。
- `test_auth_permissions.py` +1：摘要编辑的 body actor 伪造被当前用户覆盖。
- `test_internal_token.py` +2：system 令牌可访问批 B 收紧的管理面；内部令牌最小长度。
- 顺带修 `test_csl_references.py` / `test_doc_export.py` 两条针对旧私有常量的
  monkeypatch（`_PROJECT_ROOT` / `shutil` 现在在 `toolchain` 模块）。
- **验证**：后端 **490 passed**（483 + 7）、MCP **123 passed**、前端
  `tsc + vite build` 通过（本批无前端改动）、改动文件 ruff E/F/I 通过、
  `scripts/check_doc_counts.py` 通过。
- **仍未做/留部署补验**：Docker 镜像重建（本机网络 SSL EOF/超时）、GitHub Actions
  实跑（含 docs job）、第二台物理机 LAN 验收、正式证书 80→443；登录限速与 CSRF token
  按部署层/后续处理。

### 12.48.3 不适用条件

- 版本门槛假定项目使用 pandoc 3.x（Dockerfile/`scripts/get_pandoc.py` 钉 3.14）；
  pandoc 3.0 以下一律视为不可用，不再尝试兼容。
- 工具版本检查依赖目标环境能执行 `pandoc --version`；沙箱/只读挂载导致无法执行时，
  CSL/导出会按「无法读取版本」不可用（宁可明确报错，不在运行期才炸）。
- 内部令牌长度规则只防弱配置，不防泄露；令牌轮换、IP 白名单、登录限速仍是部署层问题。

## 12.49 SOTA 审查高/中危修复批（2026-11-16）

> 触发：按 `SOTA-REVIEW-GUIDE.md` 完成全库代码审查（F1–F21），用户拍板「先把高危和
> 中危修掉，修完更新开发文档」。本批修 6 条高/中危（F12/F14/F1/F2/F15/F10），
> 不新增产品功能；低危项（F3–F9/F11/F13/F16–F21 等）留后续批次。

### 12.49.1 改了什么

1. **F12（高）Zotero 导入 owner 隔离**：`zotero_key` / `zotero_item_key` 是全局唯一键，
   早期 upsert 不查归属——成员 B 上传同一份 Zotero 导出可改写成员 A 的私有文献/集合，
   甚至给 A 的文献注入 PDF 版本。`import_zotero` 增加 `owner_user_id` / `is_superuser`
   参数：upsert 只命中调用者自己的行（DOI 兜底匹配同样只匹配自己的），foreign 行计入
   `papers_skipped_foreign` / `collections_skipped_foreign` 跳过；superuser 与
   `can_edit_paper` 同口径，可维护全部（含 legacy NULL-owner）。
2. **F10（中）Zotero 文件上传先限流再读**：`POST /import/zotero/file` 原来先把整个
   上传读进内存再查大小；改为边读边查（1MB  chunks），超过 50MB 立即 413。
   网络源 `POST /import/zotero` 仍仅管理员（SSRF 面）。
3. **F14（高）重解析后写作卡证据重锚**：`persist` 先删后插，block/figure/table 全换
   UUID，claim 证据全部悬空。`reparse.py` 快照/重锚扩展到 claim：block 按
   `(page, 归一化文本)`、figure 按 `cite_key`（退化 `(page, caption)`）、table 按
   `(page, caption/html)`、summary 按 id 存在性（摘要可能因失配被删）。映射不回来的
   证据剔除并置新列 `claim.evidence_stale`（迁移 **0017**），**卡片永不删除**；
   用户重新提交证据后标记清除；前端写作卡显示「证据失效」徽标。
   `persist` 里 table 插入移到 reconcile 之前（table 证据也要重锚）。
4. **F1（中）对比矩阵 claim 泄露**：`build_matrix` 的 claim 查询无 owner 过滤——
   共享文献进矩阵时，别人挂在上面的私有卡片跟着进「我的论断」行。增加 `user` 参数，
   claim 查询加 `private_claim_condition(user)`。
5. **F2（中）NULL-owner 旧卡跨库泄露**：`can_access_claim` 对 NULL-owner 一律放行，
   团队模式下任何人拿到 id 就能读别人私有文献上的旧卡（写入口还用 403 暴露存在性）。
   新增 `team.ensure_claim_access`：owner/管理员直通，NULL-owner 旧卡补一次文献可见性，
   看不见统一 404；get/update/delete/revisions/restore/outline 六个按 id 入口统一走它。
   `list_claims` 改为 `user` 参数：自己的卡 + **可见文献上**的 NULL-owner 旧卡。
   写权限不变（旧卡仍只有管理员能维护）。
6. **F15（中）重解析丢提案回滚保险**：`_PROPOSAL_FIELDS` 快照漏了
   `before_json` / `auto_accepted` / `rolled_back_at` / `rolled_back_by`，重解析后
   「一键回滚」永远报「没有 before 快照」。快照与重建 kwargs 补齐这 4 个字段。

### 12.49.2 测试与验证

- 新增 `test_zotero_owner_isolation.py` 5 条（F12×4 + F10×1）、
  `test_reparse_claims.py` 4 条（F14：重锚/全失配/摘要被删/重新选证清标记）、
  `test_claim_visibility.py` 3 条（F1 矩阵隔离 + F2 私有/共享旧卡）；
  `test_reparse_ai_products.py` +1（F15：采纳→重解析→回滚→再重解析状态保留）。
- **验证**：后端 **503 passed**（490 + 13）、MCP **123 passed**、前端
  `tsc + vite build` 通过（Writing.tsx 徽标 + types.ts 字段）、
  `scripts/check_doc_counts.py` 通过（迁移 0001–0017，下一个 0018）。
- 环境注记：本机 shell `NO_PROXY` 含 `[::1]` 会让 httpx 0.28.1 构造 AsyncClient 崩溃
  （Invalid port），MCP 测试需 `NO_PROXY= no_proxy=` 前缀——环境问题，非代码回归。

### 12.49.3 不适用条件 / 留后续

- 低危项未动：F3 system owner 过滤口径、F5 LaTeX 转义顺序、F6 恢复历史版本不重验证据、
  F7 cite_key 兜底碰撞、F8 管理员重置不踢会话、F9 并发 setup、F11 agent 审计 actor、
  F13 thesis 无 claim 上限、F16 回滚盲恢复、F17 IntegrityError、F18 worker 心跳、
  F19 fail/recover 竞态、F20 迁移一致性测试忽略唯一/外键/类型、F21 sweep 回滚口径等。
- `evidence_stale` 只标「重解析导致的失配」；恢复历史版本（F6）可能再次引入悬空证据，
  该路径不在本批范围。
- Zotero 隔离按「调用者自己的行」判定；跨成员合并同一文献仍是产品上未开放的能力。

## 12.50 轻量 OCR + 视觉模型交叉校验（2026-11-17，Web 开发版）

### 12.50.1 决策

个人版不再把 Docling / MinerU 这类重 OCR 模型作为默认解析路径；改为：

1. 有文字层：pdfium（零模型，保持现状）；
2. 无文字层/扫描页：RapidOCR PP-OCR mobile（Apache-2.0，`rapidocr` 自带权重）逐页识别，
   文字行连同 bbox 直接写入 `block`（`engine=liteocr`）；
3. OCR 页再交给用户配置的**视觉大模型**逐页校对：
   - 无出入：直接采用 OCR 结果，不产生提案；
   - 有出入：写 `ocr_proposal`，阅读页显示「OCR / 大模型修订」对照，由用户决定；
4. 全程由 worker 自动执行，上传后用户只需要等待；提案处理完前 OCR 结果已经可读。

### 12.50.2 代码落点

- `server/litbench/lite_ocr.py`：RapidOCR 页面识别、合并 ParsedDocument、渲染给模型的整页 JPEG；
- `server/litbench/ocr_cross_validate.py`：视觉模型逐页校对（结构化 JSON：same / corrected_text / differences）；
- `server/litbench/models.py::OcrProposal` + `server/migrations/versions/0018_ocr_proposal.py`；
- `server/litbench/workers/run.py`：parse 流程里自动执行 lite OCR → 视觉校验 → 提案；
- `server/litbench/routers/ocr_review.py`：`GET /api/papers/{id}/ocr-proposals`、
  `POST /api/ocr-proposals/{id}/decide`（accept_ocr / accept_model / reject）；
- `web/src/PaperReader.tsx`：阅读页顶部的 OCR 校对卡片；接受模型修订会替换该页 OCR 文字块；
- `server/requirements-ocr-lite.txt`：`rapidocr` + `onnxruntime` 可选依赖。

### 12.50.3 配置

```powershell
$env:LITE_OCR_ENABLED='1'       # 默认开；测试里 conftest 默认关
$env:LITE_OCR_DPI='200'
$env:LITE_OCR_MAX_PAGES='80'
$env:OCR_CROSS_VALIDATE='1'     # 有视觉模型才真正调用
$env:OCR_CROSS_VALIDATE_MAX_PAGES='80'
$env:OCR_BACKEND=''             # 不再排 MinerU/Docling 子 job
$env:OCR_CROSS_ENABLED='0'      # 旧的图注双 OCR 校核可保留给重后端
```

### 12.50.4 测试与验证

- 新增 `test_lite_ocr.py`、`test_ocr_cross_validate.py`、`test_ocr_review.py`、`test_worker_lite_ocr.py`；
- 后端全量：**510 passed**；
- 真实扫描件（PIL 渲染文字成图 → PDF）上传后自动 OCR：
  - worker 识别 1 页、9 个文字块，`/api/papers/{id}/text` 可读；
  - 当前配置的 `deepseek-v4.1-flash` 不接受图片，视觉校验会按设计跳过并保留 OCR 结果。
- 前端 `npm run build`（tsc + vite）通过。

### 12.50.5 边界 / 已知缺口

- 只补**文字层**；扫描页的图/表结构暂不自动重建（下一步可让视觉模型输出 table JSON，或在有文字层时复用 pdfium 图区规则）；
- 视觉校验要求配置**支持图片输入**的模型；不支持/未配置时保留 OCR，不生成提案；
- 现有桌面安装包还没带 `rapidocr/onnxruntime` 轮子；要打包个人版时需加这两个依赖（Apache-2.0/MIT），
  并让 `desktop/main.js` 的 `LITE_OCR_ENABLED=1` 生效（已写入启动环境）；
- OCR 行级 bbox 是阅读顺序的近似，表格单元格/跨栏阅读顺序仍以视觉模型补强为主。
