"""Evidence-only Markdown reports with safe filenames and immutable source snapshots."""

import hashlib
import html
import json
import os
from pathlib import Path
from uuid import uuid4

from app.agent.contracts import AgentResponse
from app.phase3.contracts import ReportRequest
from app.phase3.service import BusinessService, ToolRun
from app.phase3.store import BusinessError, digest, now, owned_action
from app.tools.analytics_tools import compare_campaigns, summarize_campaign_performance
from app.tools.anomaly_tools import detect_campaign_anomalies


def text(value) -> str:
    value = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    return "".join(f"&#{ord(c)};" if c in "|`[]*_#\\" else c for c in value)


def source(call_id: str, path: str) -> str:
    return f"[{text(call_id)}#/{path}]"


def render_report(document: dict) -> str:
    request, results, traces = document["request"], document["tool_results"], document["tool_calls"]
    lookup = {trace["tool_name"]: trace["call_id"] for trace in traces}
    summary_id = lookup["summarize_campaign_performance"]
    summary = results[summary_id]
    ids = set(request["campaign_ids"]) if request["campaign_ids"] else None
    selected = [row for row in summary["campaigns"] if ids is None or row["campaign_id"] in ids]
    lines = [
        "# 广告投放复盘报告（仅演示 / simulation only）",
        "",
        f"生成时间：{document['created_at']}；报告 ID：{document['id']}",
        f"观察日期：{request['start_date']} 至 {request['end_date']}（闭区间）；币种：USD。",
        "来源：本地 synthetic/demo CSV；不代表真实商业投放效果。",
        f"数据文件 SHA-256：{document['source']['dataset_sha256']}",
        "",
        "## 整体表现与关键指标",
    ]
    if not selected:
        lines.append("无可用数据；不把缺失数据或 null 当作 0。")
    if ids is not None:
        lines.append("本报告为所选计划的逐计划指标，不将全数据集整体指标冒充筛选后合计。")
    display = [("整体", summary["overall"], "overall")] if ids is None else []
    display += [
        (r["campaign_id"], r, f"campaigns/{summary['campaigns'].index(r)}") for r in selected
    ]
    lines.extend(["", "| 范围 | 指标 | 值 | 状态 / 单位 | 工具引用 |", "|---|---|---|---|---|"])
    for label, row, path in display:
        for field, value in row["totals"].items():
            lines.append(
                f"| {text(label)} | {field} | {value} | {text(row['status'])} | "
                f"{source(summary_id, path + '/totals/' + field)} |"
            )
        for metric, value in row["metrics"].items():
            lines.append(
                f"| {text(label)} | {metric.upper()} | "
                f"{value['value'] if value['value'] is not None else 'null'} | "
                f"{text(value['status'])} / {text(value['unit'])} | "
                f"{source(summary_id, path + '/metrics/' + metric)} |"
            )
    lines.extend(["", "## 计划排名（历史 ROAS，非未来收益预测）"])
    for tool_name in ("compare_campaigns",):
        rank_id = lookup[tool_name]
        ranking = results[rank_id]
        ranked = [
            (i, row)
            for i, row in enumerate(ranking["rankings"])
            if ids is None or row["campaign_id"] in ids
        ]
        lines.append("按工具返回的 ROAS 从高到低展示；首尾分别为本次可计算结果的最好和最差。")
        for i, row in ranked:
            lines.append(
                f"- {text(row['campaign_id'])}：ROAS={row['metrics']['roas']['value']} "
                f"{source(rank_id, f'rankings/{i}/metrics/roas')}"
            )
        if not ranked:
            lines.append("无可计算排名。")
        lines.append(
            f"全数据集排名排除项：{text(ranking['excluded_campaigns'])} "
            f"{source(rank_id, 'excluded_campaigns')}"
        )
    anomaly_id = lookup["detect_campaign_anomalies"]
    anomaly = results[anomaly_id]
    lines.extend(
        [
            "",
            "## 异常发现与复核建议",
            f"基准期：{text(anomaly['baseline_period'])} {source(anomaly_id, 'baseline_period')}",
        ]
    )
    for i, row in enumerate(anomaly["campaigns"]):
        if ids is not None and row["campaign_id"] not in ids:
            continue
        for j, rule in enumerate(row["rules"]):
            lines.append(
                f"- {text(row['campaign_id'])} / {text(rule['rule'])}：{text(rule['status'])}；"
                f"current={rule['current_value']}，baseline={rule['baseline_value']}，"
                f"原因={text(rule['evidence_reasons'])} "
                f"{source(anomaly_id, f'campaigns/{i}/rules/{j}')}"
            )
    lines.append("触发规则仅建议人工复核；证据不足时先补充样本与完整日期，不作因果判断或自动调整。")
    lines.extend(["", "## 预算方案及审批状态"])
    proposal = document.get("proposal")
    if proposal:
        lines.extend(
            [
                f"方案 {proposal['id']}，版本 {proposal['version']}，"
                f"状态 {proposal['status']}（生成报告时快照）。",
                f"内容摘要：{proposal['proposal_sha256']}；仅模拟，不代表已真实执行。",
                "下列预算来自已保存方案；用户原预算是输入，不是历史 spend：",
            ]
        )
        for row in proposal["payload"]["allocation"]["allocations"]:
            lines.append(
                f"- {text(row['campaign_id'])}：{row['current_budget']} → "
                f"{row['proposed_budget']} USD；冻结原因={text(row['frozen_reasons'])} "
                f"[proposal:{proposal['id']}/payload/allocation/allocations]"
            )
    else:
        lines.append("未关联预算方案；未审批、未模拟、未执行任何真实投放。")
    lines.extend(
        [
            "",
            "## 数据质量与限制",
            "null 表示不可计算；未触发与证据不足不同。规则不构成统计显著性或因果结论。",
        ]
    )
    for label, row, path in display:
        lines.append(
            f"- {text(label)}：{text(row['data_quality_warnings'])}；"
            f"覆盖={text(row['coverage'])} {source(summary_id, path)}"
        )
    lines.extend(
        [
            "金额与指标来自真实运行工具；建议为确定性启发式，不是模型自由文本推断，不承诺效果改善。",
            "审批身份是本地自报；此服务仅供可信 loopback 使用，不具备公网身份认证。",
            "",
            "## 完整可复核证据",
            "包含精确工具输出、调用参数、耗时及预算快照；引用可按 call_id 和 JSON 路径核对。",
            "",
            "```json",
            json.dumps(
                {key: document[key] for key in ("tool_calls", "tool_results", "proposal", "agent")},
                ensure_ascii=False,
                indent=2,
            )
            .replace("`", "\\u0060")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e"),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


class ReportService:
    def __init__(self, business: BusinessService, reports_dir: Path):
        self.business, self.store = business, business.store
        self.reports_dir = Path(reports_dir).absolute()

    def _path(self, identity: str, *, create_directory=True) -> Path:
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise BusinessError("invalid_report_id", 422)
        if create_directory:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
        if self.reports_dir.resolve() != self.reports_dir or self.reports_dir.is_symlink():
            raise BusinessError("unsafe_reports_directory")
        path = self.reports_dir / f"{identity}.md"
        if path.is_symlink() or path.resolve().parent != self.reports_dir:
            raise BusinessError("unsafe_report_path")
        return path

    @owned_action
    def generate_campaign_report(
        self, request: ReportRequest, *, agent: AgentResponse | None = None
    ) -> dict:
        request = ReportRequest.model_validate(request)
        self.store.check_unmanaged(request.proposal_id)
        run = ToolRun()
        try:
            before = hashlib.sha256(self.business.data_path.read_bytes()).hexdigest()
            dates = {
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
            }
            _, summary = run.call(
                summarize_campaign_performance, dates, data_path=self.business.data_path
            )
            if len(summary["campaigns"]) > 100:
                raise BusinessError("report_campaign_limit", 422)
            if request.campaign_ids and not set(request.campaign_ids).issubset(
                {r["campaign_id"] for r in summary["campaigns"]}
            ):
                raise BusinessError("report_campaign_missing", 422)
            run.call(
                compare_campaigns,
                dates | {"metric": "roas", "top_n": 100, "order": "desc"},
                data_path=self.business.data_path,
            )
            run.call(
                detect_campaign_anomalies,
                dates | {"baseline_days": request.baseline_days},
                data_path=self.business.data_path,
                thresholds=self.business.thresholds,
            )
            if before != hashlib.sha256(self.business.data_path.read_bytes()).hexdigest():
                raise BusinessError("dataset_changed_during_analysis")
            proposal = (
                self.business.get_budget_proposal(request.proposal_id)
                if request.proposal_id
                else None
            )
            if proposal:
                original = proposal["payload"]["request"]
                same_dates = all(original[key] == dates[key] for key in dates)
                scope = set(
                    request.campaign_ids or [r["campaign_id"] for r in summary["campaigns"]]
                )
                proposal_scope = {r["campaign_id"] for r in original["campaigns"]}
                if (
                    not same_dates
                    or not proposal_scope.issubset(scope)
                    or proposal["payload"]["source"]["dataset_sha256"] != before
                ):
                    raise BusinessError("proposal_report_evidence_mismatch", 422)
            if agent is not None:
                self._validate_agent(agent, dates, summary)
        except (BusinessError, OSError) as exc:
            code = exc.code if isinstance(exc, BusinessError) else "data_source_error"
            self.store.failure("report_failed", code, run.traces)
            raise BusinessError(code, 422) from None
        identity = uuid4().hex
        document = {
            "id": identity,
            "created_at": now(),
            "status": "generated",
            "mode": "simulation",
            "request": request.model_dump(mode="json"),
            "tool_calls": run.traces,
            "tool_results": run.results,
            "source": {"kind": "synthetic_demo_csv", "dataset_sha256": before},
            "thresholds": self.business.thresholds.model_dump(),
            "proposal": proposal,
            "agent": agent.model_dump(mode="json") if agent is not None else None,
            "generator": "agent_report_workflow"
            if agent is not None
            else "deterministic_report_tools",
        }
        content = render_report(document)
        if len(content.encode("utf-8")) > 4 * 1024 * 1024:
            raise BusinessError("report_too_large", 413)
        document["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document["evidence_sha256"] = digest(run.results)
        document["filename"] = f"{identity}.md"
        if len(json.dumps(document, ensure_ascii=False).encode("utf-8")) > 2 * 1024 * 1024:
            raise BusinessError("evidence_too_large", 413)
        path = self._path(identity)
        temporary = path.with_suffix(".tmp")
        created = False
        temporary_created = False
        try:
            with self.store.transaction() as connection:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    temporary_created = True
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Windows rename fails if the destination already exists.
                temporary.rename(path)
                created = True
                self.store.save(connection, identity, "report", document, new=True)
                self.store.event(
                    connection,
                    identity,
                    "report_generated",
                    {"content_sha256": document["content_sha256"]},
                )
        except Exception:
            # Legacy calls commit here; task calls enlist in the outer UoW.
            # Resolve a lost commit acknowledgement before deleting a possibly
            # registered file. If storage cannot answer, retain the file and
            # propagate rather than guessing that the commit rolled back.
            if getattr(self.store._thread, "enlisted", None) is None:
                try:
                    persisted = self.store.get(identity, "report")
                except BusinessError as lookup:
                    if lookup.code != "not_found":
                        raise
                else:
                    if persisted == document:
                        return document
                    raise BusinessError("stored_evidence_integrity_error")
            cleanup = []
            for published, target in ((temporary_created, temporary), (created, path)):
                if published:
                    try:
                        target.unlink(missing_ok=True)
                        outcome = "removed"
                    except OSError:
                        outcome = "retained"
                    cleanup.append({"file": target.name, "outcome": outcome})
            self.store.failure("report_publish_failed", "report_write_failed", run.traces)
            error = BusinessError("report_write_failed", 500)
            error.report_cleanup = {"id": identity, "files": cleanup}
            raise error from None
        return document

    @staticmethod
    def _validate_agent(agent: AgentResponse, dates: dict, summary: dict):
        if agent.status != "completed" or agent.plan is None or agent.plan.date_range is None:
            raise BusinessError("agent_report_requires_completed_evidence")
        if agent.plan.date_range.model_dump(mode="json") != dates:
            raise BusinessError("agent_report_date_mismatch")
        matches = [
            t
            for t in agent.tool_calls
            if t.tool_name == "summarize_campaign_performance"
            and t.status == "success"
            and t.arguments == dates
        ]
        if not any(agent.tool_results.get(t.call_id) == summary for t in matches):
            raise BusinessError("agent_report_evidence_mismatch")
        if not any(
            e.call_id in {t.call_id for t in matches}
            and e.tool_name == "summarize_campaign_performance"
            for e in agent.evidence
        ):
            raise BusinessError("agent_report_missing_citation")

    def get_report(self, identity: str) -> dict:
        with self.store.read_snapshot() as connection:
            document = self.store.load(connection, identity, "report")
            path = self._path(identity, create_directory=False)
            try:
                raw = path.read_bytes()
            except OSError:
                raise BusinessError("report_file_missing", 404) from None
            if hashlib.sha256(raw).hexdigest() != document["content_sha256"]:
                raise BusinessError("report_file_integrity_error")
            return document | {"content": raw.decode("utf-8")}

    def list_reports(self, limit: int = 50, offset: int = 0) -> list[dict]:
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise BusinessError("invalid_pagination", 422)
        with self.store.read_snapshot() as connection:
            rows = connection.execute(
                "SELECT id FROM documents WHERE kind='report' ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            documents = [self.store.load(connection, row["id"], "report") for row in rows]
            return [
                {key: doc[key] for key in ("id", "created_at", "status", "filename", "generator")}
                for doc in documents
            ]
