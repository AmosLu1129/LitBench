# SPDX-License-Identifier: AGPL-3.0-or-later
"""init: paper / paper_version / block / figure / tab / job

Revision ID: 0001
Revises:
Create Date: 2026-11-11

只建 P1 需要的 6 张表。写作侧（card / claim / project / outline / draft /
cite_anchor）随 P2 一起进来，用 ``alembic revision --autogenerate`` 生成 0002。

``server_default=sa.text("now()")`` 是 Postgres 语法，本迁移只在 Postgres 上跑；
单元测试用 ``Base.metadata.create_all``（跨库）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("doi", sa.String(length=255), nullable=True),
        sa.Column("arxiv_id", sa.String(length=32), nullable=True),
        sa.Column("cite_key", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_norm", sa.String(length=512), nullable=False),
        sa.Column("authors_json", sa.JSON(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("venue", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper"),
    )
    op.create_index("ix_paper_doi", "paper", ["doi"])
    op.create_index("ix_paper_arxiv_id", "paper", ["arxiv_id"])
    op.create_index("ix_paper_cite_key", "paper", ["cite_key"])
    op.create_index("ix_paper_title_norm", "paper", ["title_norm"])
    op.create_index("ix_paper_year", "paper", ["year"])

    op.create_table(
        "paper_version",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("paper_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("venue", sa.String(length=255), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("doi", sa.String(length=255), nullable=True),
        sa.Column("arxiv_id", sa.String(length=32), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("parser", sa.String(length=32), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=True),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name="fk_paper_version_paper_id_paper",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_version"),
        sa.UniqueConstraint("file_sha256", name="uq_version_sha256"),
    )
    op.create_index("ix_paper_version_paper_id", "paper_version", ["paper_id"])
    op.create_index("ix_paper_version_file_sha256", "paper_version", ["file_sha256"])

    op.create_table(
        "block",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("bbox_json", sa.JSON(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("order_idx", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(length=32), nullable=True),
        sa.Column("engine", sa.String(length=32), nullable=True),
        sa.Column("conf", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_block_paper_version_id_paper_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_block"),
    )
    op.create_index("ix_block_paper_version_id", "block", ["paper_version_id"])
    op.create_index("ix_block_parent_id", "block", ["parent_id"])
    op.create_index("ix_block_version_page_order", "block", ["paper_version_id", "page", "order_idx"])

    op.create_table(
        "figure",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("block_id", sa.String(length=32), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("bbox_json", sa.JSON(), nullable=False),
        sa.Column("image_path", sa.Text(), nullable=True),
        sa.Column("thumb_path", sa.Text(), nullable=True),
        sa.Column("label_raw", sa.String(length=64), nullable=True),
        sa.Column("label_norm", sa.String(length=16), nullable=True),
        sa.Column("label_num", sa.Integer(), nullable=True),
        sa.Column("sublabel", sa.String(length=8), nullable=True),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("figure_type", sa.String(length=16), nullable=True),
        sa.Column("license_hint", sa.String(length=16), nullable=False),
        sa.Column("cite_key", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_figure_paper_version_id_paper_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_figure"),
        sa.UniqueConstraint("cite_key", name="uq_figure_cite_key"),
    )
    op.create_index("ix_figure_paper_version_id", "figure", ["paper_version_id"])
    op.create_index("ix_figure_block_id", "figure", ["block_id"])
    op.create_index("ix_figure_label_norm", "figure", ["label_norm"])
    op.create_index("ix_figure_figure_type", "figure", ["figure_type"])

    op.create_table(
        "tab",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("block_id", sa.String(length=32), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("label_raw", sa.String(length=64), nullable=True),
        sa.Column("label_norm", sa.String(length=16), nullable=True),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("cells_json", sa.JSON(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("quality", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_tab_paper_version_id_paper_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tab"),
    )
    op.create_index("ix_tab_paper_version_id", "tab", ["paper_version_id"])
    op.create_index("ix_tab_block_id", "tab", ["block_id"])
    op.create_index("ix_tab_label_norm", "tab", ["label_norm"])

    op.create_table(
        "job",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["paper_version_id"],
            ["paper_version.id"],
            name="fk_job_paper_version_id_paper_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job"),
    )
    op.create_index("ix_job_paper_version_id", "job", ["paper_version_id"])
    op.create_index("ix_job_created_at", "job", ["created_at"])
    op.create_index("ix_job_status_created", "job", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("job")
    op.drop_table("tab")
    op.drop_table("figure")
    op.drop_table("block")
    op.drop_table("paper_version")
    op.drop_table("paper")
