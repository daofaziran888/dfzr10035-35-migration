"""增量断点领域服务：旧游标迁移后的新模型。

每个断点显式携带：
- 来源（legacy_numeric 增量追踪纯数字 / legacy_callref 调用日志 call_ 引用 / orphan_callref 找不到原记录）
- 用途（collection 增量取数 / replay 重放待办 / review 待核查）
- 验证状态（confirmed 可验证真实采集位置 / pending_replay 待重放 / pending_review 待核 /
  reverted 回滚 / skipped 重复登记跳过）

关键不变量：只有 confirmed 断点才能推进采集边界；pending_replay / pending_review
只能保留待重放/待核状态，绝不冒充已采范围。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


# 断点状态 → 是否允许推进采集边界
CAN_ADVANCE = {"confirmed"}


class IncrBreakpointService(BaseModuleService):
    MODULE = "incr_breakpoint"
    ENTITY = "增量断点"
    OPERATION_TERM = "迁移"
    LIST_FIELDS = [
        "断点编号", "迁移批次", "目标端点", "关联标记", "原始游标", "来源", "用途",
        "验证状态", "采集位置", "请求引用", "重放任务", "下一次采集起点", "登记时间",
    ]
    REQUIRED_FIELDS = ["断点编号", "目标端点", "原始游标", "来源", "用途"]
    STATUS_ORDER = ["待重放", "待核", "已确认", "已回滚", "已跳过"]
    ACTION_RULES = {
        "转待重放": "待重放",
        "转待核": "待核",
        "确认采集位置": "已确认",
        "回滚断点": "已回滚",
        "重复登记跳过": "已跳过",
    }
    NEGATIVE_ACTIONS = ["回滚断点"]

    @staticmethod
    def verification_to_status(verification: str) -> str:
        return {
            "confirmed": "已确认",
            "pending_replay": "待重放",
            "pending_review": "待核",
            "reverted": "已回滚",
            "skipped": "已跳过",
        }.get(verification, "待核")

    def list_for_tracker(self, tracker_id: int) -> list[dict]:
        return [
            bp for bp in store.rows(self.MODULE)
            if int(bp.get("关联标记ID", 0)) == tracker_id
        ]

    def confirmed_boundary(self, endpoint: str) -> int | None:
        """端点下所有已确认断点的最大真实采集位置，即已采范围上界。"""
        positions = [
            int(bp["采集位置"])
            for bp in store.rows(self.MODULE)
            if bp.get("目标端点") == endpoint
            and bp.get("验证状态") == "confirmed"
            and str(bp.get("采集位置", "")).isdigit()
        ]
        return max(positions) if positions else None

    def next_collection_range(self, endpoint: str, limit: int = 100) -> dict:
        """计算下一次增量取数范围：仅由 confirmed 断点推进，待重放/待核不参与。"""
        boundary = self.confirmed_boundary(endpoint)
        if boundary is None:
            return {
                "endpoint": endpoint,
                "credential_type": "collection",
                "start": None,
                "limit": limit,
                "reason": "没有可验证的已确认断点，不能从待重放/待核游标启动取数",
            }
        return {
            "endpoint": endpoint,
            "credential_type": "collection",
            "start": boundary,
            "next_cursor": boundary + limit,
            "limit": limit,
            "reason": "仅由已确认真实采集位置推进",
        }

    def replay_todo(self, endpoint: str | None = None) -> list[dict]:
        rows = store.rows(self.MODULE)
        if endpoint is not None:
            rows = [bp for bp in rows if bp.get("目标端点") == endpoint]
        return [bp for bp in rows if bp.get("验证状态") == "pending_replay"]

    def review_todo(self, endpoint: str | None = None) -> list[dict]:
        rows = store.rows(self.MODULE)
        if endpoint is not None:
            rows = [bp for bp in rows if bp.get("目标端点") == endpoint]
        return [bp for bp in rows if bp.get("验证状态") == "pending_review"]

    def apply_replay_claim(
        self, breakpoint_id: int, task_id: int, task_code: str, claim_status: str,
    ) -> dict | None:
        """重放队列认领结果回写到增量追踪清单（断点侧）。

        认领动作本身只回写认领结果与请求引用，不推进采集边界；
        只有重放成功并产出可验证的新采集位置后，才允许确认断点。
        """
        bp = store.find(self.MODULE, breakpoint_id)
        if bp is None:
            return None
        bp["重放任务"] = task_code
        bp["重放任务ID"] = task_id
        bp["认领结果"] = claim_status
        bp["用途"] = "replay"
        if claim_status == "已认领":
            bp["验证状态"] = "pending_replay"
            bp["status"] = "待重放"
            bp["pending"] = True
        return bp

    def run_action(self, entry_id: int, action: str):
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 同步验证状态字段，保证列表筛选与状态机一致
        entry["验证状态"] = {
            "已确认": "confirmed",
            "待重放": "pending_replay",
            "待核": "pending_review",
            "已回滚": "reverted",
            "已跳过": "skipped",
        }.get(entry.get("status", ""), entry.get("验证状态", "pending_review"))
        return entry, message
