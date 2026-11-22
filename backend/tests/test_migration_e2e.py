"""旧游标 → 增量断点迁移的端到端剧本验证（服务层，内存仓库）。

剧本严格对应需求：
1. 纯数字 2500、call_17、找不到原记录的 call_88 三类历史值；
2. 迁移中途停止，停止期间重放队列并发认领 call_17；
3. 恢复回填，完成迁移；
4. 用重放队列领取该端点下一段数据，再回查增量追踪清单；
5. 校验：只有可确认历史位置推进采集边界；待核 / 仅 call_ 引用记录保持待重放/待核；
6. 回滚与重复登记只能保留真实已采范围，不能跳过待重放请求。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.cursor_migration import CursorMigrationService  # noqa: E402
from app.services.incr_breakpoint import IncrBreakpointService  # noqa: E402
from app.services.retry_queue import RetryQueueService  # noqa: E402
from app.store import store  # noqa: E402

ENDPOINT = "ENDP-LEGACY-9001"
failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def bp_map() -> dict[str, dict]:
    return {bp["原始游标"]: bp for bp in store.rows("incr_breakpoint")}


def main() -> int:
    mig = CursorMigrationService()
    bps = IncrBreakpointService()
    rq = RetryQueueService()
    batch = store.rows("cursor_migration")[0]
    bid = int(batch["id"])

    print("== 0. 初始存量：两类历史位置都在 incr_tracker.当前游标 ==")
    raw = {t["当前游标"] for t in store.rows("incr_tracker") if t["目标端点"] == ENDPOINT}
    check("存量游标含纯数字/call_17/call_88", raw == {"2500", "call_17", "call_88"}, raw)

    print("\n== 1. 登记迁移批次（逐端点上线，按端点扫描旧游标） ==")
    entry, msg = mig.run_action(bid, "登记批次")
    check("登记扫描出3条待转换", entry["待转换条目数"] == 3, msg)

    print("\n== 2. 转换第1条：纯数字 2500，可核对成功日志 CALL-0018 ==")
    entry, msg = mig.run_action(bid, "转换下一条")
    first = bp_map()["2500"]
    check("纯数字→已确认 confirmed", first["验证状态"] == "confirmed", first["断点编号"])
    check("已确认断点携带真实采集位置2500", first["采集位置"] == "2500")
    check("用途为增量取数 collection", first["用途"] == "collection")
    check("记录核对日志锚点", first.get("核对日志") == "CALL-0018")

    print("\n== 3. 转换第2条：call_17，挂到待重放任务 RETR-CALL-0017 ==")
    entry, msg = mig.run_action(bid, "转换下一条")
    c17 = bp_map()["call_17"]
    check("call_17→待重放 pending_replay", c17["验证状态"] == "pending_replay")
    check("保留请求引用 call_17", c17["请求引用"] == "call_17")
    check("挂钩重放任务且不写采集位置", c17["重放任务"] == "RETR-CALL-0017" and c17["采集位置"] == "")
    task17 = store.find("retry_queue", 17)
    check("重放任务回写关联断点编号", task17["关联断点编号"] == c17["断点编号"])

    print("\n== 4. 中途停止迁移（区分未转换值与已确认进度） ==")
    entry, msg = mig.run_action(bid, "中断迁移")
    check("批次状态=已中断", entry["status"] == "已中断")
    check("中断位置记录最后处理游标", entry["中断位置"] == "call_17", entry["中断位置"])
    check("已确认进度仍保留", bps.confirmed_boundary(ENDPOINT) == 2500)
    check("未转换 call_88 仍在增量追踪清单可查",
          any(t["当前游标"] == "call_88" for t in store.rows("incr_tracker") if t["目标端点"] == ENDPOINT))

    print("\n== 5. 中断期间并发：重放队列认领 call_17，结果回写断点清单 ==")
    claim = rq.claim_next_for_endpoint(ENDPOINT)
    check("认领成功且使用 replay 凭据", claim["ok"] and claim["credential_type"] == "replay", claim["message"])
    check("任务17进入重试中", store.find("retry_queue", 17)["status"] == "重试中")
    c17_after = bp_map()["call_17"]
    check("断点回写认领结果=已认领", c17_after["认领结果"] == "已认领")
    check("认领不推进采集边界（断点仍待重放）", c17_after["验证状态"] == "pending_replay")
    check("认领后采集边界仍只有2500", bps.confirmed_boundary(ENDPOINT) == 2500)
    # 并发重复认领必须被拒绝
    again = rq.claim_next_for_endpoint(ENDPOINT)
    check("并发重复认领被拒绝（无待重试可领或状态拦截）",
          (not again["ok"]) or store.find("retry_queue", 17)["status"] != "待重试", again.get("message"))

    print("\n== 6. 恢复迁移并回填剩余 call_88（找不到原记录→待核） ==")
    entry, msg = mig.run_action(bid, "继续回填")
    check("恢复后自动转换下一条", "call_88" in msg, msg)
    c88 = bp_map()["call_88"]
    check("call_88→待核 pending_review（找不到原记录）", c88["验证状态"] == "pending_review")
    check("待核记录不写采集位置、不挂重放任务", c88["采集位置"] == "" and c88["重放任务"] == "")
    entry, msg = mig.run_action(bid, "转换下一条")
    check("全部转换完自动完成", entry["status"] == "已完成", msg)
    check("计数：确认1/待重放1/待核1",
          (entry["已确认条目数"], entry["待重放条目数"], entry["待核条目数"]) == (1, 1, 1),
          (entry["已确认条目数"], entry["待重放条目数"], entry["待核条目数"]))

    print("\n== 7. 检查待重放/待核旧记录与下一次采集范围 ==")
    nxt = bps.next_collection_range(ENDPOINT)
    check("下一次采集仅由已确认位置推进: start=2500", nxt["start"] == 2500 and nxt["credential_type"] == "collection", nxt)
    check("待重放清单保留 call_17", [b["原始游标"] for b in bps.replay_todo(ENDPOINT)] == ["call_17"])
    check("待核清单保留 call_88", [b["原始游标"] for b in bps.review_todo(ENDPOINT)] == ["call_88"])

    print("\n== 8. 端点上线：新旧并行，取数/重放使用各自位置凭据 ==")
    entry, msg = mig.run_action(bid, "端点上线")
    check("端点上线状态=新版上线", entry["端点上线状态"] == "新版上线", msg)
    t91 = next(t for t in store.rows("incr_tracker") if t["id"] == 91)
    check("增量取数凭据 collection:cursor=2500", t91["采集凭据"] == "collection:cursor=2500", t91["采集凭据"])
    check("重放凭据 replay:call_17 不串用", t91["重放凭据"] == "replay:call_17", t91["重放凭据"])

    print("\n== 9. 迁移完成后再用重放队列领取该端点下一段，并回查增量追踪清单 ==")
    # 任务17此前认领失败场景演示：先 fail 回到待重试，再由队列领取下一段
    fail_res = rq.replay_claim(17, "fail")
    check("重放失败后任务保留待重试、未跳过", fail_res["task"]["status"] == "待重试")
    claim2 = rq.claim_next_for_endpoint(ENDPOINT)
    check("再次领取下一段成功", claim2["ok"] and claim2["task"]["id"] == 17, claim2.get("message"))
    manifest = mig.migration_report(bid)["tracker_manifest"]
    check("回查清单：三条旧值均可查", {m["原始游标"] for m in manifest} == {"2500", "call_17", "call_88"})
    check("清单凭据分列：取数2500 / 重放call_17",
          all(m["采集凭据"] == "collection:cursor=2500" for m in manifest)
          and all(m["重放凭据"] == "replay:call_17" for m in manifest))

    print("\n== 10. 回滚：只撤销断点，真实已采范围与待重放请求都保留 ==")
    entry, msg = mig.run_action(bid, "回滚批次")
    check("批次=已回滚", entry["status"] == "已回滚", msg)
    check("全部断点置为已回滚", all(b["验证状态"] == "reverted" for b in bp_map().values()))
    check("待重放任务17仍在队列中（未被跳过）", store.find("retry_queue", 17) is not None)
    check("旧游标原值仍在增量追踪清单",
          {t["当前游标"] for t in store.rows("incr_tracker") if t["目标端点"] == ENDPOINT}
          == {"2500", "call_17", "call_88"})
    check("回滚后无 confirmed 边界可冒充已采", bps.confirmed_boundary(ENDPOINT) is None)
    check("成功采集日志 CALL-0018 未被删除",
          any(l.get("记录编号") == "CALL-0018" and l["status"] == "成功" for l in store.rows("call_log")))

    print("\n== 11. 重复登记幂等：回滚后重新登记不产生重复断点，重新迁移结果一致 ==")
    entry2, msg2 = mig.run_action(bid, "登记批次")
    check("重新登记仍扫描3条旧游标", entry2["待转换条目数"] == 3, msg2)
    for _ in range(3):
        mig.run_action(bid, "转换下一条")
    states = {raw: bp["验证状态"] for raw, bp in bp_map().items()}
    check("重迁结果一致：2500确认/call_17待重放/call_88待核",
          states == {"2500": "confirmed", "call_17": "pending_replay", "call_88": "pending_review"}, states)
    check("重放任务仍只有一条（未重复登记）",
          len([r for r in store.rows("retry_queue") if r.get("请求引用") == "call_17"]) == 1)
    check("重迁后采集边界恢复为2500", bps.confirmed_boundary(ENDPOINT) == 2500)

    print("\n" + ("全部断言通过 ✅" if not failures else f"存在失败断言：{failures} ❌"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
