# SPDX-License-Identifier: AGPL-3.0-or-later
"""工作组与共享（M7 第二步，2026-11-15）。

用户拍板的数据模型（docs/17 §8.1）：

* 每个用户的**私有库只归个人**：``paper/collection/claim.owner_user_id``；
  旧数据为 NULL = legacy，仍按全局库可见（不做回填，避免把公共语料判给某个账号）；
* **工作组**（``workgroup`` / ``workgroup_member``）是共享的最小单位；
* **共享是文献级**（``paper_share``）：图/表/摘要随文献可见性，不能单独共享；
  v1 团队库是平铺文献列表，不做团队目录/团队笔记；
* **私有状态按用户隔离**（``user_paper_state``）：同一篇被共享的文献，
  笔记/收藏/阅读状态各用各的，否则 B 改备注会把 A 的覆盖掉。

实现取舍：

* ``owner_user_id`` 只做逻辑外键、不建 FK 约束。原因：SQLite 上 ``ALTER TABLE
  ADD CONSTRAINT`` 不支持（Alchemy/Alembic 需要 batch 重建表），而 dev 库是手工
  应用 DDL 的；账号又不支持删除（只能停用），owner 悬空的风险很低。
  ``workgroup_member`` / ``paper_share`` / ``user_paper_state`` 的 FK 在
  ``create_table`` 里建，删除工作组时 CASCADE 正常。
* 索引名严格按 ``Base.metadata`` 的命名规范，``test_migration_matches_models``
  会逐列/逐索引比对。

Revision ID: 0014_team_workgroups
Revises: 0013_team_auth
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_team_workgroups"
down_revision = "0013_team_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 工作组 ----
    op.create_table(
        "workgroup",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workgroup"),
    )
    op.create_index("ix_workgroup_created_by", "workgroup", ["created_by"])

    # ---- 成员 ----
    op.create_table(
        "workgroup_member",
        sa.Column("workgroup_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="member"),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workgroup_id"],
            ["workgroup.id"],
            name="fk_workgroup_member_workgroup_id_workgroup",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_workgroup_member_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workgroup_id", "user_id", name="pk_workgroup_member"),
    )
    op.create_index("ix_workgroup_member_user_id", "workgroup_member", ["user_id"])

    # ---- 共享：文献 → 工作组 ----
    op.create_table(
        "paper_share",
        sa.Column("paper_id", sa.String(length=32), nullable=False),
        sa.Column("workgroup_id", sa.String(length=32), nullable=False),
        sa.Column("shared_by", sa.String(length=32), nullable=True),
        sa.Column(
            "shared_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"], ["paper.id"], name="fk_paper_share_paper_id_paper", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workgroup_id"],
            ["workgroup.id"],
            name="fk_paper_share_workgroup_id_workgroup",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_id", "workgroup_id", name="pk_paper_share"),
    )
    op.create_index("ix_paper_share_workgroup_id", "paper_share", ["workgroup_id"])
    op.create_index("ix_paper_share_shared_by", "paper_share", ["shared_by"])

    # ---- 用户对文献的私有状态 ----
    op.create_table(
        "user_paper_state",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("paper_id", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("favorite", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reading_status", sa.String(length=16), nullable=False, server_default="unread"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_user_paper_state_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name="fk_user_paper_state_paper_id_paper",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "paper_id", name="pk_user_paper_state"),
    )
    op.create_index("ix_user_paper_state_paper_id", "user_paper_state", ["paper_id"])

    # ---- 归属字段（逻辑外键，见文件头说明） ----
    op.add_column("paper", sa.Column("owner_user_id", sa.String(length=32), nullable=True))
    op.add_column("collection", sa.Column("owner_user_id", sa.String(length=32), nullable=True))
    op.add_column("claim", sa.Column("owner_user_id", sa.String(length=32), nullable=True))
    op.create_index("ix_paper_owner_user_id", "paper", ["owner_user_id"])
    op.create_index("ix_collection_owner_user_id", "collection", ["owner_user_id"])
    op.create_index("ix_claim_owner_user_id", "claim", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_claim_owner_user_id", table_name="claim")
    op.drop_index("ix_collection_owner_user_id", table_name="collection")
    op.drop_index("ix_paper_owner_user_id", table_name="paper")
    op.drop_column("claim", "owner_user_id")
    op.drop_column("collection", "owner_user_id")
    op.drop_column("paper", "owner_user_id")

    op.drop_index("ix_user_paper_state_paper_id", table_name="user_paper_state")
    op.drop_table("user_paper_state")

    op.drop_index("ix_paper_share_shared_by", table_name="paper_share")
    op.drop_index("ix_paper_share_workgroup_id", table_name="paper_share")
    op.drop_table("paper_share")

    op.drop_index("ix_workgroup_member_user_id", table_name="workgroup_member")
    op.drop_table("workgroup_member")

    op.drop_index("ix_workgroup_created_by", table_name="workgroup")
    op.drop_table("workgroup")
