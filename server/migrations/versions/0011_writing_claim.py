# SPDX-License-Identifier: AGPL-3.0-or-later
"""写作侧素材卡片（M5）：``claim`` = 一句可引用论断 + 证据。

Revision ID: 0011_writing_claim
Revises: 0010_m3_governance
Create Date: 2026-11-13

只加一张表。对比矩阵、LaTeX/BibTeX 导出和引用校验都由现有表派生，
不落库（派生结果会随源数据变化，存下来只会过期）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_writing_claim"
down_revision = "0010_m3_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claim",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("paper_id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="human"),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("order_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"], ["paper.id"], name="fk_claim_paper_id_paper", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_claim_paper_version_id_paper_version",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_claim"),
    )
    op.create_index("ix_claim_paper_id", "claim", ["paper_id"])
    op.create_index("ix_claim_paper_version_id", "claim", ["paper_version_id"])
    op.create_index("ix_claim_created_at", "claim", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_claim_created_at", table_name="claim")
    op.drop_index("ix_claim_paper_version_id", table_name="claim")
    op.drop_index("ix_claim_paper_id", table_name="claim")
    op.drop_table("claim")
