"""Add nullable tool execution correlation fields (M0).

Revision ID: f1c2d3e4a5b6
Revises: e8a1b2c3d4f5
"""
from alembic import context, op
import sqlalchemy as sa

revision = "f1c2d3e4a5b6"
down_revision = "e8a1b2c3d4f5"
branch_labels = None
depends_on = None

FIELDS = {"run_id": 128, "tool_call_id": 128, "step_id": 36,
          "operation_id": 36, "error_code": 64}


def upgrade():
    # Legacy create_all installations can already have the new nullable columns.
    existing = set() if context.is_offline_mode() else {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("tool_call_audit")
    }
    for name, length in FIELDS.items():
        if name not in existing:
            op.add_column("tool_call_audit", sa.Column(name, sa.String(length), nullable=True))


def downgrade():
    for name in reversed(FIELDS):
        op.drop_column("tool_call_audit", name)
