"""
分类管理路由

端点（见 docs/笔记分类管理功能设计方案.md v4 §3.1）：
- GET /category/tree - 获取当前用户的分类树（含 note_count）
- POST /category - 创建分类
- PUT /category/{category_id} - 更新分类
- DELETE /category/{category_id} - 软删除分类（含子分类和笔记处理）
- POST /category/{category_id}/move - 移动分类（改父级）
- POST /category/{category_id}/restore - 从回收站恢复分类（级联恢复祖先链）
- POST /category/reorder - 批量重排同级分类顺序
- POST /category/batch - 批量操作（删除/合并）
- GET /category/recycle-bin - 回收站分类列表
"""

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger_handler import logger
from app.core.failed_response import BusinessError
from app.core.success_response import success_response
from app.db.database import get_db
from app.schemas.category import (
    CategoryCreate, CategoryUpdate, CategoryMove, CategoryReorder,
    CategoryBatchOperation, CategoryResponse,
)
from app.services.category_service import CategoryService
from app.utils.auth_utils import get_current_user_id

router = APIRouter()

# 分类服务无外部依赖（无向量库/模型），直接使用模块级实例
_category_service = CategoryService()


@router.get("/category/tree", summary="获取分类树")
async def get_category_tree(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户的分类树（含子孙 note_count 与未分类统计）"""
    result = await _category_service.get_category_tree(db, user_id)
    return success_response(data=result.model_dump())


@router.post("/category", summary="创建分类")
async def create_category(
    data: CategoryCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """创建分类（校验父级归属、层级不超过 3 级、同级同名唯一）"""
    cat = await _category_service.create_category(db, user_id, data)
    # ★ 显式 commit：前端写操作成功后立即重新拉取分类树，若依赖 get_db 的
    # auto-commit（响应发送后执行），刷新会竞态读到旧数据（实测可见性延迟 ~70ms）
    await db.commit()
    return success_response(
        data=CategoryResponse(
            id=cat.id, parent_id=cat.parent_id, name=cat.name,
            icon=cat.icon, color=cat.color, sort_order=cat.sort_order,
            created_at=cat.created_at, updated_at=cat.updated_at,
            children=[], note_count=0,
        ).model_dump(),
        message="分类创建成功",
    )


@router.put("/category/{category_id}", summary="更新分类")
async def update_category(
    category_id: str,
    data: CategoryUpdate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """更新分类名称/图标/颜色（改父级走 move，排序走 reorder）"""
    cat = await _category_service.update_category(db, user_id, category_id, data)
    await db.commit()  # 显式 commit（同 create_category 注释）
    return success_response(
        data=CategoryResponse(
            id=cat.id, parent_id=cat.parent_id, name=cat.name,
            icon=cat.icon, color=cat.color, sort_order=cat.sort_order,
            created_at=cat.created_at, updated_at=cat.updated_at,
            children=[], note_count=0,
        ).model_dump(),
        message="分类更新成功",
    )


@router.delete("/category/{category_id}", summary="软删除分类")
async def delete_category(
    category_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """软删除分类（子孙一并删除；分类下笔记移为未分类）"""
    impact = await _category_service.soft_delete_category(db, user_id, category_id)
    await db.commit()  # 显式 commit（同 create_category 注释）
    return success_response(
        data=impact,
        message=f"分类已删除（{impact['subcategory_count']} 个子分类、"
                f"{impact['note_count']} 篇笔记已移为未分类）",
    )


@router.post("/category/{category_id}/move", summary="移动分类")
async def move_category(
    category_id: str,
    data: CategoryMove,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """移动分类到新父级（NULL = 移到顶级；含循环引用与层级校验）"""
    cat = await _category_service.move_category(db, user_id, category_id, data)
    await db.commit()  # 显式 commit（同 create_category 注释）
    return success_response(
        data=CategoryResponse(
            id=cat.id, parent_id=cat.parent_id, name=cat.name,
            icon=cat.icon, color=cat.color, sort_order=cat.sort_order,
            created_at=cat.created_at, updated_at=cat.updated_at,
            children=[], note_count=0,
        ).model_dump(),
        message="分类移动成功",
    )


@router.post("/category/{category_id}/restore", summary="恢复分类")
async def restore_category(
    category_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """从回收站恢复分类（级联恢复祖先链，保证父链完整）"""
    result = await _category_service.restore_category(db, user_id, category_id)
    await db.commit()  # 显式 commit（同 create_category 注释）
    return success_response(data=result, message="分类恢复成功")


@router.delete("/category/{category_id}/permanent", summary="彻底删除分类")
async def permanent_delete_category(
    category_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """彻底删除回收站中的分类（物理删除，不可恢复；软删除态的子孙一并清除）"""
    result = await _category_service.permanent_delete_category(db, user_id, category_id)
    await db.commit()  # 显式 commit（同 create_category 注释）
    return success_response(data=result, message="分类已彻底删除")


@router.post("/category/reorder", summary="批量重排分类顺序")
async def reorder_categories(
    data: CategoryReorder,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """批量重写同级分类的 sort_order（前端拖拽后提交完整顺序）"""
    await _category_service.reorder_categories(db, user_id, data)
    await db.commit()  # 显式 commit（同 create_category 注释）
    return success_response(message="排序已更新")


@router.post("/category/batch", summary="批量操作分类")
async def batch_operation(
    data: CategoryBatchOperation,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    批量操作分类（软删除 / 合并 / 彻底删除 / 恢复）

    - delete：批量软删除多个分类（各自级联处理子分类和笔记）
    - merge：将源分类下的笔记和子分类全部移至 merge_target_id，然后软删除源分类
    - permanent_delete：批量彻底删除回收站中的分类（物理删除，不可恢复）
    - restore：批量从回收站恢复分类（级联恢复祖先链）
    """
    if data.operation == 'delete':
        total_notes = 0
        total_subs = 0
        for cid in data.category_ids:
            impact = await _category_service.soft_delete_category(db, user_id, cid)
            total_notes += impact["note_count"]
            total_subs += impact["subcategory_count"]
        await db.commit()  # 显式 commit（同 create_category 注释）
        return success_response(
            data={
                "operation": "delete",
                "total": len(data.category_ids),
                "subcategory_count": total_subs,
                "note_count": total_notes,
            },
            message=f"批量删除完成：{len(data.category_ids)} 个分类",
        )

    if data.operation == 'merge':
        result = await _category_service.merge_categories(
            db, user_id, data.category_ids, data.merge_target_id
        )
        await db.commit()  # 显式 commit（同 create_category 注释）
        return success_response(
            data={"operation": "merge", **result},
            message=f"合并完成：{result['merged_count']} 个分类已并入目标分类",
        )

    # permanent_delete / restore：逐条处理并收集失败项（与 /note/batch 同模式，
    # 部分失败不影响其余条目；restore 级联恢复祖先可能导致后续条目"已恢复"报错，属预期）
    success_ids = []
    errors = []
    for cid in data.category_ids:
        try:
            if data.operation == 'permanent_delete':
                await _category_service.permanent_delete_category(db, user_id, cid)
            else:
                await _category_service.restore_category(db, user_id, cid)
            success_ids.append(cid)
        except BusinessError as e:
            errors.append({"category_id": cid, "error": str(e)})
            logger.warning(f"批量{data.operation}单条失败: category_id={cid}, error={e}")

    await db.commit()  # 显式 commit（同 create_category 注释）

    action_label = "彻底删除" if data.operation == 'permanent_delete' else "恢复"
    return success_response(
        data={
            "operation": data.operation,
            "total": len(data.category_ids),
            "success_count": len(success_ids),
            "error_count": len(errors),
            "errors": errors if errors else None,
        },
        message=f"批量{action_label}完成：成功 {len(success_ids)} 个，失败 {len(errors)} 个",
    )


@router.get("/category/recycle-bin", summary="回收站分类列表")
async def list_recycle_bin(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取回收站分类列表（按删除时间倒序，附带剩余天数）"""
    result = await _category_service.get_recycle_bin(db, user_id)
    return success_response(data=result.model_dump())
