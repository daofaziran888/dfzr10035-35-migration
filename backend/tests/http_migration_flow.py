"""HTTP 层端到端验证：对真实 uvicorn 服务走完整迁移剧本（标准库 urllib，无第三方依赖）。

用法：先启动后端，再执行  python -I tests/http_migration_flow.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api"
ENDPOINT = "ENDP-LEGACY-9001"
failures: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> dict:
    safe_path = urllib.parse.quote(path, safe="/?&=")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + safe_path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_detail": exc.read().decode()}


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def generic_action(module: str, entry_id: int, action: str) -> dict:
    return call("POST", f"/{module}/{entry_id}/actions", {"action": action})


def main() -> int:
    print("== 健康检查与新模块通用资源路由 ==")
    health = call("GET", "/health")
    check("服务健康且含24个模块", health.get("ok") and health.get("modules") == 24, health)
    mig_list = call("GET", "/cursor_migration")
    check("游标迁移列表可分页查询", mig_list["total"] >= 1 and "批次编号" in mig_list["items"][0])
    bp_list = call("GET", "/incr_breakpoint")
    check("增量断点列表初始为空表可查", bp_list["total"] == 0)

    print("\n== 通用资源路由驱动迁移：登记→转换→中断 ==")
    r = generic_action("cursor_migration", 1, "登记批次")
    check("登记批次成功", r["ok"] and r["entry"]["待转换条目数"] == 3, r["message"])
    r = generic_action("cursor_migration", 1, "转换下一条")
    check("转换2500成功", r["ok"] and "2500" in r["message"], r["message"])
    r = generic_action("cursor_migration", 1, "转换下一条")
    check("转换call_17成功", r["ok"] and "call_17" in r["message"], r["message"])
    r = generic_action("cursor_migration", 1, "中断迁移")
    check("中断成功", r["entry"]["status"] == "已中断", r["message"])

    print("\n== 中断期间并发认领（专用链路接口） ==")
    claim = call("POST", f"/chain/replay-claim-next?endpoint={ENDPOINT}")
    check("队列认领下一段成功", claim["ok"] and claim["credential_type"] == "replay", claim.get("message"))
    check("认领结果回写断点清单", claim["breakpoint"]["认领结果"] == "已认领")
    dup = call("POST", f"/chain/replay-claim-next?endpoint={ENDPOINT}")
    check("重复认领被拒绝", not dup["ok"], dup.get("message"))

    print("\n== 恢复回填并完成 ==")
    r = generic_action("cursor_migration", 1, "继续回填")
    check("恢复回填call_88", r["ok"] and "call_88" in r["message"], r["message"])
    r = generic_action("cursor_migration", 1, "转换下一条")
    check("迁移自动完成", r["entry"]["status"] == "已完成", r["message"])

    print("\n== 回查：待核旧记录、队列认领结果、下一次采集范围 ==")
    todo = call("GET", f"/chain/breakpoint-todo?endpoint={ENDPOINT}")
    check("待重放记录仅call_17", [b["原始游标"] for b in todo["replay_todo"]] == ["call_17"])
    check("待核旧记录仅call_88且可查", [b["原始游标"] for b in todo["review_todo"]] == ["call_88"])
    nxt = call("GET", f"/chain/next-collection-range?endpoint={ENDPOINT}&limit=100")
    check("下一次采集范围 start=2500/next=2600",
          nxt["start"] == 2500 and nxt["next_cursor"] == 2600 and nxt["credential_type"] == "collection", nxt)

    print("\n== 端点上线与迁移报告 ==")
    r = call("POST", "/chain/cursor-migration/1/action/端点上线")
    check("端点上线成功", r["ok"] and r["entry"]["端点上线状态"] == "新版上线", r["message"])
    report = call("GET", "/chain/cursor-migration/1")
    check("迁移报告含断点/待办/采集范围/清单回查",
          {"batch", "breakpoints", "replay_todo", "review_todo",
           "next_collection_range", "tracker_manifest"} <= set(report.keys()))
    check("报告中清单凭据分列",
          all(m["采集凭据"] == "collection:cursor=2500" for m in report["tracker_manifest"])
          and all(m["重放凭据"] == "replay:call_17" for m in report["tracker_manifest"]))

    print("\n== 回滚链路接口 ==")
    r = call("POST", "/chain/cursor-migration/1/action/回滚批次")
    check("回滚成功", r["entry"]["status"] == "已回滚", r["message"])
    bps = call("GET", "/incr_breakpoint?status=已回滚")
    check("3条断点全部已回滚可查", bps["total"] == 3)
    retries = call("GET", f"/retry_queue?失败端点={ENDPOINT}&status=重试中")
    check("待重放任务未被跳过仍在队列", retries["total"] == 1, retries.get("total"))

    print("\n" + ("HTTP 端到端全部通过 ✅" if not failures else f"失败：{failures} ❌"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
