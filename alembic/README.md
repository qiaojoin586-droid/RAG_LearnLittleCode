# alembic 目录说明

> 本目录是数据库迁移机制的核心，**请勿删除**——应用每次启动都依赖它（见下文"启动流程"）。

## 一、它是什么

Alembic 是 SQLAlchemy 官方的数据库迁移工具。本项目用它管理 MySQL 表结构的演进历史：

- 每次表结构变更（建表/加列/加索引/数据迁移）都写成一个**迁移脚本**（revision）；
- 迁移按版本号链式排列，数据库用 `alembic_version` 表记录当前版本；
- 应用启动时自动执行 `alembic upgrade head`，把数据库升级到最新结构。

与 `Base.metadata.create_all` 的区别：create_all 只会"建缺失的表"，**不会**给已有表加列、改索引、迁移数据；alembic 支持完整的增量演进。本项目已统一走 alembic（见 `app/db/database.py` init_db 注释）。

## 二、目录结构

```
alembic/
├── env.py             # 迁移运行环境（异步引擎适配、.env 加载、日志配置）
├── script.py.mako     # 生成新迁移脚本的模板（alembic revision 时使用）
├── versions/          # 迁移脚本（按版本号链式排列）
│   ├── 06f20a30109b_initial_schema.py            # ① 初始建表（全量表结构）
│   └── a3f8d2c1b9e4_add_note_categories.py       # ② 分类管理功能（建表 + 加列 + 存量数据迁移）
└── README.md          # 本文档
```

注意：**alembic.ini 在项目根目录**（不在本目录内），`app/db/database.py` 通过 `Path(__file__).resolve().parents[2] / "alembic.ini"` 定位，支持任意 CWD 启动。

## 三、启动流程（应用自动执行）

应用启动（lifespan）时 `init_db()` 在线程中执行，流程：

1. **遗留库检测**：数据库有业务表但无 `alembic_version` 表（早期 create_all 时代建的库）→ 先 `alembic stamp head` 把版本标记为最新（假设 schema 与当前模型一致）；
2. **执行 `alembic upgrade head`**：
   - 空库 → 从第一个迁移开始全量建表；
   - 有版本的库 → 只执行比当前版本新的迁移；
3. 迁移完成日志：`数据库迁移完成 (alembic upgrade head)`。

因此**日常开发不需要手动跑迁移**——重启后端即可。手动操作仅用于调试/回滚（见下文命令）。

## 四、常用命令

在项目根目录、使用 venv 执行：

```bash
# 查看当前版本与迁移历史
.venv/Scripts/python.exe -m alembic current
.venv/Scripts/python.exe -m alembic history

# 升级到最新 / 回滚一步
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic downgrade -1

# 生成新迁移脚本（自动比对模型差异；生成后务必人工检查！）
.venv/Scripts/python.exe -m alembic revision --autogenerate -m "描述"

# 离线预览：只输出 SQL 不执行（数据迁移部分会被跳过，见 §五）
.venv/Scripts/python.exe -m alembic upgrade head --sql
```

## 五、本项目的关键约定与坑

### 1. env.py 的三个特殊处理（不要改回默认行为）

| 处理 | 原因 |
|------|------|
| 复用 `app.db.database.DATABASE_URL`（env 变量驱动） | 避免数据库配置双份漂移 |
| 迁移使用**独立异步引擎**（NullPool + 用完 dispose） | 应用启动时 init_db 在线程中 asyncio.run 调用本模块，复用应用级引擎会把连接绑到错误的 event loop |
| `fileConfig(..., disable_existing_loggers=False)` | 默认会禁用所有未在 alembic.ini 声明的 logger，导致 init_db 跑完迁移后应用日志全部静默（实测 2026-08-09） |
| CLI 运行前先 `load_dotenv()` | alembic 命令不经过 main.py 的 dotenv 加载，不加载会读到 MYSQL_* 默认值 |

### 2. 迁移脚本写法约定（与 ORM 模型一致）

- **UUID 主键**：Python 侧 `uuid.uuid4()` 生成（模型 default），迁移里不写 `DEFAULT (UUID())`；
- **时间戳**：`server_default=sa.text('now()')`，MySQL 会话时区由应用强制 UTC；
- **自引用外键**（如 note_categories.parent_id）：`ondelete='CASCADE'`，软删除逻辑由业务层处理、外键级联只作为物理清理兜底；
- **索引命名**：与模型 `index=True` 生成的 `ix_<表名>_<列名>` 保持一致（如 `ix_note_categories_user_id`）。

### 3. 数据迁移的写法（参考 a3f8d2c1b9e4）

- **自包含**：数据迁移用 SQLAlchemy Core（`sa.table` / `sa.column`）实现，**不导入 ORM 模型**——模型会随代码演进，迁移必须冻结在当时的语义上。模板树等常量在迁移里放**冻结副本**，并注明源头文件；
- **离线模式守卫**：`alembic upgrade head --sql` 只生成 DDL 不执行数据迁移，用 `if context.is_offline_mode(): return` 跳过 `op.get_bind()` 的数据操作（否则 mock 连接的 execute 返回 None 会报错）；
- **幂等与安全**：批量回填前先验证数据分布（可参考分类迁移的"干跑验证 → 正式执行 → 结果复核"三步流程）；
- **downgrade 要写**：删列删表按相反顺序，保证 `downgrade -1` 可用。

### 4. 生成新迁移后必须人工检查

`--autogenerate` 只是辅助——它不认识业务语义，以下情况它会做错：

- 会生成"删列重建"而不是改列（需要手动改成 `alter_column`）；
- 不会生成数据迁移（需手写，见上条）；
- 外键的 `ondelete` 行为、自引用关系需要人工确认。

## 六、现有迁移清单

| 版本 | 文件 | 内容 |
|------|------|------|
| `06f20a30109b` | `versions/06f20a30109b_initial_schema.py` | 初始建表（users/notes/chat/review/模板/知识库/审计等全部业务表） |
| `a3f8d2c1b9e4` | `versions/a3f8d2c1b9e4_add_note_categories.py` | 分类管理功能：`note_categories` 表 + `notes.category_id` 列/索引/外键 + 存量数据迁移（模板树实例化 + 字符串→ID 回填）。已随功能上线在生产库执行并验证 |

## 七、为什么不能删除本目录

1. 应用每次启动的 `init_db()` 都执行 `alembic upgrade head`，目录缺失 → 启动失败；
2. 数据库 `alembic_version` 表记录着版本号，迁移文件缺失但版本号还在 → `upgrade head` 报 "Can't locate revision"；
3. 未来任何表结构变更都要在 `versions/` 里新增迁移（例如双写期结束后移除 `notes.category` 字段）。

其中 `__pycache__/` 可随意删除（Python 缓存，自动重建），其余文件均需保留。
