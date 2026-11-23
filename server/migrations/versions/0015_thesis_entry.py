# SPDX-License-Identifier: AGPL-3.0-or-later
"""学位论文条目类型与著录字段（M8，2026-11-15）。

``docs/17`` §11.3.1 的最小改动：

* ``paper.entry_type``：判断 references 走 ``[J]`` / ``[C]`` / ``[D]`` / ``[M]``
  哪条分支。Zotero 的 ``itemType`` 导入时映射进来；旧数据默认 ``journal``
  （保持以前的输出，不追认历史条目类型）。
* ``paper.thesis_school / thesis_degree / thesis_place``：GB/T ``[D]`` 的
  「出版地: 学校」与 BibTeX ``school/address`` 要用；没有就留 NULL，
  格式器省略并给 warning，不瞎编。

迁移只加列、不加索引：``entry_type`` 目前只服务于条目格式化，
不参与列表过滤，先不为 10 篇量级的库建索引。

Revision ID: 0015_thesis_entry
Revises: 0014_team_workgroups
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_thesis_entry"
down_revision = "0014_team_workgroups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper",
        sa.Column(
            "entry_type",
            sa.String(length=16),
            nullable=False,
            server_default="journal",
        ),
    )
    op.add_column("paper", sa.Column("thesis_school", sa.String(length=255), nullable=True))
    op.add_column("paper", sa.Column("thesis_degree", sa.String(length=255), nullable=True))
    op.add_column("paper", sa.Column("thesis_place", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("paper", "thesis_place")
    op.drop_column("paper", "thesis_degree")
    op.drop_column("paper", "thesis_school")
    op.drop_column("paper", "entry_type")
