"""重放队列领域服务：管理失败请求重试，与 request_batch、call_log、api_monitor 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class RetryQueueService(BaseModuleService):
    MODULE = "retry_queue"
    ENTITY = "重放任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "失败端点", "失败原因", "重试次数", "最大重试", "退避策略", "最后重试", "队列状态"]
    REQUIRED_FIELDS = ["任务编号", "失败端点", "失败原因"]
    STATUS_ORDER = ["待重试", "重试中", "已成功", "永久失败"]
    ACTION_RULES = {"加入队列": "重试中", "手动重试": "已成功", "标记失败": "永久失败"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str):
        """重试中/已成功时联动写调用日志和更新监控；永久失败时触发告警。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "加入队列":
            self._log_retry_start(entry)
        elif action == "手动重试":
            self._log_retry_success(entry)
            self._update_monitor_health(entry)
            message += "，已更新调用日志和监控健康上报"
        elif action == "标记失败":
            self._trigger_alert(entry)
            message += "，已触发监控告警"
        return entry, message

    def _log_retry_start(self, task: dict):
        log_rows = store.rows("call_log")
        tid = str(task["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-RETRY-{tid}",
            "接口端点": task.get("失败端点", ""),
            "请求参数": f"retry:task={tid}",
            "响应状态": "retrying",
            "响应耗时(ms)": 0,
            "返回行数": 0,
            "错误信息": task.get("失败原因", ""),
            "调用时间": "2026-11-17",
            "status": "超时", "pending": True, "abnormal": True,
        })

    def _log_retry_success(self, task: dict):
        tid = str(task["id"]).zfill(4)
        for log in store.rows("call_log"):
            if log.get("记录编号") == f"CALL-RETRY-{tid}":
                log["status"] = "成功"
                log["响应状态"] = "200"
                log["响应耗时(ms)"] = 150
                log["返回行数"] = 1
                break

    def _update_monitor_health(self, task: dict):
        """重试成功后联动刷新监控健康度。"""
        endpoint = task.get("失败端点", "")
        for mon in store.rows("api_monitor"):
            if mon.get("目标端点") == endpoint:
                mon["最近采样"] = "2026-11-17"
                if mon.get("status") == "已告警":
                    mon["status"] = "监控中"
                    mon["pending"] = True

    def _trigger_alert(self, task: dict):
        """永久失败触发监控告警和治理报告。"""
        endpoint = task.get("失败端点", "")
        tid = str(task["id"]).zfill(4)
        # 更新监控状态
        for mon in store.rows("api_monitor"):
            if mon.get("目标端点") == endpoint:
                mon["status"] = "已告警"
                mon["pending"] = False
                mon["最近采样"] = "2026-11-17"
        # 创建治理报告条目
        gov_rows = store.rows("govern_report")
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-RETRY-{tid}",
            "评估范围": f"重放队列永久失败: {endpoint}",
            "合规指标": "数据采集完整性",
            "当前得分": 60.0,
            "改进事项": f"任务{tid}重试耗尽，需人工介入",
            "评估周期": "2026-Q4",
            "负责人": "运维值班",
            "生成时间": "2026-11-17",
            "status": "待生成", "pending": True, "abnormal": True,
        })

    def replay_claim(self, task_id: int, outcome: str = "claim") -> dict:
        """认领重放队列任务，并把认领结果回写到增量追踪清单（增量断点）。

        - claim：工作节点领取下一段重放，任务 待重试 → 重试中，断点记录“已认领”；
        - succeed：重放成功，任务 → 已成功；若产出可验证采集位置，断点才允许确认；
        - fail：重放未成功，任务保持待重试，断点保持待重放，绝不推进游标。

        并发安全：只有处于“待重试”的任务能被领取，已被领取的任务再次领取会被拒绝。
        """
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"重放任务 {task_id} 不存在"}
        from app.services.incr_breakpoint import IncrBreakpointService

        bp_service = IncrBreakpointService()
        linked_bp = self._find_linked_breakpoint(task)

        if outcome == "claim":
            if task.get("status") != "待重试":
                return {
                    "ok": False,
                    "message": f"任务{task.get('任务编号')}状态为{task.get('status')}，已被领取或已结束，不能重复认领",
                    "task": task,
                }
            task["status"] = "重试中"
            task["pending"] = True
            task["队列状态"] = f"claimed:{task.get('请求引用', '')}"
            task["最后重试"] = "2026-11-22 11:00:00"
            if linked_bp is not None:
                bp_service.apply_replay_claim(
                    int(linked_bp["id"]), int(task["id"]), task["任务编号"], "已认领")
            return {
                "ok": True,
                "message": "认领成功，请求引用已保留；认领不推进采集边界",
                "task": task,
                "breakpoint": linked_bp,
                "credential_type": "replay",
            }

        if outcome == "succeed":
            task["status"] = "已成功"
            task["pending"] = False
            task["abnormal"] = False
            task["队列状态"] = "replayed_ok"
            if linked_bp is not None:
                linked_bp["认领结果"] = "重放成功"
                # 重放成功后仍需可验证采集位置才能确认，默认保持待重放，
                # 由采集侧凭成功日志执行“确认采集位置”，避免用请求引用冒充已采。
            return {"ok": True, "message": "重放成功，待可验证采集位置确认后方可推进游标",
                    "task": task, "breakpoint": linked_bp}

        # fail：保留待重放，不能跳过
        task["status"] = "待重试"
        task["pending"] = True
        task["队列状态"] = f"retry_failed:{task.get('请求引用', '')}"
        if linked_bp is not None:
            linked_bp["认领结果"] = "重放失败"
        return {"ok": True, "message": "重放未成功，任务保留待重放，未跳过任何请求",
                "task": task, "breakpoint": linked_bp}

    @staticmethod
    def _find_linked_breakpoint(task: dict) -> dict | None:
        code = task.get("关联断点编号") or ""
        for bp in store.rows("incr_breakpoint"):
            if code and bp.get("断点编号") == code:
                return bp
        # 兜底：按端点 + 请求引用匹配
        for bp in store.rows("incr_breakpoint"):
            if (bp.get("目标端点") == task.get("失败端点")
                    and bp.get("请求引用") == task.get("请求引用")):
                return bp
        return None

    def claim_next_for_endpoint(self, endpoint: str) -> dict:
        """用重放队列领取某端点的下一段数据：取最早一条待重试任务进行认领。"""
        candidates = [
            r for r in store.rows(self.MODULE)
            if r.get("失败端点") == endpoint and r.get("status") == "待重试"
        ]
        if not candidates:
            return {"ok": False, "message": f"端点{endpoint}没有待重放任务可领取",
                    "endpoint": endpoint}
        return self.replay_claim(int(candidates[0]["id"]), "claim")

    def calc_recovery_rate(self) -> dict:
        """跨模块恢复率统计：分析重放队列成功/失败分布及关联调用日志。"""
        retries = store.rows("retry_queue")
        total = len(retries)
        success = sum(1 for r in retries if r.get("status") == "已成功")
        perm_fail = sum(1 for r in retries if r.get("status") == "永久失败")
        # 关联调用日志
        logs_related = [
            l for l in store.rows("call_log")
            if "retry" in str(l.get("请求参数", ""))
        ]
        return {
            "total_retries": total,
            "success": success,
            "permanent_fail": perm_fail,
            "recovery_rate": success / total if total else 0.0,
            "logged_events": len(logs_related),
        }