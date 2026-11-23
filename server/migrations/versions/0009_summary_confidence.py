# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2b 收尾（2026-11-12）：摘要落库模型自评置信度。

Revision ID: 0009_summary_confidence
Revises: 0008_paper_workspace
Create Date: 2026-11-12

为什么加这一列：
* 自动接受（D3）一直按 ``confidence >= 阈值`` 放开，但置信度只用于当场判断、没有落库，
  事后无法看真实分布，也就无法校准 0.9 这个拍出来的阈值；
* 人工编辑/回滚也要能沿历史链看到当时的置信度，所以 ``summary_revision`` 同步加一列。

列可空：老数据、人工编辑后的摘要没有「模型自评」这回事，保持 NULL 比填 0 更诚实。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_summary_confidence"
down_revision = "0008_paper_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("summary", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column(
        "summary_revision", sa.Column("confidence", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("summary_revision", "confidence")
    op.drop_column("summary", "confidence")
