# SPDX-License-Identifier: AGPL-3.0-or-later
"""工作台改版（2026-11-12）：paper 增加用户备注与收藏。

Revision ID: 0008_paper_workspace
Revises: 0007_figure_proposal
Create Date: 2026-11-12

为什么放在 paper 而不是另开表：
* ``note`` 是用户对文献的一句话/一段备注，1:1，不需要单独表；
* ``favorite`` 是布尔标记（「收藏夹」是虚拟视图，不是一张集合表——集合仍归 collection 管，
  收藏与否不该和「自由分组」混在同一个层级里）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_paper_workspace"
down_revision = "0007_figure_proposal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper", sa.Column("note", sa.Text(), nullable=True))
    op.add_column(
        "paper",
        sa.Column(
            "favorite",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("paper", "favorite")
    op.drop_column("paper", "note")
