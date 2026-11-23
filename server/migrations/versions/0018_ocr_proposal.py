# SPDX-License-Identifier: AGPL-3.0-or-later
"""轻量 OCR 与大模型校对提案（迁移 0018，2026-11-17）。

扫描页先经 RapidOCR 写成文字块；再用已配置的视觉模型逐页校对。校对一致直接采用
OCR；不一致写一条 ``ocr_proposal`` 交用户决定（接受 OCR / 接受模型修订 / 拒绝）。
只加表 + 索引，不改既有表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_ocr_proposal"
down_revision = "0017_claim_evidence_stale"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_proposal",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("ocr_text", sa.Text(), nullable=False),
        sa.Column("model_text", sa.Text(), nullable=False),
        sa.Column("diff_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_ocr_proposal_paper_version_id_paper_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ocr_proposal"),
    )
    op.create_index("ix_ocr_proposal_paper_version_id", "ocr_proposal", ["paper_version_id"])
    op.create_index("ix_ocr_proposal_created_at", "ocr_proposal", ["created_at"])
    op.create_index("ix_ocr_proposal_status_created_at", "ocr_proposal", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ocr_proposal_status_created_at", table_name="ocr_proposal")
    op.drop_index("ix_ocr_proposal_created_at", table_name="ocr_proposal")
    op.drop_index("ix_ocr_proposal_paper_version_id", table_name="ocr_proposal")
    op.drop_table("ocr_proposal")
