# SPDX-License-Identifier: AGPL-3.0-or-later
"""figure：用户增删图（origin / deleted_at）。

Revision ID: 0004_figure_origin
Revises: 0003
Create Date: 2026-11-12

为什么需要：
- 用户在原文里框选补图 → ``origin=manual``，重解析时要保留（worker.persist 快照回填）；
- 用户删掉解析器误报的图 → 软删除 ``deleted_at``，重解析时按 cite_key 重新压回去，
  否则删掉的图会在下一次重解析后全部复活。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_figure_origin"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "figure",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="parser"),
    )
    op.add_column("figure", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_figure_deleted_at", "figure", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_figure_deleted_at", table_name="figure")
    op.drop_column("figure", "deleted_at")
    op.drop_column("figure", "origin")
