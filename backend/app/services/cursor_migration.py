"""游标迁移领域服务：把旧格式增量采集游标迁移成带来源与用途的增量断点。

背景：旧版增量追踪把两类历史位置都写进 incr_tracker.当前游标 ——
  1. 纯数字（如 "2500"）：增量采集写下的位置，可能是已完成的采集边界；
  2. call_N（如 "call_17"）：调用日志执行“标记重试”时写下的位置，
     本质是待重放请求的引用，不是已采位置。

迁移规则（核心不变量）：
- 只有能在 call_log 找到成功记录、且采集位置可核对的纯数字值，
  才能确认为 confirmed 断点并推进下一次采集边界；
- call_N 必须保留请求引用：能在重放队列找到待重放任务 → pending_replay，
  找不到原记录 → pending_review，含义不确定的旧值继续可查，绝不冒充已采；
- 迁移可中断、可恢复、可回滚；重复登记幂等；
  回滚/跳过只动断点，不删除、不伪造真实已采范围，也不能把待重放请求跳过。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.incr_breakpoint import IncrBreakpointService
from app.store import store

CALL_REF_PREFIX = "call_"
ACTION_DATE = "2026-11-22"


class CursorMigrationService(BaseModuleService):
    MODULE = "cursor_migration"
    ENTITY = "迁移批次"
    OPERATION_TERM = "迁移"
    LIST_FIELDS = [
        "批次编号", "目标端点", "端点上线状态", "待转换条目数", "已转换条目数",
        "已确认条目数", "待重放条目数", "待核条目数", "中断位置", "最后处理游标",
        "登记时间", "备注",
    ]
    REQUIRED_FIELDS = ["批次编号", "目标端点"]
    STATUS_ORDER = ["待转换", "迁移中", "已中断", "已完成", "已回滚"]
    ACTION_RULES = {
        "登记批次": "待转换",
        "转换下一条": "迁移中",
        "中断迁移": "已中断",
        "继续回填": "迁移中",
        "完成迁移": "已完成",
        "端点上线": "已完成",
        "回滚批次": "已回滚",
    }
    NEGATIVE_ACTIONS = ["回滚批次"]

    def __init__(self) -> None:
        self._breakpoints = IncrBreakpointService()

    # ------------------------------------------------------------------
    # 动作入口
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str, remark: str | None = None):
        batch = store.find(self.MODULE, entry_id)
        if batch is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action == "登记批次":
            return self._register_batch(batch, remark)
        if action == "转换下一条":
            return self._convert_next(batch)
        if action == "中断迁移":
            return self._interrupt(batch)
        if action == "继续回填":
            return self._resume(batch)
        if action == "完成迁移":
            return self._finish(batch)
        if action == "端点上线":
            return self._go_live(batch)
        if action == "回滚批次":
            return self._rollback(batch)
        return None, f"动作「{action}」暂未实现"

    # ------------------------------------------------------------------
    # 登记：扫描端点下的旧游标，重复登记幂等
    # ------------------------------------------------------------------
    def _register_batch(self, batch: dict, remark: str | None) -> tuple[dict, str]:
        endpoint = batch["目标端点"]
        # 幂等：已经登记过同端点且未回滚的批次时直接返回，不重复造断点
        existing = [
            b for b in store.rows(self.MODULE)
            if b.get("目标端点") == endpoint
            and int(b.get("id", 0)) != int(batch["id"])
            and b.get("status") != "已回滚"
        ]
        if existing:
            batch["status"] = existing[0].get("status", "待转换")
            return batch, f"端点{endpoint}已存在迁移批次{existing[0]['批次编号']}，重复登记不产生新断点"

        trackers = self._legacy_trackers(endpoint)
        batch["待转换条目数"] = len(trackers)
        batch["已转换条目数"] = 0
        batch["已确认条目数"] = 0
        batch["待重放条目数"] = 0
        batch["待核条目数"] = 0
        batch["中断位置"] = ""
        batch["最后处理游标"] = ""
        batch["端点上线状态"] = "旧版运行"
        batch["status"] = "待转换"
        batch["pending"] = True
        batch["abnormal"] = False
        if remark:
            batch["备注"] = remark
        return batch, f"已登记{len(trackers)}条旧游标待转换（旧值保留在增量追踪清单可查）"

    # ------------------------------------------------------------------
    # 单条转换：把一个旧游标分类落成一个增量断点
    # ------------------------------------------------------------------
    def _convert_next(self, batch: dict) -> tuple[dict, str]:
        if batch["status"] == "已回滚":
            return None, "批次已回滚，不能继续转换"
        endpoint = batch["目标端点"]
        trackers = self._legacy_trackers(endpoint)
        # 恢复时跳过已经生成过断点的标记，保证中断恢复/重复登记幂等
        done_tracker_ids = {
            int(bp.get("关联标记ID", 0))
            for bp in store.rows("incr_breakpoint")
            if bp.get("迁移批次ID") == int(batch["id"])
            and bp.get("验证状态") != "reverted"
        }
        pending = [t for t in trackers if int(t["id"]) not in done_tracker_ids]
        if not pending:
            return self._finish(batch)

        tracker = pending[0]
        bp = self._classify_and_create(batch, tracker)
        batch["最后处理游标"] = str(tracker.get("当前游标", ""))
        self._refresh_counts(batch)
        batch["status"] = "迁移中"
        batch["pending"] = True
        return batch, f"已转换标记{tracker['标记编号']}旧值「{bp['原始游标']}」→ 断点{bp['断点编号']}（{bp['验证状态']}）"

    def _classify_and_create(self, batch: dict, tracker: dict) -> dict[str, Any]:
        endpoint = batch["目标端点"]
        raw_cursor = str(tracker.get("当前游标", "")).strip()
        bp_rows = store.rows("incr_breakpoint")
        bp_id = max((int(r.get("id", 0)) for r in bp_rows), default=0) + 1
        code = f"BP-{str(bp_id).zfill(4)}"

        base = {
            "id": bp_id,
            "断点编号": code,
            "迁移批次": batch["批次编号"],
            "迁移批次ID": int(batch["id"]),
            "目标端点": endpoint,
            "关联标记": tracker.get("标记编号", ""),
            "关联标记ID": int(tracker["id"]),
            "原始游标": raw_cursor,
            "登记时间": f"{ACTION_DATE} 10:00:00",
            "采集位置": "",
            "请求引用": "",
            "重放任务": "",
            "重放任务ID": None,
            "认领结果": "",
            "下一次采集起点": "",
            "abnormal": False,
        }

        if raw_cursor.isdigit():
            return self._classify_numeric(base, raw_cursor)
        if raw_cursor.startswith(CALL_REF_PREFIX):
            return self._classify_call_ref(base, raw_cursor)
        # 既不是纯数字也不是 call_ 引用：含义不确定，待核且可查
        base.update({
            "来源": "unknown_legacy",
            "用途": "review",
            "验证状态": "pending_review",
            "status": "待核",
            "pending": True,
        })
        bp_rows.append(base)
        return base

    def _classify_numeric(self, bp: dict, raw_cursor: str) -> dict:
        """纯数字：只有成功调用日志且采集位置可核对，才确认真实已采范围。"""
        endpoint = bp["目标端点"]
        verified_log = self._find_verified_log(endpoint, int(raw_cursor))
        bp_rows = store.rows("incr_breakpoint")
        if verified_log is not None:
            bp.update({
                "来源": "legacy_numeric",
                "用途": "collection",
                "验证状态": "confirmed",
                "status": "已确认",
                "pending": False,
                "采集位置": raw_cursor,
                "下一次采集起点": raw_cursor,
                "核对日志": verified_log.get("记录编号", ""),
            })
        else:
            # 数字值但找不到可核对的成功采集：不能当成已采边界，转待核
            bp.update({
                "来源": "legacy_numeric",
                "用途": "review",
                "验证状态": "pending_review",
                "status": "待核",
                "pending": True,
            })
        bp_rows.append(bp)
        return bp

    def _classify_call_ref(self, bp: dict, raw_cursor: str) -> dict:
        """call_N：保留请求引用；能挂到待重放任务进重放待办，找不到原记录则待核。"""
        endpoint = bp["目标端点"]
        ref_id = raw_cursor[len(CALL_REF_PREFIX):]
        bp["请求引用"] = raw_cursor
        bp_rows = store.rows("incr_breakpoint")

        retry_task = self._find_retry_task(endpoint, raw_cursor)
        if retry_task is not None and retry_task.get("status") in ("待重试", "重试中"):
            bp.update({
                "来源": "legacy_callref",
                "用途": "replay",
                "验证状态": "pending_replay",
                "status": "待重放",
                "pending": True,
                "重放任务": retry_task.get("任务编号", ""),
                "重放任务ID": int(retry_task["id"]),
                "abnormal": True,
            })
            # 双向挂钩：队列认领结果后续回写到该断点
            retry_task["关联断点编号"] = bp["断点编号"]
            retry_task["凭据类型"] = "replay"
        elif retry_task is not None:
            # 引用能找到任务但已结束/永久失败：请求引用保留，转人工待核，不冒充已采
            bp.update({
                "来源": "legacy_callref",
                "用途": "review",
                "验证状态": "pending_review",
                "status": "待核",
                "pending": True,
                "重放任务": retry_task.get("任务编号", ""),
                "重放任务ID": int(retry_task["id"]),
                "abnormal": True,
            })
        else:
            # 找不到调用日志/重放任务原记录：含义不确定，继续可查但不冒充已采
            bp.update({
                "来源": "orphan_callref",
                "用途": "review",
                "验证状态": "pending_review",
                "status": "待核",
                "pending": True,
                "abnormal": True,
            })
        bp_rows.append(bp)
        return bp

    # ------------------------------------------------------------------
    # 中断 / 恢复 / 完成 / 上线 / 回滚
    # ------------------------------------------------------------------
    def _interrupt(self, batch: dict) -> tuple[dict, str]:
        if batch["status"] not in ("迁移中", "待转换"):
            return None, f"批次当前为{batch['status']}，不允许中断"
        converted = self._batch_breakpoints(batch)
        batch["中断位置"] = batch.get("最后处理游标", "") or (
            converted[-1]["原始游标"] if converted else "")
        batch["status"] = "已中断"
        batch["pending"] = True
        return batch, (
            f"迁移已中断：已转换{len(converted)}条，未转换值保留在增量追踪清单，"
            "已确认进度不受影响，恢复时从未转换标记继续"
        )

    def _resume(self, batch: dict) -> tuple[dict, str]:
        if batch["status"] != "已中断":
            return None, f"批次当前为{batch['status']}，只有已中断批次才能继续回填"
        batch["status"] = "迁移中"
        entry, message = self._convert_next(batch)
        if entry is None:
            return None, message
        return entry, f"已恢复迁移并继续回填；{message}"

    def _finish(self, batch: dict) -> tuple[dict, str]:
        self._refresh_counts(batch)
        endpoint = batch["目标端点"]
        total = batch["待转换条目数"]
        converted = batch["已转换条目数"]
        if converted < total:
            return None, f"还有{total - converted}条旧游标未转换，不能完成迁移"
        batch["status"] = "已完成"
        batch["pending"] = False
        batch["中断位置"] = ""
        # 迁移完成后端点仍保持旧版/新版并行，等待显式“端点上线”切换凭据
        batch["端点上线状态"] = "并行运行"
        return batch, (
            f"端点{endpoint}迁移完成：已确认{batch['已确认条目数']}条、"
            f"待重放{batch['待重放条目数']}条、待核{batch['待核条目数']}条；"
            "待重放/待核记录均未推进采集边界"
        )

    def _go_live(self, batch: dict) -> tuple[dict, str]:
        if batch["status"] != "已完成":
            return None, "只有已完成迁移的端点才能切换到新版采集凭据"
        endpoint = batch["目标端点"]
        boundary = self._breakpoints.confirmed_boundary(endpoint)
        if boundary is None:
            return None, "没有任何已确认真实采集位置，不能上线新版采集"
        # 新旧并行部署：增量取数用 collection 凭据，重放用 replay 凭据，互不串用
        for tracker in self._legacy_trackers(endpoint):
            tracker["上线状态"] = "新版上线"
            tracker["采集凭据"] = f"collection:cursor={boundary}"
            replay_refs = [
                bp["请求引用"] for bp in self._batch_breakpoints(batch)
                if bp.get("验证状态") == "pending_replay" and bp.get("请求引用")
            ]
            tracker["重放凭据"] = "replay:" + ",".join(replay_refs) if replay_refs else "replay:none"
        batch["端点上线状态"] = "新版上线"
        return batch, (
            f"端点{endpoint}已上线：增量取数凭据 collection:cursor={boundary}，"
            "重放继续使用 replay 凭据引用待重放请求"
        )

    def _rollback(self, batch: dict) -> tuple[dict, str]:
        endpoint = batch["目标端点"]
        breakpoints = self._batch_breakpoints(batch)
        reverted = 0
        for bp in breakpoints:
            if bp.get("验证状态") in ("reverted", "skipped"):
                continue
            # 回滚只撤销断点登记：真实已采范围（confirmed 对应的成功日志）仍在，
            # 待重放请求引用解除挂钩但任务保留，绝不把待重放请求跳过
            bp["验证状态"] = "reverted"
            bp["status"] = "已回滚"
            bp["pending"] = False
            reverted += 1
            task_id = bp.get("重放任务ID")
            if task_id:
                task = store.find("retry_queue", int(task_id))
                # 只解除断点挂钩，任务本身无论处于什么状态都保留在队列中，不能跳过
                if task is not None:
                    task["关联断点编号"] = ""
        for tracker in self._legacy_trackers(endpoint):
            tracker["上线状态"] = "旧版运行"
            tracker["采集凭据"] = ""
            tracker["重放凭据"] = ""
        batch["status"] = "已回滚"
        batch["端点上线状态"] = "旧版运行"
        batch["pending"] = False
        batch["abnormal"] = True
        self._refresh_counts(batch)
        return batch, (
            f"已回滚{reverted}条断点：旧游标保留可查，待重放任务仍在队列中，"
            "真实已采范围未被删除或改写"
        )

    # ------------------------------------------------------------------
    # 辅助查询
    # ------------------------------------------------------------------
    def _legacy_trackers(self, endpoint: str) -> list[dict]:
        return [
            t for t in store.rows("incr_tracker")
            if t.get("目标端点") == endpoint
        ]

    def _batch_breakpoints(self, batch: dict) -> list[dict]:
        return [
            bp for bp in store.rows("incr_breakpoint")
            if bp.get("迁移批次ID") == int(batch["id"])
        ]

    def _refresh_counts(self, batch: dict) -> None:
        bps = self._batch_breakpoints(batch)
        active = [bp for bp in bps if bp.get("验证状态") != "reverted"]
        batch["待转换条目数"] = len(self._legacy_trackers(batch["目标端点"]))
        batch["已转换条目数"] = len(active)
        batch["已确认条目数"] = sum(1 for bp in active if bp.get("验证状态") == "confirmed")
        batch["待重放条目数"] = sum(1 for bp in active if bp.get("验证状态") == "pending_replay")
        batch["待核条目数"] = sum(1 for bp in active if bp.get("验证状态") == "pending_review")

    @staticmethod
    def _find_verified_log(endpoint: str, position: int) -> dict | None:
        """成功调用日志且采集位置与纯数字游标一致，才算可验证的真实采集位置。"""
        for log in store.rows("call_log"):
            if (
                log.get("接口端点") == endpoint
                and log.get("status") == "成功"
                and str(log.get("采集位置", "")).isdigit()
                and int(log["采集位置"]) == position
            ):
                return log
        return None

    @staticmethod
    def _find_retry_task(endpoint: str, call_ref: str) -> dict | None:
        for task in store.rows("retry_queue"):
            if (
                task.get("失败端点") == endpoint
                and task.get("请求引用") == call_ref
            ):
                return task
        return None

    # ------------------------------------------------------------------
    # 跨模块视图：迁移结果、待办与下一次采集范围
    # ------------------------------------------------------------------
    def migration_report(self, batch_id: int) -> dict:
        batch = store.find(self.MODULE, batch_id)
        if batch is None:
            return {"ok": False, "message": f"迁移批次 {batch_id} 不存在"}
        endpoint = batch["目标端点"]
        bps = self._batch_breakpoints(batch)
        return {
            "ok": True,
            "batch": {k: batch.get(k) for k in self.LIST_FIELDS + ["status"]},
            "breakpoints": bps,
            "replay_todo": self._breakpoints.replay_todo(endpoint),
            "review_todo": self._breakpoints.review_todo(endpoint),
            "next_collection_range": self._breakpoints.next_collection_range(endpoint),
            "tracker_manifest": [
                {
                    "标记编号": t.get("标记编号"),
                    "原始游标": t.get("当前游标"),
                    "上线状态": t.get("上线状态", "旧版运行"),
                    "采集凭据": t.get("采集凭据", ""),
                    "重放凭据": t.get("重放凭据", ""),
                }
                for t in self._legacy_trackers(endpoint)
            ],
        }
