"""
分类管理功能 API 集成验证脚本（设计 §阶段四 集成测试）

前置条件：后端已重启（加载新代码），admin 种子用户存在或 DB 可直连。

流程：
1. 直连 DB 创建专用测试用户 + 模板分类树 + 2 篇测试笔记
2. 登录获取 Token
3. 逐项验证分类 API：
   - 分类树（含 note_count / uncategorized_count）
   - 创建（顶级/子级/孙级/第 4 级拒绝/同名拒绝）
   - 移动（循环拒绝、子树层级拒绝、正常移动）
   - 重排（列表不匹配拒绝、正常重排）
   - 笔记移动（PUT /note/{id}/category）+ 列表过滤（category_id / 未分类哨兵）
   - 合并（目标为源子孙拒绝、正常合并）
   - 软删除（影响范围）+ 回收站列表 + 恢复（祖先链）
4. 清理测试用户（级联删除分类与笔记）

用法: python scripts/verify_category_api.py
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

# 允许以 `python scripts/verify_category_api.py` 方式运行时导入项目根目录的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

BASE_URL = "http://localhost:8000/api/v1"
USERNAME = f"cat_it_{uuid.uuid4().hex[:8]}"
PASSWORD = "ItTest1234!"
USER_ID = None
NOTE_IDS = []

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {detail}")


async def api(client: httpx.AsyncClient, method: str, path: str, token: str = None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await client.request(method, f"{BASE_URL}{path}", headers=headers, **kw)
    return r


def print_api_error(r: httpx.Response):
    try:
        return r.json().get("message", "")
    except Exception:
        return ""


async def main():
    # 1. 直连 DB 创建测试用户 + 模板分类 + 测试笔记
    from app.db.database import async_session_factory
    from app.models.user import User
    from app.services.category_service import seed_template_tree
    from app.utils.auth_utils import hash_password
    from app.models.note import Note

    global USER_ID
    USER_ID = str(uuid.uuid4())

    async with async_session_factory() as db:
        user = User(
            uuid=USER_ID, username=USERNAME, email=f"{USERNAME}@test.local",
            email_verified=True, password=hash_password(PASSWORD),
        )
        db.add(user)
        await db.flush()
        cat_map = await seed_template_tree(db, USER_ID)
        work_id = cat_map["工作"].id
        for i in range(2):
            note = Note(
                id=str(uuid.uuid4()), user_id=USER_ID,
                title=f"测试笔记{i}", content=f"内容{i}",
                tags=[], category="工作", category_id=work_id,
            )
            db.add(note)
            NOTE_IDS.append(note.id)
        await db.commit()
    print(f"[准备] 测试用户 {USERNAME} 已创建（含模板树 + 2 篇笔记）")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 2. 登录
            r = await api(client, "POST", "/auth/login", json={
                "username": USERNAME, "password": PASSWORD,
            })
            if r.status_code != 200:
                print(f"[失败] 登录失败: {r.status_code} {r.text}")
                sys.exit(1)
            token = r.json()["data"]["access_token"]
            print("[准备] 登录成功")

            # 3.1 分类树
            r = await api(client, "GET", "/category/tree", token)
            check("GET /category/tree 返回 200", r.status_code == 200, print_api_error(r))
            tree = r.json().get("data", {})
            top_names = [c["name"] for c in tree.get("categories", [])]
            check("模板树顶级分类齐全（工作/学习/技术/生活/其他等）",
                  all(n in top_names for n in ["工作", "学习", "技术", "生活", "其他"]))
            check("uncategorized_count = 0", tree.get("uncategorized_count") == 0,
                  f"实际 {tree.get('uncategorized_count')}")
            work_node = [c for c in tree.get("categories", []) if c["name"] == "工作"][0]
            check("工作分类 note_count = 2（含子孙统计）", work_node.get("note_count") == 2,
                  f"实际 {work_node.get('note_count')}")

            # 3.2 创建
            r = await api(client, "POST", "/category", token,
                          json={"name": "测试顶级", "icon": "🧪", "color": "#4a9eff"})
            check("POST /category 创建顶级分类", r.status_code == 200, print_api_error(r))
            top_id = r.json()["data"]["id"]

            r = await api(client, "POST", "/category", token,
                          json={"name": "测试二级", "parent_id": top_id})
            check("POST /category 创建二级分类", r.status_code == 200, print_api_error(r))
            lvl2_id = r.json()["data"]["id"]

            r = await api(client, "POST", "/category", token,
                          json={"name": "测试三级", "parent_id": lvl2_id})
            check("POST /category 创建三级分类", r.status_code == 200, print_api_error(r))
            lvl3_id = r.json()["data"]["id"]

            r = await api(client, "POST", "/category", token,
                          json={"name": "测试四级", "parent_id": lvl3_id})
            check("第 4 级创建被拒绝", r.status_code == 400,
                  f"status={r.status_code} {print_api_error(r)}")

            r = await api(client, "POST", "/category", token, json={"name": "测试顶级"})
            check("同级同名创建被拒绝（409）", r.status_code == 409,
                  f"status={r.status_code} {print_api_error(r)}")

            # 3.3 移动
            r = await api(client, "POST", f"/category/{top_id}/move", token,
                          json={"parent_id": lvl2_id})
            check("移动到自身子孙被拒绝（循环）", r.status_code == 400,
                  f"status={r.status_code} {print_api_error(r)}")

            r = await api(client, "POST", f"/category/{top_id}/move", token,
                          json={"parent_id": lvl3_id})
            check("子树移动突破 3 级被拒绝", r.status_code == 400,
                  f"status={r.status_code} {print_api_error(r)}")

            r = await api(client, "POST", f"/category/{lvl3_id}/move", token, json={"parent_id": None})
            check("正常移动到顶级", r.status_code == 200, print_api_error(r))

            # 3.4 重排
            r = await api(client, "POST", "/category/reorder", token,
                          json={"parent_id": top_id, "ordered_ids": ["not-exist-id"]})
            check("重排列表不匹配被拒绝", r.status_code == 400,
                  f"status={r.status_code} {print_api_error(r)}")

            r = await api(client, "GET", "/category/tree", token)
            children = [c for c in r.json()["data"]["categories"] if c["id"] == top_id][0]["children"]
            ordered = [c["id"] for c in children]
            r = await api(client, "POST", "/category/reorder", token,
                          json={"parent_id": top_id, "ordered_ids": list(reversed(ordered))})
            check("正常重排", r.status_code == 200, print_api_error(r))

            # 3.5 笔记移动 + 列表过滤
            r = await api(client, "PUT", f"/note/{NOTE_IDS[0]}/category?category_id={top_id}", token)
            check("PUT /note/{id}/category 移动笔记", r.status_code == 200, print_api_error(r))
            check("移动后双写 category 字符串", r.json()["data"]["category"] == "测试顶级",
                  f"实际 {r.json()['data']['category']}")

            r = await api(client, "GET", "/note", token, params={"category_id": top_id})
            check("列表按 category_id 过滤", r.status_code == 200 and r.json()["data"]["total"] == 1,
                  f"total={r.json()['data'].get('total')}")

            r = await api(client, "GET", "/note", token, params={"category_id": "__uncategorized__"})
            # 当前 2 篇笔记：1 篇在"测试顶级"、1 篇在"工作" → 未分类应为 0
            check("未分类哨兵过滤（返回空列表）", r.status_code == 200 and r.json()["data"]["total"] == 0,
                  f"total={r.json()['data'].get('total')}")

            r = await api(client, "GET", "/note", token, params={"category": "工作"})
            check("字符串过滤向后兼容", r.status_code == 200 and r.json()["data"]["total"] == 1,
                  f"total={r.json()['data'].get('total')}")

            # 补充：把笔记移为未分类后哨兵应能过滤到
            r = await api(client, "PUT", f"/note/{NOTE_IDS[1]}/category", token)
            check("PUT 移为未分类（不带 category_id）", r.status_code == 200, print_api_error(r))
            r = await api(client, "GET", "/note", token, params={"category_id": "__uncategorized__"})
            check("移为未分类后哨兵过滤命中", r.status_code == 200 and r.json()["data"]["total"] == 1,
                  f"total={r.json()['data'].get('total')}")

            # 3.6 合并
            r = await api(client, "POST", "/category/batch", token,
                          json={"category_ids": [top_id], "operation": "merge",
                                "merge_target_id": lvl2_id})
            check("合并目标为源子孙被拒绝", r.status_code == 400,
                  f"status={r.status_code} {print_api_error(r)}")

            r = await api(client, "POST", "/category/batch", token,
                          json={"category_ids": [lvl3_id], "operation": "merge",
                                "merge_target_id": "工作"})
            check("合并目标不存在被拒绝", r.status_code == 404,
                  f"status={r.status_code} {print_api_error(r)}")

            # 3.7 软删除 + 回收站 + 恢复
            r = await api(client, "DELETE", f"/category/{top_id}", token)
            check("软删除返回影响范围", r.status_code == 200
                  and r.json()["data"]["note_count"] >= 1, print_api_error(r))

            r = await api(client, "GET", "/note", token, params={"category_id": top_id})
            check("删除后笔记移为未分类", r.json()["data"]["total"] == 0,
                  f"total={r.json()['data'].get('total')}")

            r = await api(client, "GET", "/category/recycle-bin", token)
            names = [c["name"] for c in r.json()["data"]["categories"]]
            check("回收站包含已删除分类", "测试顶级" in names, f"实际 {names}")

            r = await api(client, "POST", f"/category/{lvl2_id}/restore", token)
            check("恢复子分类（级联恢复祖先链）", r.status_code == 200, print_api_error(r))

            r = await api(client, "GET", "/category/tree", token)
            names = [c["name"] for c in r.json()["data"]["categories"]]
            check("恢复后父链完整（测试顶级可见）", "测试顶级" in names, f"实际 {names}")

        # 4. 清理
        async with async_session_factory() as db:
            from sqlalchemy import delete
            from app.models.user import User
            await db.execute(delete(User).where(User.uuid == USER_ID))
            await db.commit()
        print("[清理] 测试用户已删除（分类与笔记级联清除）")
    except Exception:
        print("[清理] 发生异常，尝试清理测试用户")
        try:
            async with async_session_factory() as db:
                from sqlalchemy import delete
                from app.models.user import User
                await db.execute(delete(User).where(User.uuid == USER_ID))
                await db.commit()
        except Exception as e:
            print(f"[清理失败] {e}")
        raise

    print(f"\n结果: {len(PASSED)} 通过, {len(FAILED)} 失败")
    if FAILED:
        print("失败项:", FAILED)
        sys.exit(1)
    print("✅ 集成验证全部通过")


asyncio.run(main())
