"""
分类管理纯逻辑测试（P1: 无 DB / Redis / LLM 外部依赖）

覆盖分类管理功能（docs/笔记分类管理功能设计方案.md v4）的关键纯逻辑：
- 层级校验公式 would_exceed_depth（§7.2，含子树高度，v3 修复的 off-by-one）
- 批量操作 Schema 校验（merge 必须指定目标、目标不能是源自身）
- 批量重排 Schema 校验（ordered_ids 必填）

运行: python -m pytest tests/test_category_logic.py -v
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.services.category_service import CategoryService, would_exceed_depth, MAX_DEPTH
from app.core.failed_response import BusinessError
from app.schemas.category import CategoryBatchOperation, CategoryReorder


@pytest.mark.parametrize("target_depth,source_height,allowed", [
    (2, 0, True),   # 仅移动笔记，不增加目标分类层级
    (1, 1, True),   # 源子分类挂到目标下，最深处恰为第三级
    (0, 2, True),
    (2, 1, False),
    (1, 2, False),
])
def test_merge_uses_depth_after_source_category_is_removed(target_depth, source_height, allowed):
    service = CategoryService()
    categories = {key: SimpleNamespace(id=key, name=key, deleted_at=None)
                  for key in ("source", "target")}

    async def get_category(db, user_id, category_id):
        return categories[category_id]

    service._get_active_category = get_category
    service.would_create_cycle = AsyncMock(return_value=False)
    service.get_depth = AsyncMock(return_value=target_depth)
    service.get_subtree_height = AsyncMock(return_value=source_height)
    service._next_sort_order = AsyncMock(return_value=0)
    result = MagicMock()
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result), flush=AsyncMock())
    if allowed:
        merged = asyncio.run(service.merge_categories(db, "user", ["source"], "target"))
        assert merged["merged_count"] == 1
        assert categories["source"].deleted_at is not None
    else:
        with pytest.raises(BusinessError, match="超过 3 级"):
            asyncio.run(service.merge_categories(db, "user", ["source"], "target"))
        db.execute.assert_not_awaited()


# ========== 层级校验公式（设计 §7.2） ==========

class TestWouldExceedDepth:
    """正确推导：X 移到 P 下，X 最深后代新深度 = P.depth + 1 + X.subtree_height ≤ 2"""

    @pytest.mark.parametrize("parent_depth,subtree_height", [
        (0, 0),  # 叶子到顶级 → 深度 1 ✓
        (0, 1),  # 1 层子树到顶级 → 最深深度 2 ✓
        (1, 0),  # 叶子到一级 → 深度 2 ✓
    ])
    def test_legal_combinations(self, parent_depth, subtree_height):
        """合法组合：最深后代仍在 3 级以内"""
        assert would_exceed_depth(parent_depth, subtree_height) is False

    @pytest.mark.parametrize("parent_depth,subtree_height", [
        (0, 2),  # 2 层子树到顶级 → 最深后代深度 3（第 4 层）✗
        (1, 1),  # 1 层子树到一级 → 最深后代深度 3 ✗
        (2, 0),  # 叶子到孙级 → 深度 3 ✗
        (2, 2),  # 极端组合 ✗
    ])
    def test_illegal_combinations(self, parent_depth, subtree_height):
        """非法组合：最深后代落到第 4 层，必须拒绝"""
        assert would_exceed_depth(parent_depth, subtree_height) is True

    def test_boundary_is_max_depth(self):
        """阈值与 MAX_DEPTH 一致：parent_depth + subtree_height == MAX_DEPTH 即拒绝"""
        assert would_exceed_depth(0, MAX_DEPTH) is True
        assert would_exceed_depth(0, MAX_DEPTH - 1) is False


# ========== 批量操作 Schema 校验（设计 §3.1.1） ==========

class TestCategoryBatchOperation:
    def test_merge_requires_target(self):
        """merge 操作必须指定 merge_target_id"""
        with pytest.raises(ValidationError):
            CategoryBatchOperation(category_ids=["a"], operation="merge")

    def test_merge_target_cannot_be_source(self):
        """合并目标不能是待操作分类自身"""
        with pytest.raises(ValidationError):
            CategoryBatchOperation(
                category_ids=["a", "b"], operation="merge", merge_target_id="a"
            )

    def test_merge_valid(self):
        op = CategoryBatchOperation(
            category_ids=["a", "b"], operation="merge", merge_target_id="c"
        )
        assert op.merge_target_id == "c"

    def test_delete_requires_no_target(self):
        op = CategoryBatchOperation(category_ids=["a"], operation="delete")
        assert op.operation == "delete"

    def test_permanent_delete_valid(self):
        """回收站批量彻底删除（无目标要求）"""
        op = CategoryBatchOperation(category_ids=["a", "b"], operation="permanent_delete")
        assert op.operation == "permanent_delete"

    def test_restore_valid(self):
        """回收站批量恢复（无目标要求）"""
        op = CategoryBatchOperation(category_ids=["a"], operation="restore")
        assert op.operation == "restore"

    def test_merge_only_requires_target(self):
        """merge 之外的操作不要求 merge_target_id"""
        op = CategoryBatchOperation(category_ids=["a"], operation="permanent_delete")
        assert op.merge_target_id is None


# ========== 批量重排 Schema 校验（设计 §7.6） ==========

class TestCategoryReorder:
    def test_ordered_ids_required(self):
        """ordered_ids 必填且至少 1 项"""
        with pytest.raises(ValidationError):
            CategoryReorder(ordered_ids=[])

    def test_parent_optional(self):
        """parent_id 缺省表示顶级"""
        op = CategoryReorder(ordered_ids=["a", "b"])
        assert op.parent_id is None
        assert op.ordered_ids == ["a", "b"]
