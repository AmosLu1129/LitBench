# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作卡证据失效标记（迁移 0017，2026-11-16，SOTA 审查 F14）。

重解析是「先删后插」：block / figure / table 全换新 id。claim 的证据锚在旧 id 上，
重解析时按稳定身份重锚（见 reparse.py）；映射不回来的证据剔除并置 ``evidence_stale``，
卡片本身永不删除（人工内容不丢）。旧数据不需要回填：默认 False。

Revision ID: 0017_claim_evidence_stale
Revises: 0016_claim_revisions
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_claim_evidence_stale"
down_revision = "0016_claim_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claim",
        sa.Column("evidence_stale", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("claim", "evidence_stale")