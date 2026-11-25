"""
分类核心业务服务

实现分类的完整生命周期管理（见 docs/笔记分类管理功能设计方案.md v4）：
- 分类树构建（含子孙 note_count、未分类统计）
- 分类 CRUD（创建/更新/软删除/恢复）
- 移动 / 批量重排 / 合并（含循环引用、3 级限制、同级同名校验）
- 回收站查询与 14 天过期清理定时任务
- 模板分类树实例化（新用户注册 + 存量数据迁移复用）

核心约束：
- 所有方法均以 user_id 为必选参数，确保用户隔离
- 层级限制：顶级=0，最多 3 级（深度 0/1/2）
- 同级同名唯一（应用层校验，排除已软删除记录）
"""

import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError, ErrorCode
from app.models.category import NoteCategory
from app.models.note import Note
from app.utils.time_utils import utc_now
from app.schemas.category import (
    CategoryCreate, CategoryUpdate, CategoryMove, CategoryReorder,
    CategoryResponse, CategoryTreeResponse,
    DeletedCategoryResponse, DeletedCategoryListResponse,
)

# 回收站自动清理阈值（天）——与 app/core/scheduler.py 的 CLEANUP_EXPIRED_DAYS 保持一致
RECYCLE_BIN_CLEANUP_DAYS = 14

# 最大层级数（顶级=1 级，最多 3 级 → 深度 0/1/2）
MAX_DEPTH = 2


def would_exceed_depth(parent_depth: int, subtree_height: int) -> bool:
    """
    层级校验纯函数（抽出便于单测，设计 §7.2）

    正确推导：把 X 移到深度为 parent_depth 的父级 P 下，
    X 最深后代的新深度 = P.depth + 1 + X.subtree_height，
    3 级上限要求最深深度 ≤ 2（即 ≤ MAX_DEPTH），
    等价于 parent_depth + subtree_height ≤ 1，即 >= 2 时拒绝。

    合法组合：0+0（叶子到顶级）、0+1（1 层子树到顶级）、1+0（叶子到一级）
    非法组合：0+2、1+1、2+0（最深后代落到第 4 层）
    """
    return parent_depth + subtree_height >= MAX_DEPTH

# 模板分类树（与 front/src/constants/noteCategoryTree.ts 静态树一致；
# 迁移完成后前端删除该静态配置文件）
TEMPLATE_TREE: List[dict] = [
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


async def seed_template_tree(db: AsyncSession, user_id: str) -> Dict[str, NoteCategory]:
    """
    为指定用户创建模板分类树（新用户注册时调用）

    模板中所有节点名互不重复，因此可用 name → NoteCategory 映射；
    迁移脚本中同名匹配行为依赖此特性（用户笔记 category="脚本" 会归入 工作/脚本）。

    Args:
        db: 数据库会话
        user_id: 用户 ID

    Returns:
        name → NoteCategory 映射（含模板全部节点）
    """
    cat_map: Dict[str, NoteCategory] = {}

    async def _seed(nodes: List[dict], parent_id: Optional[str]) -> None:
        for i, node in enumerate(nodes):
            cat = NoteCategory(
                id=str(uuid.uuid4()),
                user_id=user_id,
                parent_id=parent_id,
                name=node["name"],
                sort_order=i,
            )
            db.add(cat)
            cat_map[node["name"]] = cat
            if node.get("children"):
                await _seed(node["children"], cat.id)

    await _seed(TEMPLATE_TREE, None)
    await db.flush()
    return cat_map


class CategoryService:
    """
    分类服务类

    所有方法均为无状态（每次调用传入 db 会话），
    通过 user_id 强制用户隔离，跨用户访问一律拒绝。
    """

    async def _get_active_category(
        self, db: AsyncSession, user_id: str, category_id: str
    ) -> NoteCategory:
        """查询活跃分类（归属校验；未找到或已软删除时抛 404）"""
        result = await db.execute(
            select(NoteCategory).where(
                NoteCategory.id == category_id,
                NoteCategory.user_id == user_id,
                NoteCategory.deleted_at.is_(None),
            )
        )
        cat = result.scalar_one_or_none()
        if not cat:
            raise BusinessError(code=ErrorCode.CATEGORY_NOT_FOUND, http_status=404)
        return cat

    async def _get_deleted_category(
        self, db: AsyncSession, user_id: str, category_id: str
    ) -> NoteCategory:
        """查询回收站中的分类（归属校验；未删除时抛 404）"""
        result = await db.execute(
            select(NoteCategory).where(
                NoteCategory.id == category_id,
                NoteCategory.user_id == user_id,
                NoteCategory.deleted_at.isnot(None),
            )
        )
        cat = result.scalar_one_or_none()
        if not cat:
            raise BusinessError(
                code=ErrorCode.CATEGORY_NOT_FOUND,
                message="回收站中未找到该分类",
                http_status=404,
            )
        return cat

    async def _collect_descendants(
        self, db: AsyncSession, user_id: str, category_id: str
    ) -> List[NoteCategory]:
        """递归收集某分类的所有活跃子孙分类（不含自身）"""
        all_cats = (
            await db.execute(
                select(NoteCategory).where(
                    NoteCategory.user_id == user_id,
                    NoteCategory.deleted_at.is_(None),
                )
            )
        ).scalars().all()

        children_map: Dict[Optional[str], List[NoteCategory]] = defaultdict(list)
        for cat in all_cats:
            children_map[cat.parent_id].append(cat)

        descendants: List[NoteCategory] = []

        def _walk(parent_id: str) -> None:
            for child in children_map.get(parent_id, []):
                descendants.append(child)
                _walk(child.id)

        _walk(category_id)
        return descendants

    async def get_depth(self, db: AsyncSession, category_id: Optional[str]) -> int:
        """获取分类的深度（顶级为 0；逐级向上遍历 parent 链）"""
        depth = 0
        current_id = category_id
        while current_id:
            result = await db.execute(
                select(NoteCategory.parent_id).where(NoteCategory.id == current_id)
            )
            current_id = result.scalar_one_or_none()
            if current_id:
                depth += 1
        return depth

    async def get_subtree_height(self, db: AsyncSession, user_id: str, category_id: str) -> int:
        """获取子树自身高度（叶子为 0，即子树内最深后代相对该分类的层数）"""
        all_cats = (
            await db.execute(
                select(NoteCategory.id, NoteCategory.parent_id).where(
                    NoteCategory.user_id == user_id,
                    NoteCategory.deleted_at.is_(None),
                )
            )
        ).all()

        children_map: Dict[Optional[str], List[str]] = defaultdict(list)
        for cat_id, pid in all_cats:
            children_map[pid].append(cat_id)

        memo: Dict[str, int] = {}

        def max_depth(cid: str) -> int:
            if cid in memo:
                return memo[cid]
            kids = children_map.get(cid, [])
            memo[cid] = 0 if not kids else 1 + max(max_depth(k) for k in kids)
            return memo[cid]

        return max_depth(category_id)

    async def would_create_cycle(
        self, db: AsyncSession, category_id: str, new_parent_id: Optional[str]
    ) -> bool:
        """检测将 category_id 移到 new_parent_id 下是否会形成循环（从目标向上遍历）"""
        if not new_parent_id:
            return False

        current = new_parent_id
        while current:
            if current == category_id:
                return True
            result = await db.execute(
                select(NoteCategory.parent_id).where(NoteCategory.id == current)
            )
            current = result.scalar_one_or_none()

        return False

    @staticmethod
    def _parent_condition(parent_id: Optional[str]):
        """构造 parent_id 过滤条件（NULL 需用 is_(None)）"""
        if parent_id is None:
            return NoteCategory.parent_id.is_(None)
        return NoteCategory.parent_id == parent_id

    async def check_same_name(
        self, db: AsyncSession, user_id: str, name: str,
        parent_id: Optional[str], exclude_id: Optional[str] = None,
    ) -> None:
        """校验同一 parent 下 name 唯一（排除已软删除记录和自身）"""
        query = select(NoteCategory.id).where(
            NoteCategory.user_id == user_id,
            self._parent_condition(parent_id),
            NoteCategory.name == name,
            NoteCategory.deleted_at.is_(None),
        )
        if exclude_id:
            query = query.where(NoteCategory.id != exclude_id)
        result = await db.execute(query)
        if result.scalar_one_or_none():
            raise BusinessError(code=ErrorCode.CATEGORY_NAME_EXISTS, http_status=409)

    async def _next_sort_order(
        self, db: AsyncSession, user_id: str, parent_id: Optional[str]
    ) -> int:
        """计算同级下一个排序值（现有最大值 + 1，新分类默认排末尾）"""
        result = await db.execute(
            select(func.max(NoteCategory.sort_order)).where(
                NoteCategory.user_id == user_id,
                self._parent_condition(parent_id),
                NoteCategory.deleted_at.is_(None),
            )
        )
        return (result.scalar() or 0) + 1

    def _to_response(
        self, cat: NoteCategory, children: List[CategoryResponse], note_count: int
    ) -> CategoryResponse:
        """ORM 对象 → 树形响应节点"""
        return CategoryResponse(
            id=cat.id,
            parent_id=cat.parent_id,
            name=cat.name,
            icon=cat.icon,
            color=cat.color,
            sort_order=cat.sort_order,
            created_at=cat.created_at,
            updated_at=cat.updated_at,
            children=children,
            note_count=note_count,
        )

    # ========== 分类树 ==========

    async def get_category_tree(
        self, db: AsyncSession, user_id: str
    ) -> CategoryTreeResponse:
        """获取分类树（含子孙 note_count 与未分类统计）"""
        # 1. 全部活跃分类
        cats = (
            await db.execute(
                select(NoteCategory).where(
                    NoteCategory.user_id == user_id,
                    NoteCategory.deleted_at.is_(None),
                )
            )
        ).scalars().all()

        # 2. 各分类直属笔记数（GROUP BY 一次查询）
        count_rows = await db.execute(
            select(Note.category_id, func.count()).where(
                Note.user_id == user_id,
                Note.deleted_at.is_(None),
                Note.category_id.isnot(None),
            ).group_by(Note.category_id)
        )
        own_counts: Dict[str, int] = dict(count_rows.all())

        # 3. 未分类笔记数
        uncategorized = await db.execute(
            select(func.count()).select_from(Note).where(
                Note.user_id == user_id,
                Note.deleted_at.is_(None),
                Note.category_id.is_(None),
            )
        )
        uncategorized_count = uncategorized.scalar() or 0

        # 4. 递归构建树（note_count 自底向上累加子孙）
        children_map: Dict[Optional[str], List[NoteCategory]] = defaultdict(list)
        for cat in cats:
            children_map[cat.parent_id].append(cat)

        count_memo: Dict[str, int] = {}

        def subtree_count(cat: NoteCategory) -> int:
            if cat.id in count_memo:
                return count_memo[cat.id]
            total = own_counts.get(cat.id, 0)
            for child in children_map.get(cat.id, []):
                total += subtree_count(child)
            count_memo[cat.id] = total
            return total

        def build_tree(parent_id: Optional[str]) -> List[CategoryResponse]:
            children = children_map.get(parent_id, [])
            children.sort(key=lambda x: (x.sort_order, x.name))
            return [
                self._to_response(cat, build_tree(cat.id), subtree_count(cat))
                for cat in children
            ]

        return CategoryTreeResponse(
            categories=build_tree(None),
            uncategorized_count=uncategorized_count,
        )

    # ========== 分类 CRUD ==========

    async def create_category(
        self, db: AsyncSession, user_id: str, data: CategoryCreate
    ) -> NoteCategory:
        """创建分类（校验父级归属、层级、同名）"""
        if data.parent_id:
            parent = await self._get_active_category(db, user_id, data.parent_id)
            if await self.get_depth(db, parent.id) >= MAX_DEPTH:
                raise BusinessError(
                    code=ErrorCode.INVALID_PARAMETER,
                    message="分类层级不能超过 3 级",
                )
            await self.check_same_name(db, user_id, data.name, parent.id)
        else:
            await self.check_same_name(db, user_id, data.name, None)

        cat = NoteCategory(
            id=str(uuid.uuid4()),
            user_id=user_id,
            parent_id=data.parent_id,
            name=data.name,
            icon=data.icon,
            color=data.color,
            sort_order=await self._next_sort_order(db, user_id, data.parent_id),
        )
        db.add(cat)
        await db.flush()
        await db.refresh(cat)  # 回读数据库生成的 created_at/updated_at

        logger.info(f"分类创建成功: category_id={cat.id}, user_id={user_id}, name={cat.name}")
        return cat

    async def update_category(
        self, db: AsyncSession, user_id: str, category_id: str, data: CategoryUpdate
    ) -> NoteCategory:
        """更新分类名称/图标/颜色（改父级走 move，排序走 reorder）"""
        cat = await self._get_active_category(db, user_id, category_id)

        update_data = data.model_dump(exclude_unset=True)
        if "name" in update_data:
            await self.check_same_name(
                db, user_id, update_data["name"], cat.parent_id, exclude_id=cat.id
            )
        for field, value in update_data.items():
            setattr(cat, field, value)

        await db.flush()
        await db.refresh(cat)  # 回读数据库生成的 updated_at
        logger.info(f"分类更新成功: category_id={cat.id}, fields={list(update_data.keys())}")
        return cat

    async def soft_delete_category(
        self, db: AsyncSession, user_id: str, category_id: str
    ) -> dict:
        """
        软删除分类（沿用笔记 14 天回收站惯例）

        子孙分类一并软删除；分类下的笔记（含子孙分类下）category_id 与
        category 字符串同步清空（双写期必须同时清空，否则降级查询/旧前端
        仍会把笔记显示在已删除分类下）。

        Returns:
            影响范围 { subcategory_count, note_count }（删除前的统计）
        """
        cat = await self._get_active_category(db, user_id, category_id)
        descendants = await self._collect_descendants(db, user_id, category_id)
        all_ids = [cat.id] + [d.id for d in descendants]

        # 统计影响范围（排除软删除笔记）
        note_count = await db.execute(
            select(func.count()).select_from(Note).where(
                Note.user_id == user_id,
                Note.category_id.in_(all_ids),
                Note.deleted_at.is_(None),
            )
        )
        note_count = note_count.scalar() or 0

        # 全部软删除
        now = utc_now()
        for c in [cat] + descendants:
            c.deleted_at = now

        # 笔记移为未分类（双写字段同步清空）
        if note_count:
            await db.execute(
                update(Note)
                .where(
                    Note.user_id == user_id,
                    Note.category_id.in_(all_ids),
                )
                .values(category_id=None, category=None)
            )

        await db.flush()

        impact = {
            "subcategory_count": len(descendants),
            "note_count": note_count,
        }
        logger.info(
            f"分类软删除成功: category_id={cat.id}, "
            f"subcategories={impact['subcategory_count']}, notes={impact['note_count']}"
        )
        return impact

    async def _promote_active_children(self, db: AsyncSession, cat: NoteCategory) -> None:
        """
        将某分类下未软删除的直接子分类提升为顶级（物理删除前的防御保护）

        设计流程下活跃节点父链必完整（恢复会级联恢复祖先），多层孤儿不可达，
        此处仅检查直接子级作为防御代码（与 cleanup_expired_categories 共用）。
        """
        active_children = (
            await db.execute(
                select(NoteCategory.id).where(
                    NoteCategory.parent_id == cat.id,
                    NoteCategory.user_id == cat.user_id,
                    NoteCategory.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not active_children:
            return

        # 重算 sort_order 避免与现有顶级分类撞值
        max_order = await db.execute(
            select(func.max(NoteCategory.sort_order)).where(
                NoteCategory.user_id == cat.user_id,
                NoteCategory.parent_id.is_(None),
            )
        )
        base = (max_order.scalar() or 0) + 1
        for i, cid in enumerate(active_children):
            await db.execute(
                update(NoteCategory)
                .where(NoteCategory.id == cid)
                .values(parent_id=None, sort_order=base + i)
            )

    async def permanent_delete_category(
        self, db: AsyncSession, user_id: str, category_id: str
    ) -> dict:
        """
        彻底删除回收站中的分类（物理删除，不可恢复）

        - 必须在回收站中（deleted_at 非空），否则 404
        - 软删除态的子孙由 parent_id ON DELETE CASCADE 一并物理清除
        - 活跃子分类（防御边界情况）先提升为顶级再删
        - 笔记由 category_id ON DELETE SET NULL 兜底（正常流程软删除时已清空关联）
        """
        cat = await self._get_deleted_category(db, user_id, category_id)

        # 活跃子分类保护（与 cleanup_expired_categories 同一防御逻辑）
        await self._promote_active_children(db, cat)

        await db.delete(cat)
        await db.flush()

        logger.info(f"分类彻底删除: category_id={cat.id}, name={cat.name}, user_id={user_id}")
        return {"deleted_name": cat.name}

    async def restore_category(
        self, db: AsyncSession, user_id: str, category_id: str
    ) -> dict:
        """
        从回收站恢复分类（级联恢复祖先链 + 子孙分类）

        ⚠️ 正确性说明：若只恢复当前分类，而父分类仍在回收站，会出现：
        a) 树按 parent_id 构建时该节点不可见（断链）
        b) 14 天后物理清理父分类时，ON DELETE CASCADE 会物理删除已恢复的子分类
        因此恢复时必须级联恢复所有已软删除的祖先，确保 parent_id 链完整。

        同时一并恢复当前分类的全部软删除子孙：删除时子孙随父级一起进了回收站，
        若恢复只带回父级而子分类留在回收站，用户会误以为子分类"丢失"。
        同级同名冲突时整体拒绝（先改名或先恢复现有分类再试）。
        """
        cat = await self._get_deleted_category(db, user_id, category_id)

        # 1. 向上遍历 parent_id 链，收集已软删除的祖先
        to_restore: List[NoteCategory] = [cat]
        current_id = cat.parent_id
        while current_id:
            result = await db.execute(
                select(NoteCategory).where(
                    NoteCategory.id == current_id,
                    NoteCategory.user_id == user_id,
                )
            )
            ancestor = result.scalar_one_or_none()
            if not ancestor:
                break
            if ancestor.deleted_at is not None:
                to_restore.append(ancestor)
            current_id = ancestor.parent_id

        # 2. 收集当前分类的全部已软删除子孙（递归，仅当前分类的子树，
        #    祖先分类的其他分支不受影响）
        all_cats = (
            await db.execute(
                select(NoteCategory).where(NoteCategory.user_id == user_id)
            )
        ).scalars().all()
        children_map: Dict[Optional[str], List[NoteCategory]] = defaultdict(list)
        for c in all_cats:
            children_map[c.parent_id].append(c)

        def collect_deleted_children(node: NoteCategory) -> None:
            for child in children_map.get(node.id, []):
                if child.deleted_at is not None:
                    to_restore.append(child)
                    collect_deleted_children(child)

        collect_deleted_children(cat)

        # 3. 同级同名校验（先校验后修改：任一冲突则整体拒绝，事务回滚）
        for r in to_restore:
            await self.check_same_name(db, user_id, r.name, r.parent_id, exclude_id=r.id)

        # 4. 统一清除 deleted_at
        for r in to_restore:
            r.deleted_at = None

        await db.flush()

        logger.info(
            f"分类恢复成功: category_id={cat.id}, 级联恢复 {len(to_restore)} 个（祖先 + 子孙）"
        )
        return {"restored_count": len(to_restore)}

    # ========== 移动 / 重排 / 合并 ==========

    async def move_category(
        self, db: AsyncSession, user_id: str, category_id: str, data: CategoryMove
    ) -> NoteCategory:
        """移动分类到新父级（含循环引用、层级、同名校验；排序走 reorder 端点）"""
        cat = await self._get_active_category(db, user_id, category_id)

        if data.parent_id:
            # 目标父级校验
            await self._get_active_category(db, user_id, data.parent_id)

            # 循环引用校验
            if await self.would_create_cycle(db, category_id, data.parent_id):
                raise BusinessError(
                    code=ErrorCode.INVALID_PARAMETER,
                    message="不能将分类移动到自身或其子孙分类下",
                )

            # 层级校验（含子树高度，防止整棵子树被推到 3 级以上，公式见 would_exceed_depth）
            parent_depth = await self.get_depth(db, data.parent_id)
            subtree_height = await self.get_subtree_height(db, user_id, category_id)
            if would_exceed_depth(parent_depth, subtree_height):
                raise BusinessError(
                    code=ErrorCode.INVALID_PARAMETER,
                    message=f"移动后子树层级将超过 3 级（当前子树高度 {subtree_height} 层）",
                )

            # 同级同名校验
            await self.check_same_name(db, user_id, cat.name, data.parent_id, exclude_id=cat.id)

        # 更新父级；sort_order 置为同级末尾，随后由前端调用 reorder 精确排序
        cat.parent_id = data.parent_id
        cat.sort_order = await self._next_sort_order(db, user_id, data.parent_id)
        await db.flush()
        await db.refresh(cat)  # 回读数据库生成的 updated_at

        logger.info(f"分类移动成功: category_id={cat.id}, new_parent={data.parent_id}")
        return cat

    async def reorder_categories(
        self, db: AsyncSession, user_id: str, data: CategoryReorder
    ) -> None:
        """批量重写同级分类的 sort_order（前端拖拽后提交完整顺序）"""
        if data.parent_id:
            await self._get_active_category(db, user_id, data.parent_id)

        # 1. 验证 ordered_ids 与该 parent 下所有未删除分类完全一致（不遗漏、不多余）
        result = await db.execute(
            select(NoteCategory.id).where(
                NoteCategory.user_id == user_id,
                self._parent_condition(data.parent_id),
                NoteCategory.deleted_at.is_(None),
            )
        )
        existing_ids = set(result.scalars().all())
        if set(data.ordered_ids) != existing_ids:
            raise BusinessError(
                code=ErrorCode.INVALID_PARAMETER,
                message="排序列表与当前同级分类不匹配",
            )

        # 2. 按新顺序重写 sort_order（0, 1, 2, ...）
        for i, cat_id in enumerate(data.ordered_ids):
            await db.execute(
                update(NoteCategory)
                .where(NoteCategory.id == cat_id, NoteCategory.user_id == user_id)
                .values(sort_order=i)
            )
        await db.flush()

        logger.info(
            f"分类重排成功: parent={data.parent_id}, count={len(data.ordered_ids)}"
        )

    async def merge_categories(
        self, db: AsyncSession, user_id: str, source_ids: List[str], target_id: str
    ) -> dict:
        """
        合并分类：将源分类下的笔记和子分类移至目标分类，然后软删除源分类

        冲突校验：
        - 目标不得是任何源分类的子孙（复用循环检测，防止子分类 parent 指向自身）
        - 源之间不得存在父子关系（批量合并请分开操作）
        - 源子分类移入目标后：同级同名校验 + 层级限制（子树高度）
        """
        source_ids = list(dict.fromkeys(source_ids))  # 去重且保持顺序

        # 1. 目标与源分类归属校验
        target = await self._get_active_category(db, user_id, target_id)
        if target_id in source_ids:
            raise BusinessError(
                code=ErrorCode.INVALID_PARAMETER, message="合并目标不能是待操作分类自身"
            )

        sources: List[NoteCategory] = []
        for sid in source_ids:
            cat = await self._get_active_category(db, user_id, sid)
            if await self.would_create_cycle(db, sid, target_id):
                raise BusinessError(
                    code=ErrorCode.INVALID_PARAMETER,
                    message="合并目标不能是源分类的子孙分类",
                )
            # 源之间不得存在父子关系
            for other in sources:
                if await self.would_create_cycle(db, other.id, sid):
                    raise BusinessError(
                        code=ErrorCode.INVALID_PARAMETER,
                        message="批量合并的分类不能包含父子关系，请分开操作",
                    )
            sources.append(cat)

        # 2. 合并时源分类本身消失，最深后代的新深度 = 目标深度 + 源子树高度。
        # 移动分类的 would_exceed_depth 会额外计算源分类这一层，此处不能复用。
        target_depth = await self.get_depth(db, target_id)
        for cat in sources:
            subtree_height = await self.get_subtree_height(db, user_id, cat.id)
            if target_depth + subtree_height > MAX_DEPTH:
                raise BusinessError(
                    code=ErrorCode.INVALID_PARAMETER,
                    message=f"合并后分类「{cat.name}」的子树层级将超过 3 级",
                )

        # 3. 源子分类移入目标后的同级同名校验
        child_rows = await db.execute(
            select(NoteCategory.id, NoteCategory.name).where(
                NoteCategory.user_id == user_id,
                NoteCategory.parent_id.in_(source_ids),
                NoteCategory.deleted_at.is_(None),
            )
        )
        for child_id, child_name in child_rows.all():
            await self.check_same_name(db, user_id, child_name, target_id, exclude_id=child_id)

        # 4. 源分类下笔记移至目标（双写：category 字符串同步为目标分类名）
        await db.execute(
            update(Note)
            .where(
                Note.user_id == user_id,
                Note.category_id.in_(source_ids),
            )
            .values(category_id=target_id, category=target.name)
        )

        # 5. 源子分类挂到目标下（sort_order 续在目标现有子分类之后）
        base_order = await self._next_sort_order(db, user_id, target_id)
        child_rows = (
            await db.execute(
                select(NoteCategory.id).where(
                    NoteCategory.user_id == user_id,
                    NoteCategory.parent_id.in_(source_ids),
                    NoteCategory.deleted_at.is_(None),
                ).order_by(NoteCategory.sort_order)
            )
        ).scalars().all()
        for i, child_id in enumerate(child_rows):
            await db.execute(
                update(NoteCategory)
                .where(NoteCategory.id == child_id, NoteCategory.user_id == user_id)
                .values(parent_id=target_id, sort_order=base_order + i)
            )

        # 6. 软删除源分类
        now = utc_now()
        for cat in sources:
            cat.deleted_at = now
        await db.flush()

        result = {"merged_count": len(sources), "target_id": target_id}
        logger.info(
            f"分类合并成功: sources={source_ids}, target={target_id}"
        )
        return result

    # ========== 回收站 ==========

    async def get_recycle_bin(
        self, db: AsyncSession, user_id: str
    ) -> DeletedCategoryListResponse:
        """获取回收站分类列表（按删除时间倒序，附带剩余天数与子孙数量）"""
        deleted = (
            await db.execute(
                select(NoteCategory).where(
                    NoteCategory.user_id == user_id,
                    NoteCategory.deleted_at.isnot(None),
                ).order_by(NoteCategory.deleted_at.desc())
            )
        ).scalars().all()

        # 子孙数量：统计其他已软删除分类中，祖先链经过该分类的数量
        children_map: Dict[Optional[str], List[NoteCategory]] = defaultdict(list)
        for cat in deleted:
            children_map[cat.parent_id].append(cat)

        def count_descendants(cat: NoteCategory) -> int:
            total = len(children_map.get(cat.id, []))
            for child in children_map.get(cat.id, []):
                total += count_descendants(child)
            return total

        now = utc_now()
        items = []
        for cat in deleted:
            days_elapsed = (now - cat.deleted_at).days if cat.deleted_at else 0
            items.append(DeletedCategoryResponse(
                id=cat.id,
                name=cat.name,
                icon=cat.icon,
                color=cat.color,
                deleted_at=cat.deleted_at,
                days_remaining=max(0, RECYCLE_BIN_CLEANUP_DAYS - days_elapsed),
                descendant_count=count_descendants(cat),
            ))

        return DeletedCategoryListResponse(categories=items, total=len(items))

    async def cleanup_expired_categories(self, db: AsyncSession, days: int = RECYCLE_BIN_CLEANUP_DAYS) -> int:
        """
        清理过期软删除分类（deleted_at 超过 days 天）→ 返回删除数量

        定时任务调用。物理删除前保护活跃子分类：
        注：恢复操作会级联恢复祖先链（见 restore_category），因此清理前所有
        活跃节点的父链必完整，多层孤儿在设计流程下不可达；此处仅检查直接子级，
        作为防御代码防止边界情况（如历史数据异常）。
        """
        cutoff = utc_now() - timedelta(days=days)

        result = await db.execute(
            select(NoteCategory).where(
                NoteCategory.deleted_at.isnot(None),
                NoteCategory.deleted_at < cutoff,
            )
        )
        expired = list(result.scalars().all())
        if not expired:
            return 0

        # 活跃子分类保护：物理删除前将有未软删除直接子分类的父级先"斩断"
        # （将活跃子分类提升为顶级；注：恢复操作会级联恢复祖先链，
        # 因此设计流程下活跃节点父链必完整，此处仅防御边界情况）
        for cat in expired:
            await self._promote_active_children(db, cat)

        # 物理删除（parent_id ON DELETE CASCADE 会一并清除软删除的子孙；
        # 笔记的 category_id ON DELETE SET NULL 自动置空）
        for cat in expired:
            await db.delete(cat)
        await db.flush()

        logger.info(f"清理过期分类完成: 删除 {len(expired)} 个")
        return len(expired)
