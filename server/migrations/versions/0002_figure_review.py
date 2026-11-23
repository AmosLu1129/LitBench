# SPDX-License-Identifier: AGPL-3.0-or-later
"""figure 增加纠错字段

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11

误报可接受，但必须能被纠正。这张表加的是「谁在什么时候判定这张图定位对不对，
以及修正后的 bbox」。人工点一次和 agent 调一次走同一条通道，
判定结果沉淀下来就是解析器的 golden set。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "figure",
        sa.Column("review_status", sa.String(length=16), nullable=False, server_default="pending"),
    )
    op.add_column("figure", sa.Column("review_note", sa.Text(), nullable=True))
    op.add_column("figure", sa.Column("bbox_override_json", sa.JSON(), nullable=True))
    op.add_column("figure", sa.Column("reviewed_by", sa.String(length=64), nullable=True))
    op.add_column("figure", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_figure_review_status", "figure", ["review_status"])


def downgrade() -> None:
    op.drop_index("ix_figure_review_status", table_name="figure")
    op.drop_column("figure", "reviewed_at")
    op.drop_column("figure", "reviewed_by")
    op.drop_column("figure", "bbox_override_json")
    op.drop_column("figure", "review_note")
    op.drop_column("figure", "review_status")
