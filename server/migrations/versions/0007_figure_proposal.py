# SPDX-License-Identifier: AGPL-3.0-or-later
"""图提取 AI 纠错的提议表（M3）。

Revision ID: 0007_figure_proposal
Revises: 0006_summary
Create Date: 2026-11-12

为什么单独一张表：AI 对「图号 / 图注 / 裁框」的判断是**建议**，不是真值。
采纳才写 figure 的正式字段；拒绝也留记录（被拒的提议能看出模型哪类判断不可靠）。
同一张图可以有多条历史提议（按 created_at 倒序看），只有最新 pending 那条会被 UI 当建议。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_figure_proposal"
down_revision = "0006_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "figure_proposal",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("figure_id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="figure_review"),
        sa.Column("proposed_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="text"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["figure_id"], ["figure.id"], name="fk_figure_proposal_figure_id_figure", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_figure_proposal_paper_version_id_paper_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_figure_proposal"),
    )
    op.create_index("ix_figure_proposal_figure_id", "figure_proposal", ["figure_id"])
    op.create_index(
        "ix_figure_proposal_paper_version_id", "figure_proposal", ["paper_version_id"]
    )
    op.create_index("ix_figure_proposal_created_at", "figure_proposal", ["created_at"])
    op.create_index("ix_figure_proposal_status", "figure_proposal", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_figure_proposal_status", table_name="figure_proposal")
    op.drop_index("ix_figure_proposal_created_at", table_name="figure_proposal")
    op.drop_index("ix_figure_proposal_paper_version_id", table_name="figure_proposal")
    op.drop_index("ix_figure_proposal_figure_id", table_name="figure_proposal")
    op.drop_table("figure_proposal")
