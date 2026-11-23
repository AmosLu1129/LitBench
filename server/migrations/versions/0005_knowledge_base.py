# SPDX-License-Identifier: AGPL-3.0-or-later
"""知识库（M1）：collection / paper_collection / import_run + paper 元数据扩展。

Revision ID: 0005_knowledge_base
Revises: 0004_figure_origin
Create Date: 2026-11-12

背景：Zotero 只读导入（D1 决策）。Zotero 继续当文献库主人，这边只镜像集合树与条目
元数据，不回写；摘要/标注等衍生物留在本库。详见 docs/15。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_knowledge_base"
down_revision = "0004_figure_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- paper 元数据扩展 ---
    op.add_column("paper", sa.Column("abstract", sa.Text(), nullable=True))
    op.add_column(
        "paper", sa.Column("tags_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json"))
    )
    op.add_column(
        "paper",
        sa.Column("reading_status", sa.String(length=16), nullable=False, server_default="unread"),
    )
    op.add_column("paper", sa.Column("zotero_item_key", sa.String(length=32), nullable=True))
    op.add_column(
        "paper",
        sa.Column("metadata_source", sa.String(length=16), nullable=False, server_default="upload"),
    )
    op.add_column("paper", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_paper_zotero_item_key", "paper", ["zotero_item_key"])

    # --- 知识库目录树 ---
    op.create_table(
        "collection",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("parent_id", sa.String(length=32), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("zotero_key", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["collection.id"], name="fk_collection_parent_id_collection", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_collection"),
        sa.UniqueConstraint("zotero_key", name="uq_collection_zotero_key"),
    )
    op.create_index("ix_collection_parent_id", "collection", ["parent_id"])

    op.create_table(
        "paper_collection",
        sa.Column("paper_id", sa.String(length=32), nullable=False),
        sa.Column("collection_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["collection_id"], ["collection.id"], name="fk_paper_collection_collection_id_collection", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"], name="fk_paper_collection_paper_id_paper", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("paper_id", "collection_id", name="pk_paper_collection"),
    )
    op.create_index("ix_paper_collection_paper_id", "paper_collection", ["paper_id"])
    op.create_index("ix_paper_collection_collection_id", "paper_collection", ["collection_id"])

    # --- 导入审计 ---
    op.create_table(
        "import_run",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_import_run"),
    )
    op.create_index("ix_import_run_started_at", "import_run", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_import_run_started_at", table_name="import_run")
    op.drop_table("import_run")
    op.drop_index("ix_paper_collection_collection_id", table_name="paper_collection")
    op.drop_index("ix_paper_collection_paper_id", table_name="paper_collection")
    op.drop_table("paper_collection")
    op.drop_index("ix_collection_parent_id", table_name="collection")
    op.drop_table("collection")
    op.drop_constraint("uq_paper_zotero_item_key", "paper", type_="unique")
    op.drop_column("paper", "imported_at")
    op.drop_column("paper", "metadata_source")
    op.drop_column("paper", "zotero_item_key")
    op.drop_column("paper", "reading_status")
    op.drop_column("paper", "tags_json")
    op.drop_column("paper", "abstract")
