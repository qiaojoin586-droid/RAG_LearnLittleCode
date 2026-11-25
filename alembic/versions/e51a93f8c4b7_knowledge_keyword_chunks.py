"""Store keyword-searchable text for published knowledge chunks."""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT


revision = 'e51a93f8c4b7'
down_revision = 'd26b913f7e5c'
branch_labels = None
depends_on = None
TABLE = 'knowledge_search_chunks'
INDEX = 'ft_knowledge_search_content'


def upgrade():
    bind = None if context.is_offline_mode() else op.get_bind()
    if bind is None or not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column('document_id', sa.Integer(), sa.ForeignKey('knowledge_documents.id', ondelete='CASCADE'),
                      primary_key=True),
            sa.Column('ordinal', sa.Integer(), primary_key=True),
            sa.Column('resource_version_id', sa.String(64), nullable=False),
            sa.Column('content', sa.Text().with_variant(LONGTEXT(), 'mysql'), nullable=False),
            mysql_engine='InnoDB', mysql_charset='utf8mb4',
        )
    if op.get_context().dialect.name == 'mysql':
        indexes = set() if bind is None else {item['name'] for item in sa.inspect(bind).get_indexes(TABLE)}
        if INDEX not in indexes:
            op.create_index(INDEX, TABLE, ['content'], mysql_prefix='FULLTEXT', mysql_with_parser='ngram')


def downgrade():
    op.drop_table(TABLE)
