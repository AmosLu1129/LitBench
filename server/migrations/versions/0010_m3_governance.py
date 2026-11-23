# SPDX-License-Identifier: AGPL-3.0-or-later
"""M3 正式化地基（2026-11-13）：审计日志 + agent 任务租约 + 提案回滚/自动接受字段。

Revision ID: 0010_m3_governance
Revises: 0009_summary_confidence
Create Date: 2026-11-13

三件事：
1. ``audit_log``：所有正式字段写操作（人工/自动采纳、回滚、改图号）留 before/after，
   出问题能查、能回滚；不记录文献正文。
2. ``agent_task``：agent 任务租约（claim / heartbeat / retry / 超时回收），
   内置 worker 先跑，外部 agent（M5 MCP）复用同一张表。
3. ``figure_proposal`` 补 ``before_json``/``auto_accepted``/回滚标记；
   ``figure`` 补 ``caption_source``（parser|agent|human），保证用户改过的图注永不被自动覆盖。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_m3_governance"
down_revision = "0009_summary_confidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- figure.caption_source ------------------------------------------------
    op.add_column(
        "figure",
        sa.Column(
            "caption_source",
            sa.String(length=16),
            nullable=False,
            server_default="parser",
        ),
    )

    # --- figure_proposal：回滚与自动接受 --------------------------------------
    op.add_column("figure_proposal", sa.Column("before_json", sa.JSON(), nullable=True))
    op.add_column(
        "figure_proposal",
        sa.Column(
            "auto_accepted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "figure_proposal",
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "figure_proposal",
        sa.Column("rolled_back_by", sa.String(length=64), nullable=True),
    )

    # --- audit_log ------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False, server_default="human"),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False, server_default="figure"),
        sa.Column("target_id", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"])
    op.create_index(
        "ix_audit_log_action_created", "audit_log", ["action", "created_at"]
    )

    # --- agent_task -----------------------------------------------------------
    op.create_table(
        "agent_task",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False, server_default="figure"),
        sa.Column("target_id", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_agent_task"),
    )
    op.create_index("ix_agent_task_kind", "agent_task", ["kind"])
    op.create_index("ix_agent_task_created_at", "agent_task", ["created_at"])
    op.create_index(
        "ix_agent_task_idempotency_key", "agent_task", ["idempotency_key"], unique=True
    )
    op.create_index(
        "ix_agent_task_status_created", "agent_task", ["status", "priority", "created_at"]
    )
    op.create_index("ix_agent_task_target", "agent_task", ["target_type", "target_id"])
    op.create_index("ix_agent_task_lease_until", "agent_task", ["lease_until"])


def downgrade() -> None:
    op.drop_index("ix_agent_task_lease_until", table_name="agent_task")
    op.drop_index("ix_agent_task_target", table_name="agent_task")
    op.drop_index("ix_agent_task_status_created", table_name="agent_task")
    op.drop_index("ix_agent_task_idempotency_key", table_name="agent_task")
    op.drop_index("ix_agent_task_created_at", table_name="agent_task")
    op.drop_index("ix_agent_task_kind", table_name="agent_task")
    op.drop_table("agent_task")

    op.drop_index("ix_audit_log_action_created", table_name="audit_log")
    op.drop_index("ix_audit_log_target", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_column("figure_proposal", "rolled_back_by")
    op.drop_column("figure_proposal", "rolled_back_at")
    op.drop_column("figure_proposal", "auto_accepted")
    op.drop_column("figure_proposal", "before_json")
    op.drop_column("figure", "caption_source")
