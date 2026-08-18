"""add durable pause_requested run state

Revision ID: 005_run_pause_resume
Revises: 004_run_recovery_checkpoint
"""

from alembic import op


revision = "005_run_pause_resume"
down_revision = "004_run_recovery_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('queued','running','waiting_permission','pause_requested','paused','resume_requested',"
        "'cancel_requested','cancelling','completed','failed','cancelled')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE agent_runs SET status = 'running' WHERE status = 'pause_requested'"
    )
    op.execute(
        "UPDATE agent_runs SET status = 'paused' WHERE status = 'resume_requested'"
    )
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('queued','running','waiting_permission','paused',"
        "'cancel_requested','cancelling','completed','failed','cancelled')",
    )
