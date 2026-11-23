# SPDX-License-Identifier: AGPL-3.0-or-later
"""章节级摘要（M2）：summary / summary_revision。

Revision ID: 0006_summary
Revises: 0005_knowledge_base
Create Date: 2026-11-12

背景：M2a 无 key 骨架——先用 mock provider 把「章节抽取 → prompt → JSON 解析 →
证据校验 → 落库 → UI」全链路跑通（docs/15）。表结构两段共用，M2b 只换 provider。

两条刻意的设计：
1. 摘要挂 paper_version 而不是 paper：v1/v2 的章节文本不同，摘要不能跨版本复用；
2. 证据是硬约束：evidence_block_ids_json 必须非空且全指向本版本 block，校验不过不落库。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_summary"
down_revision = "0005_knowledge_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "summary",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("section_key", sa.String(length=64), nullable=False),
        sa.Column("section_title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("key_points_json", sa.JSON(), nullable=False),
        sa.Column("evidence_block_ids_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="agent"),
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="mock"),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("auto_accepted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("human_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("edited_by", sa.String(length=64), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_summary_paper_version_id_paper_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_summary"),
        sa.UniqueConstraint("paper_version_id", "section_key", name="uq_summary_version_section"),
    )
    op.create_index("ix_summary_paper_version_id", "summary", ["paper_version_id"])
    op.create_index("ix_summary_version_status", "summary", ["paper_version_id", "status"])

    op.create_table(
        "summary_revision",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("summary_id", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("key_points_json", sa.JSON(), nullable=False),
        sa.Column("evidence_block_ids_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="agent"),
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="mock"),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("reason", sa.String(length=32), nullable=False, server_default="generated"),
        sa.Column("edited_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["summary_id"],
            ["summary.id"],
            name="fk_summary_revision_summary_id_summary",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_summary_revision"),
    )
    op.create_index("ix_summary_revision_summary_id", "summary_revision", ["summary_id"])


def downgrade() -> None:
    op.drop_index("ix_summary_revision_summary_id", table_name="summary_revision")
    op.drop_table("summary_revision")
    op.drop_index("ix_summary_version_status", table_name="summary")
    op.drop_index("ix_summary_paper_version_id", table_name="summary")
    op.drop_table("summary")
