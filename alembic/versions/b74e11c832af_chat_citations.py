"""Keep versioned citations with their committed assistant reply."""
from alembic import context, op
import sqlalchemy as sa

revision = 'b74e11c832af'
down_revision = 'e51a93f8c4b7'
branch_labels = None
depends_on = None


def upgrade():
    if context.is_offline_mode() or 'citations_json' not in {
        column['name'] for column in sa.inspect(op.get_bind()).get_columns('chat_messages')
    }:
        op.add_column('chat_messages', sa.Column('citations_json', sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError('Inspect saved citations before downgrade')
    if op.get_bind().scalar(sa.text('SELECT 1 FROM chat_messages WHERE citations_json IS NOT NULL LIMIT 1')):
        raise RuntimeError('Saved citations exist')
    op.drop_column('chat_messages', 'citations_json')
