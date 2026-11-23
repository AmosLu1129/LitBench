# SPDX-License-Identifier: AGPL-3.0-or-later
"""本地账号与会话（2026-11-15，团队共享第一步）。

设计取舍：

* **本地账号 + 管理员建号**（用户 2026-11-15 拍板），不引第三方账号体系；
* 密码用标准库 ``hashlib.scrypt``（N=2^14, r=8, p=1），存 ``scrypt$N$r$p$salt$hash``，
  校验用常量时间比较；不引 passlib/argon2 依赖（自部署要少装东西）；
* 会话是**服务端 session**：浏览器 cookie 里是随机 token，库里只存 sha256 + 过期时间；
  退出/禁用账号可立刻失效，比 JWT 更适合小团队；
* 认证默认关闭（``LITBENCH_AUTH=0/1``）：M7 工作组/共享做完、Docker 部署时再默认打开，
  避免在功能只完成一半时把现有单机开发流程锁死。

Revision ID: 0013_team_auth
Revises: 0012_paper_biblio_fields
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_team_auth"
down_revision = "0012_paper_biblio_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="member"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_app_user"),
    )
    op.create_index("ix_app_user_username", "app_user", ["username"], unique=True)
    op.create_index("ix_app_user_created_at", "app_user", ["created_at"])

    op.create_table(
        "user_session",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_user_session_user_id_app_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_session"),
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])
    op.create_index("ix_user_session_token_sha256", "user_session", ["token_sha256"], unique=True)
    op.create_index("ix_user_session_expires_at", "user_session", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_user_session_expires_at", table_name="user_session")
    op.drop_index("ix_user_session_token_sha256", table_name="user_session")
    op.drop_index("ix_user_session_user_id", table_name="user_session")
    op.drop_table("user_session")
    op.drop_index("ix_app_user_created_at", table_name="app_user")
    op.drop_index("ix_app_user_username", table_name="app_user")
    op.drop_table("app_user")
