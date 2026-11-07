"""Expand model-selected interpretation types only when supported by actual tool results.

Model choices identify the analytical focus. Numeric values, rule labels, ordering,
and data limitations are resolved from tool output, never from model-authored prose.
"""

import json

from app.agent.contracts import Interpretation, ToolTrace

INTERPRETATION_TOOLS = {
    "campaign_list": {"list_campaigns"},
    "records": {"query_campaigns", "get_campaign_detail"},
    "performance": {"summarize_campaign_performance"},
    "ranking": {"compare_campaigns"},
    "anomaly_screening": {"detect_campaign_anomalies"},
}


def render_interpretation(item: Interpretation, trace: ToolTrace, result: dict) -> str:
    if trace.tool_name not in INTERPRETATION_TOOLS[item.kind]:
        raise ValueError("Interpretation does not match tool evidence.")
    citation = f"[{item.call_id}] "
    if item.kind == "campaign_list":
        return citation + f"当前数据集包含 {result['campaign_count']} 个广告计划。"
    if item.kind == "records":
        return citation + (
            f"符合条件的记录共 {result['total_records']} 条，当前页返回 "
            f"{result['returned_records']} 条；has_more={result['has_more']}。"
            "分页结果不能直接代表完整区间的汇总表现。"
        )
    if item.kind == "performance":
        overall = result["overall"]
        metrics = "; ".join(
            f"{name.upper()}={metric['value']} ({metric['status']}, {metric['unit']})"
            for name, metric in overall["metrics"].items()
        )
        return citation + (
            f"本次汇总使用 {overall['row_count']} 条记录，"
            f"覆盖 {overall['campaign_count']} 个计划。{metrics}。"
            "比率由完整区间的分子、分母汇总后计算；null 表示不可计算。"
            f"数据状态={overall['status']}，覆盖情况="
            + json.dumps(overall["coverage"], ensure_ascii=False)
        )
    if item.kind == "ranking":
        ordered = (
            ", ".join(
                f"{row['campaign_id']}={row['metrics'][result['metric']]['value']}"
                for row in result["rankings"]
            )
            or "没有可计算的排名"
        )
        direction = "从低到高" if result["order"] == "asc" else "从高到低"
        return citation + (
            f"按 {result['metric'].upper()} {direction}排列：{ordered}。"
            f"本次读取 {result['sample_rows']} 条记录；"
            f"排除项={json.dumps(result['excluded_campaigns'], ensure_ascii=False)}。"
            "单项指标排名不能独立证明计划的整体价值或因果关系。"
        )
    alerts, insufficient = [], []
    for campaign in result["campaigns"]:
        for rule in campaign["rules"]:
            label = f"{campaign['campaign_id']}:{rule['rule']}"
            if rule["status"] == "triggered":
                alerts.append(label)
            elif rule["status"] == "insufficient_evidence":
                insufficient.append(label)
    return citation + (
        "触发启发式规则："
        + (", ".join(alerts) or "无")
        + "；证据不足的规则："
        + (", ".join(insufficient) or "无")
        + "。未触发与证据不足必须区分，规则告警不构成统计显著性或因果诊断。"
    )
