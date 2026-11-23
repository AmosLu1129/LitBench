# SPDX-License-Identifier: AGPL-3.0-or-later
"""文献著录字段（2026-11-13）：volume / issue / pages。

为什么不是一上来就建：这三个字段只在「生成 references 条目」时才用得到，
而生成格式（GB/T 7714、APA、IEEE…）都要求卷期页。Zotero 导入能带就带，
带不进来就留空——格式器会省略缺失段并在返回里标 warning，不拿假数据凑格式。

Revision ID: 0012_paper_biblio_fields
Revises: 0011_writing_claim
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_paper_biblio_fields"
down_revision = "0011_writing_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper", sa.Column("volume", sa.String(length=32), nullable=True))
    op.add_column("paper", sa.Column("issue", sa.String(length=32), nullable=True))
    op.add_column("paper", sa.Column("pages", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("paper", "pages")
    op.drop_column("paper", "issue")
    op.drop_column("paper", "volume")
