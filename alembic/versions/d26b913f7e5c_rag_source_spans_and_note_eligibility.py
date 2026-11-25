"""Record published source spans and let notes opt out of vector indexing."""

from alembic import context, op
import sqlalchemy as sa


revision = 'd26b913f7e5c'
down_revision = 'c72d19e85a4b'
branch_labels = None
depends_on = None


def upgrade():
    inspector = None if context.is_offline_mode() else sa.inspect(op.get_bind())
    if not inspector or 'is_indexed' not in {row['name'] for row in inspector.get_columns('notes')}:
        op.add_column('notes', sa.Column('is_indexed', sa.Boolean(), server_default=sa.true(), nullable=False))
    if not inspector or 'source_manifest' not in {row['name'] for row in inspector.get_columns('knowledge_documents')}:
        op.add_column('knowledge_documents', sa.Column('source_manifest', sa.JSON(), nullable=True))


def downgrade():
    if not context.is_offline_mode():
        bind = op.get_bind()
        if (bind.exec_driver_sql('SELECT 1 FROM notes WHERE is_indexed = false LIMIT 1').first()
                or bind.exec_driver_sql('SELECT 1 FROM knowledge_documents WHERE source_manifest IS NOT NULL LIMIT 1').first()):
            raise RuntimeError('RAG source manifests or note indexing preferences exist')
    op.drop_column('knowledge_documents', 'source_manifest')
    op.drop_column('notes', 'is_indexed')
