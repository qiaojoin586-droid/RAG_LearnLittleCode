"""Pin local index storage independently of semantic identity."""
from alembic import context,op
import sqlalchemy as sa

revision='c346e7f8a9b0'
down_revision='b235d6e7f8a9'
branch_labels=None
depends_on=None


def upgrade():
    if context.is_offline_mode():
        op.add_column('rag_index_versions',sa.Column('storage_binding',sa.JSON(),nullable=True))
        op.add_column('model_traces',sa.Column('index_context',sa.JSON(),nullable=True));return
    columns={item['name']:item for item in sa.inspect(op.get_bind()).get_columns('rag_index_versions')}
    if 'storage_binding' not in columns:
        op.add_column('rag_index_versions',sa.Column('storage_binding',sa.JSON(),nullable=True))
    elif not isinstance(columns['storage_binding']['type'],sa.JSON) or not columns['storage_binding']['nullable']:
        raise RuntimeError('Existing index storage binding shape differs')
    trace_columns={item['name']:item for item in sa.inspect(op.get_bind()).get_columns('model_traces')}
    if 'index_context' not in trace_columns:op.add_column('model_traces',sa.Column('index_context',sa.JSON(),nullable=True))
    elif not isinstance(trace_columns['index_context']['type'],sa.JSON) or not trace_columns['index_context']['nullable']:
        raise RuntimeError('Existing index trace context shape differs')


def downgrade():
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
            "SELECT 1 FROM model_traces WHERE index_context IS NOT NULL AND LOWER(JSON_TYPE(index_context)) <> 'null' LIMIT 1").first():
        raise RuntimeError('Index trace provenance exists; retain schema or explicitly archive it first')
    if not context.is_offline_mode() and op.get_bind().exec_driver_sql(
            "SELECT 1 FROM rag_index_versions WHERE storage_binding IS NOT NULL AND LOWER(JSON_TYPE(storage_binding)) <> 'null' LIMIT 1").first():
        raise RuntimeError('Pinned index storage exists; retain schema or explicitly archive bindings first')
    op.drop_column('rag_index_versions','storage_binding')
    op.drop_column('model_traces','index_context')
