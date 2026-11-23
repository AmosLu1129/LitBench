# SPDX-License-Identifier: AGPL-3.0-or-later
"""运行时配置。全部走环境变量，密钥不写进代码。"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://litbench:litbench@localhost:5432/litbench"

    storage_dir: Path = Path("./.data")
    max_upload_mb: int = 200

    # 前端开发端口 + 部署端口
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    # 解析后端：pdfium（默认，轻量）| docling（增强档）| mineru（需独立容器）
    parser_backend: str = "pdfium"
    mineru_url: str | None = None

    # 是否为每张图渲染缩略图（整页渲染 + 裁剪）。关掉能省 10–20% 解析时间，
    # 但界面上的图卡片会全部变成「无图片」——只有跑指标脚本时才该关。
    render_figure_images: bool = True

    # 按需升级：轻量档解析完发现「看到了图号但没定位到图」/「图注被截断」时，
    # 自动再排一次增强档（默认 Docling）。置空字符串即关闭。
    escalate_backend: str = "docling"
    escalate_min_missing: int = 1  # 至少缺几个图号才升级
    escalate_min_caption_chars: int = 40  # 图注平均字数低于此值算没读懂

    # OCR（没有文字层的扫描件）：mineru | docling | 空字符串=关闭。
    # MinerU 是 AGPL：只能通过独立 venv 的 CLI 子进程调用，**绝不 import 进主进程**。
    ocr_backend: str = "mineru"
    mineru_cmd: str = r"C:\mineru-env\Scripts\mineru-kit.exe"
    mineru_workdir: Path = Path(r"C:\litbench\.data\mineru-work")
    mineru_home: Path = Path(r"C:\litbench\.data\mineru-home")
    mineru_tier: str = "flash"  # flash|basic|standard|advanced
    mineru_model_source: str = "modelscope"
    mineru_timeout_s: int = 3600
    # docling 走 OCR 时用 RapidOCR（主镜像里装 onnxruntime 即可，MIT）
    docling_ocr_lang: str = "chinese"

    # 双 OCR 交叉校核（阶段 2，见 docs/14）：RapidOCR（PP-OCRv4，Apache-2.0）对图注
    # 小图做第二套识别；文字层质量低/图注有噪声时，以两套 OCR 的共识为准。
    ocr_cross_enabled: bool = True
    ocr_cross_dpi: int = 200
    text_quality_min: float = 0.8  # 低于它认为文字层不可信，走双 OCR 校核

    # 轻量 OCR（RapidOCR PP-OCR mobile）：只补没有文字层的扫描页，不需要装
    # Docling/MinerU；桌面个人版默认开启，重后端 OCR 仍然可以并行存在。
    lite_ocr_enabled: bool = True
    lite_ocr_dpi: int = 200
    lite_ocr_max_pages: int = 80
    # 轻量 OCR 之后用已配置的视觉模型逐页校对；无模型/调用失败时保留 OCR 结果。
    ocr_cross_validate: bool = True
    ocr_cross_validate_max_pages: int = 80

    # 解析后过滤：教材里「扫码看课件」的独立二维码会被版面模型当成图片块。
    # 关掉它二维码会以「图」的身份进库（实测 5 本教材混入 4 张）。
    filter_qr_figures: bool = True

    # Zotero 只读导入（M1，docs/15 D1）：Zotero 继续当文献库主人，这边只镜像。
    # api = Zotero 7 local API（Zotero 开着时可用）；sqlite = 只读打开 zotero.sqlite。
    zotero_api_base: str = "http://127.0.0.1:23119/api/users/0"
    zotero_sqlite: Path | None = None  # 为空时自动探测 ~/Zotero/zotero.sqlite
    # 附件目录（stored attachments）。为空时：sqlite 源用 db 同级 storage/，
    # api 源用 ~/Zotero/storage。B 方案（自动导入 Zotero 附件 PDF）会用。
    zotero_storage: Path | None = None

    # --- 团队共享（2026-11-15，M7）：本地账号 / 会话 --------------------------
    # 认证默认关闭：工作组与共享还没交付完，先把地基搭好、单机开发流程不变；
    # Docker/局域网部署时设 LITBENCH_AUTH=1，再初始化管理员。
    auth_enabled: bool = False
    auth_session_days: int = 14
    # 反向代理 HTTPS 时设为 true，cookie 带 Secure
    auth_cookie_secure: bool = False
    auth_cookie_name: str = "litbench_session"
    # MCP 内部服务令牌（2026-11-15，docs/17 §11.1）：认证打开后 MCP 仍要能调 /api/*。
    # 请求头 ``X-LitBench-Internal-Token`` 与本值一致时，服务端把它当作 system 主体
    # （全库只读/管理，不落库）。空字符串 = 不启用。
    # 部署时 api 与 mcp 注入**同一个值**；值只放部署 env，不进仓库/日志。
    internal_api_token: str = ""

    @field_validator("internal_api_token")
    @classmethod
    def _validate_internal_token(cls, value: str) -> str:
        """内部令牌等价于全库 system 权限，太短等于没有；启动时就拒绝。"""
        token = (value or "").strip()
        if token and len(token) < 16:
            raise ValueError(
                "INTERNAL_API_TOKEN 至少 16 个字符（它等价于全库管理权限）"
            )
        return token

    # LLM：BYO，服务端不内置任何密钥
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None

    # --- M2 章节摘要（docs/15） -------------------------------------------------
    # mock = M2a 默认（无 key，确定性假响应）；api = 用户在界面里配置的 BYO 网关
    # （协议见 llm.py：chat_completion / anthropic_messages / openai_responses）；
    # deepseek = 旧的 env 方式（M2b 过渡）；agent = M4 预留。
    llm_provider: str = "mock"
    llm_timeout_s: float = 60.0
    llm_max_tokens: int = 1200

    # M9.1b-lite：复用同一个 chat 模型做检索查询改写（不是 embedding 向量召回）。
    # 只在查询含中文时自动触发；改写失败/未配置模型时静默回退 BM25。
    query_expand_enabled: bool = True
    query_expand_timeout_s: float = 8.0
    query_expand_max_keywords: int = 8
    query_expand_cache_size: int = 256
    # BYO key 的落盘位置：默认放 storage_dir（.data/，已在 .gitignore），不进仓库/DB。
    # 只存服务端；GET /api/settings/llm 只回掩码，原文永不回传前端。
    llm_config_file: Path | None = None

    # 送进 prompt 的章节正文上限（字符）与证据块上限；超了截断，避免一次烧掉整本书。
    # 字符预算是主约束；块数是安全上限：pdfium 是**行级块**（4 页窗口可达 200+ 条），
    # 40 会让一节只送进半天正文（2026-11-13 审查修）；docling 段级块通常远小于此。
    summary_max_chars: int = 6000
    summary_max_blocks: int = 300
    # 自动接受阈值（D3）：mock/真实 provider 都按这个分值决定 draft→published
    summary_auto_accept: bool = True
    summary_auto_accept_confidence: float = 0.9
    # 一条摘要至少绑定几条证据；不满足直接拒绝落库
    summary_min_evidence: int = 1
    # 推理模型（如 deepseek-v4.1-flash）会先花 token 在 reasoning 上；整篇速览/图纠错
    # 的 JSON 输出比单章长，给高一点的输出预算，避免 content 为空（见 llm.py 的报错）。
    # 图纠错还带图片（image tokens 会挤占预算），实测 1600 不够，给到 4000。
    paper_summary_max_tokens: int = 2400
    # 单章摘要输出比整篇短，但同样要给推理模型留 reasoning 预算：实测 1200 时约一半
    # 调用会「推理耗尽 / JSON 被截断」，3000 才稳定（M2b 真机校准）。
    section_summary_max_tokens: int = 3000
    figure_review_max_tokens: int = 4000

    # --- M3 正式化：自动接受器 + agent 任务租约 --------------------------------
    # 自动接受默认开启，但只对「白名单字段 + 证据非空 + 置信 ≥ 阈值 + 目标未被人工改过」
    # 四类条件同时满足的 proposal 生效；宁严勿滥（docs/15 §5 D3）。
    auto_accept_enabled: bool = True
    auto_accept_threshold: float = 0.95
    auto_accept_min_evidence: int = 1
    # agent 任务租约：领任务后多久不心跳就算过期（秒）；过期任务可被重新领取。
    task_lease_seconds: int = 300
    task_max_attempts: int = 3

    # --- M8 CSL 引用渲染（2026-11-16） -----------------------------------------
    # 用 pandoc --citeproc 渲染官方 CSL 样式（GB/T 7714-2015 / APA 7 / IEEE）。
    # pandoc 只作独立子进程调用（GPL-2.0，见 THIRD_PARTY.md），不链接进主程序；
    # 找不到 pandoc 时 CSL 引擎返回 503，13 种手写模板照常可用。
    csl_enabled: bool = True
    # 空 = 依次找 PATH 与 storage_dir/tools/pandoc/*/pandoc（scripts/get_pandoc.py 的落点）
    pandoc_path: str = ""
    csl_timeout_s: float = 20.0

    # --- M8 Word/PDF 导出（2026-11-16） ---------------------------------------
    # Word 用 pandoc；PDF 再要 typst（单文件二进制，避免 API 进程拉 xelatex 的
    # MiKTeX 权限问题，见 docs/09 §0.6）。缺工具时导出接口 503，其他功能不受影响。
    docx_export_enabled: bool = True
    pdf_export_enabled: bool = True
    # 空 = 依次找 PATH 与 storage_dir/tools/typst/*/typst（scripts/get_typst.py 的落点）
    typst_path: str = ""
    # typst PDF 正文字体；空 = 用 typst 默认字体 + 系统字体回退（Windows 中文可回退）
    pdf_main_font: str = ""
    document_export_timeout_s: float = 180.0

    # --- M8 元数据补全（Crossref / arXiv，2026-11-16） -------------------------
    # 只读外部 API，查询结果先 diff、用户确认后 apply 才写 paper 表；不写 Zotero。
    # 缺网络/接口失败时只影响补全功能，其他功能不受影响。
    metadata_lookup_enabled: bool = True
    crossref_api_base: str = "https://api.crossref.org"
    arxiv_api_base: str = "https://export.arxiv.org/api/query"
    metadata_timeout_s: float = 15.0
    # Crossref 礼貌池建议留联系邮箱（可空；只进 User-Agent，不落库/日志）
    crossref_mailto: str = ""

    # --- M3c 框复核（多模态；默认只提议不自动写） ------------------------------
    bbox_review_max_tokens: int = 4000
    bbox_review_dpi: int = 110
    bbox_review_max_image_dim: int = 1100
    # 默认 False：框复核建议只进待审队列；打开后仍需同时满足 IoU 极低/极高 +
    # caption 锚点一致 + 置信 ≥ 0.95 才会自动写 bbox_override。
    bbox_review_auto_accept: bool = False

    def model_post_init(self, __context: object) -> None:
        # storage_dir 相对路径按「进程启动目录」定死成绝对路径。否则 API 和 worker
        # 各自从不同工作目录启动时，会各自往不同的 .data 写文件，表现为
        # 「解析成功但缩略图 404」这种极难定位的错。
        if not self.storage_dir.is_absolute():
            self.storage_dir = (Path.cwd() / self.storage_dir).resolve()

    @property
    def pdf_dir(self) -> Path:
        return self.storage_dir / "pdfs"

    @property
    def image_dir(self) -> Path:
        return self.storage_dir / "images"

    @property
    def llm_config_path(self) -> Path:
        """BYO key 配置文件路径（服务端私有，不进仓库/DB）。"""
        if self.llm_config_file is not None:
            return self.llm_config_file
        return self.storage_dir / "llm-config.json"


settings = Settings()
