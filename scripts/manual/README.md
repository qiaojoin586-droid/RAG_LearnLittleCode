# 手动验证脚本（非 CI 测试）

此目录收纳从项目根目录移入的调试/手工验证脚本。**它们不是单元测试**：
依赖真实 MySQL/Redis、真实 LLM/外部 API，CI 不会执行（正式测试在 `tests/` 下）。

> ⚠️ 运行前需准备：`.env` 已配置（DB/Redis/API Key）、MySQL/Redis 已启动。

## 当前保留

| 文件 | 用途 |
| :--- | :--- |
| `mysql_backup.sh` | MySQL 备份脚本（生产风险分析），从 .env 读取配置，mysqldump 一致性快照 + gzip 压缩 |

## 已清理（2026-08-21）

以下文件已验证完毕并删除：

- `test_mcp_phase1.py` — MCP 工具接入集成验证
- `test_external_api_tools.py` — 外部 API 工具联调
- `test_plan_execute.py` — Plan-and-Execute 端到端验证
- `test_repeat_fix.py` — Agent 重复输出修复回归验证
- `test_aspose_renderer.py` — Aspose 渲染测试
- `cleanup_orphan_tool_files.py` — 一次性孤儿文件清理运维
- `verify_katex_tmp.js` / `verify_tz_tmp.py` / `test_img_tmp.png` — 一次性临时验证文件
