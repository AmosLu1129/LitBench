# SPDX-License-Identifier: AGPL-3.0-or-later
"""图号校核：label 来源/置信度/子图组 + 文字层质量

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11

背景：真实课本里大量图「看起来没有编号」，其实是一个系列图被拆成多个图块，
或图注文本里的编号没被提取/被 OCR 识别坏。`labeling.py` 规则层会补这些编号，
但必须回答「这个编号依据是什么、有多可信」，否则等于把猜的当真的。

- figure.label_source：text / rule_repair / rule_inherit / rule_infer / ocr / ocr_consensus / review
- figure.label_confidence：0–1，低于 LOW_CONFIDENCE 的只作候选
- figure.group_key：同一系列子图的组（界面折叠、按父图号检索）
- paper_version.text_quality / text_quality_flags_json：文字层质量分与依据，
  阶段 2 用它决定「文字层太烂时改信两套 OCR 的共识」（见 docs/14）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("figure", sa.Column("label_source", sa.String(length=16), nullable=True))
    op.add_column("figure", sa.Column("label_confidence", sa.Float(), nullable=True))
    op.add_column("figure", sa.Column("group_key", sa.String(length=64), nullable=True))
    op.create_index("ix_figure_label_source", "figure", ["label_source"])
    op.create_index("ix_figure_group_key", "figure", ["group_key"])

    op.add_column("paper_version", sa.Column("text_quality", sa.Float(), nullable=True))
    op.add_column("paper_version", sa.Column("text_quality_flags_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_version", "text_quality_flags_json")
    op.drop_column("paper_version", "text_quality")
    op.drop_index("ix_figure_group_key", table_name="figure")
    op.drop_index("ix_figure_label_source", table_name="figure")
    op.drop_column("figure", "group_key")
    op.drop_column("figure", "label_confidence")
    op.drop_column("figure", "label_source")
