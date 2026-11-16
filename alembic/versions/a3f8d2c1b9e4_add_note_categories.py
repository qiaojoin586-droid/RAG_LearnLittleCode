"""add note_categories

Revision ID: a3f8d2c1b9e4
Revises: 06f20a30109b
Create Date: 2026-08-21

分类管理功能（docs/笔记分类管理功能设计方案.md v4）：
1. 创建 note_categories 表（树形分类 + 软删除）
2. notes 表新增 category_id 列（双写过渡，category 字符串保留 6 个月）
3. 存量数据迁移：逐用户将 category 字符串映射到分类记录

⚠️ 数据迁移使用 SQLAlchemy Core 自包含实现（不依赖 ORM 模型，保证迁移历史稳定）。
TEMPLATE_TREE 为模板树的冻结副本（源模板见 app/services/category_service.py；
新用户注册走 live 代码，本迁移只负责存量用户）。
"""
from typing import Sequence, Union

import uuid

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f8d2c1b9e4'
down_revision: Union[str, None] = '06f20a30109b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 模板分类树冻结副本（与迁移当时的 front/src/constants/noteCategoryTree.ts 一致）
TEMPLATE_TREE = [
    {"name": "工作", "children": [
        {"name": "脚本"}, {"name": "项目文档", "children": [{"name": "XXX服务_V1.0.06"}]}
    ]},
    {"name": "学习"},
    {"name": "技术", "children": [
        {"name": "Flutter"}, {"name": "Java"}, {"name": "Nodejs"}, {"name": "Vue"}
    ]},
    {"name": "生活", "children": [
        {"name": "手工"}, {"name": "旅游"}, {"name": "育儿"}, {"name": "菜谱"}
    ]},
    {"name": "闪念"},
    {"name": "阅读"},
    {"name": "三体"},
    {"name": "其他"},
]


def migrate_category_data(bind) -> None:
    """逐用户迁移：字符串 category → category_id（含模板树实例化）"""
    users_t = sa.table('users', sa.column('uuid', sa.String))
    notes_t = sa.table(
        'notes',
        sa.column('user_id', sa.String),
        sa.column('category', sa.String),
        sa.column('category_id', sa.String),
    )
    cats_t = sa.table(
        'note_categories',
        sa.column('id', sa.String),
        sa.column('user_id', sa.String),
        sa.column('parent_id', sa.String),
        sa.column('name', sa.String),
        sa.column('sort_order', sa.Integer),
        sa.column('deleted_at', sa.DateTime),
    )

    user_ids = [row[0] for row in bind.execute(sa.select(users_t.c.uuid))]

    for user_id in user_ids:
        # ① 提取该用户所有不重复的非空 category 值
        rows = bind.execute(
            sa.select(notes_t.c.category)
            .where(
                notes_t.c.user_id == user_id,
                notes_t.c.category.isnot(None),
                notes_t.c.category != "",
            )
            .distinct()
        )
        category_values = [row[0] for row in rows]

        # ② 以模板树为蓝本创建完整分类树（保留静态树设计的层级结构）
        #    同名匹配行为：模板嵌套节点（如 工作/脚本）也会写入 name 映射，
        #    历史笔记 category="脚本" 将归入 工作/脚本（期望行为）
        existing = list(bind.execute(
            sa.select(cats_t.c.id, cats_t.c.parent_id, cats_t.c.name).where(
                cats_t.c.user_id == user_id,
                cats_t.c.deleted_at.is_(None),
            )
        ))
        existing_by_parent = {(row.parent_id, row.name): row.id for row in existing}
        name_to_id = {row.name: row.id for row in existing}

        def seed(nodes, parent_id):
            for i, node in enumerate(nodes):
                cid = existing_by_parent.get((parent_id, node["name"]))
                if cid is None:
                    cid = str(uuid.uuid4())
                    bind.execute(cats_t.insert().values(
                        id=cid, user_id=user_id, parent_id=parent_id,
                        name=node["name"], sort_order=i,
                    ))
                name_to_id[node["name"]] = cid
                if node.get("children"):
                    seed(node["children"], cid)

        seed(TEMPLATE_TREE, None)

        # ③ 任意字符串值：模板中不存在的额外创建为顶级分类
        for cat_str in category_values:
            if cat_str not in name_to_id:
                cid = str(uuid.uuid4())
                bind.execute(cats_t.insert().values(
                    id=cid, user_id=user_id, parent_id=None,
                    name=cat_str, sort_order=len(name_to_id),
                ))
                name_to_id[cat_str] = cid

        # ④ 批量回填 notes.category_id
        for cat_str, cid in name_to_id.items():
            bind.execute(
                notes_t.update()
                .where(
                    notes_t.c.user_id == user_id,
                    notes_t.c.category == cat_str,
                    notes_t.c.category_id.is_(None),
                )
                .values(category_id=cid)
            )


def upgrade() -> None:
    """升级迁移：建表 + 加列 + 存量数据迁移"""
    offline = context.is_offline_mode()
    inspector = None if offline else sa.inspect(op.get_bind())
    create_categories = inspector is None or not inspector.has_table('note_categories')
    columns = set() if inspector is None else {
        col['name'] for col in inspector.get_columns('notes')
    }
    add_category_id = 'category_id' not in columns

    # 1. 创建 note_categories 表
    if create_categories:
        _create_category_table()

    # 旧版 create_all 可能已创建部分结构；逐项检查，避免重复 DDL。
    category_indexes = set() if create_categories else {
        index['name'] for index in inspector.get_indexes('note_categories')
    }
    for name, column in (
        ('ix_note_categories_user_id', 'user_id'),
        ('ix_note_categories_parent_id', 'parent_id'),
    ):
        if name not in category_indexes:
            op.create_index(name, 'note_categories', [column], unique=False)

    if add_category_id:
        op.add_column('notes', sa.Column('category_id', sa.String(length=36), nullable=True))
    note_indexes = set() if inspector is None else {
        index['name'] for index in inspector.get_indexes('notes')
    }
    if 'ix_notes_category_id' not in note_indexes:
        op.create_index('ix_notes_category_id', 'notes', ['category_id'], unique=False)
    foreign_keys = [] if inspector is None else inspector.get_foreign_keys('notes')
    if not any(
        fk['constrained_columns'] == ['category_id'] and fk['referred_table'] == 'note_categories'
        for fk in foreign_keys
    ):
        op.create_foreign_key(
            'fk_notes_category_id_note_categories',
            'notes', 'note_categories', ['category_id'], ['id'], ondelete='SET NULL',
        )

    if not offline and (create_categories or add_category_id):
        migrate_category_data(op.get_bind())


def _create_category_table() -> None:
    op.create_table(
        'note_categories',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('parent_id', sa.String(length=36), nullable=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('icon', sa.String(length=50), nullable=True),
        sa.Column('color', sa.String(length=20), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.uuid'], ondelete='CASCADE'),
        # 自引用：父分类物理删除时子孙一并清除（软删除由业务层处理，不走外键级联）
        sa.ForeignKeyConstraint(['parent_id'], ['note_categories.id'], ondelete='CASCADE'),
    )


def downgrade() -> None:
    """回滚迁移：删列 + 删表"""
    op.drop_constraint('fk_notes_category_id_note_categories', 'notes', type_='foreignkey')
    op.drop_index('ix_notes_category_id', table_name='notes')
    op.drop_column('notes', 'category_id')
    op.drop_index('ix_note_categories_parent_id', table_name='note_categories')
    op.drop_index('ix_note_categories_user_id', table_name='note_categories')
    op.drop_table('note_categories')
