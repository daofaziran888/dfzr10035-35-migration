"""调用日志领域服务：管理API调用记录，与 retry_queue、api_monitor、rate_limit、govern_report 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class CallLogService(BaseModuleService):
    MODULE = "call_log"
    ENTITY = "调用记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "接口端点", "请求参数", "响应状态", "响应耗时", "返回行数", "错误信息", "调用时间"]
    REQUIRED_FIELDS = ["记录编号", "接口端点", "请求参数"]
    STATUS_ORDER = ["成功", "超时", "限流拒绝", "鉴权失败"]
    ACTION_RULES = {"标记审计": "超时", "标记重试": "限流拒绝"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str):
        """标记审计时联动流水审计；标记重试时创建重放任务并检索引用追踪状态。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "标记审计":
            self._audit_call(entry)
            message += "，已记录管道审计"
        elif action == "标记重试":
            self._enqueue_from_log(entry)
            self._check_incr_tracker(entry)
            message += "，已创建重放任务并检查增量追踪状态"
        return entry, message

    def _audit_call(self, log: dict):
        """调用异常时创建管道审计记录。"""
        audit_rows = store.rows("pipeline_audit")
        lid = str(log["id"]).zfill(4)
        audit_rows.append({
            "id": max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1,
            "记录编号": f"PIPE-CALL-{lid}",
            "管道名称": f"call_log_audit:{lid}",
            "操作人": "system",
            "操作类型": "审计",
            "变更摘要": f"调用记录{lid}标记审计:超时",
            "操作时间": "2026-11-17",
            "来源节点": "call_log",
            "审计结论": "待复核",
            "status": "待复核", "pending": True, "abnormal": True,
        })

    def _enqueue_from_log(self, log: dict):
        """从调用日志创建重放任务。"""
        retry_rows = store.rows("retry_queue")
        lid = str(log["id"]).zfill(4)
        retry_rows.append({
            "id": max((int(r.get("id", 0)) for r in retry_rows), default=0) + 1,
            "任务编号": f"RETR-CALL-{lid}",
            "失败端点": log.get("接口端点", ""),
            "失败原因": f"调用日志{lid}限流拒绝或超时",
            "重试次数": 0,
            "最大重试": 3,
            "退避策略": "linear:3s",
            "最后重试": "",
            "队列状态": "from_call_log",
            # 保留 call_N 请求引用：这是重放凭据，不是已采集位置
            "请求引用": f"call_{log['id']}",
            "凭据类型": "replay",
            "关联断点编号": "",
            "status": "待重试", "pending": True, "abnormal": True,
        })

    def _check_incr_tracker(self, log: dict):
        """重试时检查增量追踪状态，同步游标。"""
        endpoint = log.get("接口端点", "")
        for tracker in store.rows("incr_tracker"):
            if tracker.get("目标端点") == endpoint:
                tracker["当前游标"] = f"call_{log['id']}"
                tracker["最后同步"] = "2026-11-17"

    def calc_call_metrics(self, endpoint: str) -> dict:
        """跨模块调用指标：汇总日志+重试+监控+限流+治理。"""
        logs = [l for l in store.rows("call_log") if l.get("接口端点") == endpoint]
        retries = [r for r in store.rows("retry_queue") if r.get("失败端点") == endpoint]
        monitors = [m for m in store.rows("api_monitor") if m.get("目标端点") == endpoint]
        rate_limits = [rl for rl in store.rows("rate_limit") if rl.get("目标端点") == endpoint]
        return {
            "endpoint": endpoint,
            "total_calls": len(logs),
            "success_rate": sum(1 for l in logs if l.get("status") == "成功") / len(logs) if logs else 1.0,
            "avg_latency_ms": sum(int(l.get("响应耗时(ms)", 0)) for l in logs) / len(logs) if logs else 0,
            "pending_retries": len(retries),
            "monitors_active": sum(1 for m in monitors if m.get("status") == "监控中"),
            "rate_limits_active": sum(1 for rl in rate_limits if rl.get("status") == "生效中"),
        }