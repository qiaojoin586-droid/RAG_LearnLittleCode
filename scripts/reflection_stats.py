"""
Reflection 收益统计报表（P2 增强 1）

读取 logs/reflection_metrics.jsonl（record_reflection_metric 落盘的结构化指标），
输出 L1 触发率/修正率/最终通过率/批判延迟与 L2 修复成功率等收益数据。

用法：
    .venv/Scripts/python.exe -X utf8 scripts/reflection_stats.py
    .venv/Scripts/python.exe -X utf8 scripts/reflection_stats.py --file logs/reflection_metrics.jsonl
    .venv/Scripts/python.exe -X utf8 scripts/reflection_stats.py --since 2026-08-01
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# 项目根（scripts/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FILE = BASE_DIR / "logs" / "reflection_metrics.jsonl"


def load_metrics(path: Path, since: str = "") -> list:
    """读取 JSONL 指标；since 为 YYYY-MM-DD 时仅保留当日及之后记录"""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and row.get("ts", "")[:10] < since:
            continue
        rows.append(row)
    return rows


def pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "-"
    return f"{numerator / denominator * 100:.1f}%"


def report(rows: list) -> str:
    l1 = [r for r in rows if "triggered" in r]           # L1 自检记录（plan_execute + react）
    l1_triggered = [r for r in l1 if r["triggered"]]
    l2 = [r for r in rows if "l2_repair_triggered" in r]  # L2 修复记录
    l2_success = [r for r in l2 if r["l2_repair_success"]]
    by_task = Counter(r.get("task", "?") for r in rows)
    refined = [r for r in l1_triggered if r.get("refine_rounds", 0) > 0]
    passed = [r for r in l1_triggered if r.get("final_passed")]

    latency_ms = [
        ms for r in l1_triggered
        for ms in (r.get("critique_latency_ms") or [])
    ]

    lines = [
        "=" * 56,
        "Reflection 收益统计报表（P2 增强 1）",
        "=" * 56,
        f"总记录数: {len(rows)}（按 task 分布: {dict(by_task) or '无'}）",
        "",
        "--- L1 结果自检 ---",
        f"  自检记录: {len(l1)}",
        f"  触发批判: {len(l1_triggered)}（触发率 {pct(len(l1_triggered), len(l1))}）",
        f"  发生修正: {len(refined)}（修正率 {pct(len(refined), len(l1_triggered))}）",
        f"  最终通过: {len(passed)}（最终通过率 {pct(len(passed), len(l1_triggered))}）",
        f"  平均批判延迟: {sum(latency_ms) / len(latency_ms):.0f} ms" if latency_ms else "  平均批判延迟: -",
        "",
        "--- L2 工具失败修复 ---",
        f"  修复触发: {len(l2)}",
        f"  修复成功: {len(l2_success)}（成功率 {pct(len(l2_success), len(l2))}）",
        f"  修复失败工具分布: {dict(Counter(r.get('failed_tool', '?') for r in l2 if not r['l2_repair_success'])) or '无'}",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Reflection 收益统计报表")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="指标 JSONL 路径")
    parser.add_argument("--since", default="", help="起始日期 YYYY-MM-DD")
    args = parser.parse_args()

    rows = load_metrics(Path(args.file), args.since)
    if not rows:
        print(f"无指标数据（{args.file}）——先用应用跑几个触发反思的任务。")
        sys.exit(0)
    print(report(rows))


if __name__ == "__main__":
    main()
