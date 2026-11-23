# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作台小修：claim 编辑历史（迁移 0016，2026-11-15）。

``docs/17`` §11.4「写作台小修：claim 编辑历史」的数据落点：

* 每次 claim 内容真正变化（文字/章节/状态/标签/证据/排序）前，把旧状态压进
  ``claim_revision``；
* 恢复历史版本时，先把当前状态也压一份（``reason=restored``），再写回快照，
  历史链不会因为回滚而断；
* 只加表 + 索引，不改 ``claim`` 现有列，旧数据不需要回填。

Revision ID: 0016_claim_revisions
Revises: 0015_thesis_entry
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_claim_revisions"
down_revision = "0015_thesis_entry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claim_revision",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("claim_id", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("order_idx", sa.Integer(), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("edited_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.id"],
            name="fk_claim_revision_claim_id_claim",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_claim_revision"),
    )
    op.create_index("ix_claim_revision_claim_id", "claim_revision", ["claim_id"])
    op.create_index("ix_claim_revision_created_at", "claim_revision", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_claim_revision_created_at", table_name="claim_revision")
    op.drop_index("ix_claim_revision_claim_id", table_name="claim_revision")
    op.drop_table("claim_revision")
