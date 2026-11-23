<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# 第三方组件

原则：**主镜像里只允许 MIT / Apache-2.0 / BSD / PostgreSQL 类许可证。** copyleft 组件一律走可选 profile 或子进程。

## 直接依赖（进主镜像）

| 组件 | 许可证 | 用途 |
| --- | --- | --- |
| Docling | MIT | PDF 解析基线（版面 / 表格 / 图片 / bbox） |
| pypdfium2 | Apache-2.0 · BSD-3 | PDF 页面渲染（替代 AGPL 的 PyMuPDF） |
| pypdf | BSD-3 | PDF 结构操作 |
| FastAPI / Uvicorn | MIT / BSD-3 | API |
| SQLAlchemy / Alembic | MIT | ORM / 迁移 |
| asyncpg | Apache-2.0 | Postgres 驱动 |
| pgvector | PostgreSQL License | 向量检索 |
| Valkey | BSD-3 | 队列 |
| PDF.js | Apache-2.0 | 前端 PDF 渲染与 bbox 高亮 |
| CSL 样式（citation-style-language/styles） | CC BY-SA 3.0 | 参考文献样式**数据**（GB/T 7714-2015 / APA 7 / IEEE），随仓库 `server/litbench/csl_styles/` 分发，不改内容；来源与 SHA-256 见该目录 README |
| pandoc | GPL-2.0 | CSL 引用渲染（`--citeproc`）与 docx/pdf 导出；**仅子进程调用，不链接**；镜像里固定 3.14 + SHA-256 校验 |
| typst | MIT | **PDF 导出引擎**（pandoc `--pdf-engine=typst` 的子进程）；单文件二进制，镜像里固定 0.15.1 + SHA-256 |
| Noto CJK 字体（Docker 镜像内） | SIL OFL 1.1 | PDF/Word 中文字形（apt `fonts-noto-cjk`，不随仓库分发） |
| RapidOCR | Apache-2.0 | OCR 引擎（Docling 增强档的 OCR 路径；图号双 OCR 交叉校核的第二套，v4 模型随包分发） |
| onnxruntime | MIT | RapidOCR 的推理后端（CPU） |

## 可选组件（`docker compose --profile ocr`，不进主镜像）

| 组件 | 许可证 | 接入方式 | 说明 |
| --- | --- | --- | --- |
| MinerU | AGPL-3.0 | **独立 venv + `mineru-kit` CLI 子进程** | 扫描件 OCR / 版面增强；主镜像既不 import 也不安装它的依赖；模型权重由用户自行下载（默认 modelscope），仓库不分发 |
| Marker | GPL-3.0 + 权重附加条款 | 独立容器 | 权重有商业收入门槛，**仓库不分发权重** |

## 明确不用

| 组件 | 许可证 | 原因 / 替代 |
| --- | --- | --- |
| PyMuPDF | AGPL-3.0 | 换 pypdfium2 + pypdf |
| WebPlotDigitizer | AGPL-3.0 | 只参考算法，轴检测自己写 |
| MinIO | AGPL-3.0 | 用云 S3 / SeaweedFS |
| Redis ≥ 7.4 | RSALv2 / SSPLv1 | 用 Valkey |

## 模型权重

所有模型权重**不随仓库分发**。用户在设置页或环境变量里指定权重路径，由用户自行下载并遵守其许可证。
科研模型常见 research-only / non-commercial 条款——这类权重不得进入发行包，也不得用于托管服务。

## 免责

用户上传的 PDF、论文中的图与表，其版权与使用权与本项目许可证无关。
本项目只提供检索、引用建议与重绘辅助，不提供论文内容的再分发。
