"""Publish note spans and their keyword index with the note job."""
import re

from alembic import context, op
import sqlalchemy as sa

revision = 'ce28a435b197'
down_revision = 'b74e11c832af'
branch_labels = None
depends_on = None
TABLE = 'note_search_chunks'


def upgrade():
    inspector = None if context.is_offline_mode() else sa.inspect(op.get_bind())
    if inspector is None or 'source_manifest' not in {c['name'] for c in inspector.get_columns('note_index_jobs')}:
        op.add_column('note_index_jobs', sa.Column('source_manifest', sa.JSON(none_as_null=True)))
    if inspector is None or not inspector.has_table(TABLE):
        options = {}
        if op.get_context().dialect.name == 'mysql':
            collation = 'utf8mb4_unicode_ci'
            if inspector is not None:
                charset, collation = op.get_bind().execute(sa.text(
                    "SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='notes' AND COLUMN_NAME='id'"
                )).one()
                if charset != 'utf8mb4' or not re.fullmatch(r'utf8mb4_[a-z0-9_]+', collation or ''):
                    raise RuntimeError('Note chunk foreign key requires a verified utf8mb4 note ID collation')
            options = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate=collation)
        op.create_table(TABLE,
            sa.Column('note_id', sa.String(36), sa.ForeignKey('notes.id', ondelete='CASCADE'), primary_key=True),
            sa.Column('ordinal', sa.Integer, primary_key=True),
            sa.Column('revision', sa.String(36), nullable=False),
            sa.Column('title', sa.Text, nullable=False), sa.Column('content', sa.Text, nullable=False),
            **options)
    if op.get_context().dialect.name == 'mysql' and (
            inspector is None or 'ft_note_chunks_text' not in {i['name'] for i in inspector.get_indexes(TABLE)}):
        op.create_index('ft_note_chunks_text', TABLE, ['title', 'content'], mysql_prefix='FULLTEXT', mysql_with_parser='ngram')


def downgrade():
    if context.is_offline_mode() or op.get_bind().scalar(sa.text(
            'SELECT 1 FROM note_index_jobs WHERE source_manifest IS NOT NULL LIMIT 1')):
        raise RuntimeError('Published note chunks require a compatible index before downgrade')
    op.drop_table(TABLE)
    op.drop_column('note_index_jobs', 'source_manifest')
