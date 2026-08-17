"""
Reflection 收益统计落盘（P2 增强 1）

设计文档：AI对话Reflection反思机制设计方案.md §3.9/§7。
reflection_metric 结构化指标以 JSONL 追加落盘（logs/reflection_metrics.jsonl），
供 scripts/reflection_stats.py 统计触发率/修正率/延迟与 L2 修复成功率。

原则：度量是旁路——落盘失败只告警、绝不影响主流程。
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from app.core.logger_handler import logger
from app.utils.config import BASE_DIR

# 指标文件路径（环境变量可覆盖；默认项目根 logs/ 下，随 .gitignore 忽略）
METRICS_FILE = Path(
    os.getenv("REFLECTION_METRICS_FILE", str(BASE_DIR / "logs" / "reflection_metrics.jsonl"))
)


def record_reflection_metric(metric: Dict[str, Any]) -> None:
    """
    追加一条反思指标到 JSONL（失败静默——度量不能影响主流程）

    Args:
        metric: 指标字典，如 {"task": "plan_execute", "triggered": true, ...}；
                自动附加 ts 时间戳字段
    """
    try:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **metric}
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"reflection 指标落盘失败（忽略）: {e}")
