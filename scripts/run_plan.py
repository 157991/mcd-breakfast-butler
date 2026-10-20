"""执行前一晚算好的计划（定时任务入口）。

设计要点：
1. **执行前必须复核金额。** 菜单、库存、券在一夜之间都会变。计划里的价格只是
   昨晚的价，不是现在的价。偏离超过阈值就不下单，改为推送"要不要换"。
2. **只执行到「待支付」。** 35 个工具里没有扣款能力，这里也只能拿到 payH5Url。
3. **幂等。** 同一条计划重复执行前先查 db，已 executed 的直接跳过，
   避免定时任务重跑产生重复订单。

用法：
    python scripts/run_plan.py                  # 执行 20 分钟内到点的计划
    python scripts/run_plan.py --dry-run        # 只复核不下单
    python scripts/run_plan.py --id 3          # 指定计划
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import mcp_call  # noqa: E402

# 金额偏离容忍度：复核价超出计划价 10% 或超出预算，就不自动下单
DEVIATION_LIMIT = 0.10


def price_now(payload: dict) -> dict:
    """用当前真实价格复核计划里的订单。"""
    args = {
        "storeCode": payload["storeCode"],
        "orderType": payload["orderType"],
        "beType": payload["beType"],
        "items": payload["items"],
    }
    if payload.get("beCode"):
        args["beCode"] = payload["beCode"]
    if payload.get("reservationDate"):
        args["reservationDate"] = payload["reservationDate"]
    return mcp_call.call_tool("calculate-price", args)


def run_one(plan: dict, dry: bool = False) -> dict:
    payload = plan["payload"]
    out = {"plan_id": plan["id"], "target_at": plan["target_at"], "actions": []}

    # 1) 复核金额
    chk = price_now(payload)
    if not chk.get("success"):
        out["result"] = "price_failed"
        out["message"] = chk.get("message")
        return out
    now_cents = (chk.get("data") or {}).get("price")
    out["price_now_cents"] = now_cents

    planned = (plan.get("reason_json") and
               json.loads(plan["reason_json"]).get("planned_cents"))
    out["price_planned_cents"] = planned

    prof = db.get_profile() or {}
    budget_cents = int((prof.get("budget_cny") or 999) * 100)

    if now_cents and now_cents > budget_cents:
        out["result"] = "over_budget"
        out["message"] = f"复核价 {now_cents/100:.2f} 超预算 {budget_cents/100:.2f}，不下单"
        return out

    if planned and now_cents and planned > 0:
        gap = now_cents / planned - 1
        out["deviation"] = round(gap, 4)
        if gap > DEVIATION_LIMIT:
            out["result"] = "price_jumped"
            out["message"] = (f"价格从 {planned/100:.2f} 涨到 {now_cents/100:.2f}"
                              f"（+{gap:.0%}），不下单")
            return out

    # 2) 商品是否还在售（复核时已隐含，但售罄可能只在下单时才报）
    if dry:
        out["result"] = "dry_run_ok"
        return out

    # 3) 下单（只到待支付）
    args = dict(payload)
    created = mcp_call.call_tool("create-order", args)
    if not created.get("success"):
        out["result"] = "create_failed"
        out["message"] = created.get("message")
        return out

    od = (created.get("data") or {}).get("orderDetail") or {}
    pay_url = (created.get("data") or {}).get("payH5Url") or od.get("payH5Url")

    od_for_db = dict(od)
    od_for_db.setdefault("storeCode", payload.get("storeCode"))
    od_for_db.setdefault("orderType", payload.get("orderType"))
    od_for_db.setdefault("items", payload.get("items"))
    od_for_db["payH5Url"] = pay_url
    db.save_order(od_for_db)
    db.mark_plan(plan["id"], "executed", od.get("orderId"))

    out.update({
        "result": "created",
        "order_id": od.get("orderId"),
        "pay_url": pay_url,
        "pay_expire_at": od.get("expirePayTime"),
        "real_cents": od.get("realTotalAmount"),
        "discount_cents": od.get("totalDiscountAmount"),
        "coupons": od.get("couponList") or [],
        "combo": [c.get("itemName") for p in (od.get("orderProductList") or [])
                  for c in (p.get("comboItemList") or [])],
    })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="执行到点的早餐计划（只到待支付）")
    ap.add_argument("--dry-run", action="store_true", help="只复核金额，不下单")
    ap.add_argument("--id", type=int, help="指定计划 id")
    ap.add_argument("--window", type=int, default=20, help="提前多少分钟内执行")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()

    db.connect().close()
    plans = [p for p in db.due_plans(a.window)] if not a.id else _by_id(a.id)
    if not plans:
        if a.json:
            print(json.dumps({"executed": 0, "reason": "no_due_plan"}, ensure_ascii=False))
        else:
            print(f"[{datetime.now():%H:%M}] 没有到点的计划，不做任何操作。")
        return 0

    results = [run_one(p, dry=a.dry_run) for p in plans]

    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    for r in results:
        print(f"\n计划 #{r['plan_id']} · 目标 {r['target_at']}")
        print(f"  结果: {r['result']}")
        if r.get("message"):
            print(f"  说明: {r['message']}")
        if r.get("price_now_cents") is not None:
            print(f"  复核价: ¥{r['price_now_cents']/100:.2f}"
                  f"（计划价 ¥{(r.get('price_planned_cents') or 0)/100:.2f}）")
        if r.get("result") == "created":
            print(f"  订单: {r['order_id']}")
            print(f"  实付: ¥{(r.get('real_cents') or 0)/100:.2f}")
            if r.get("coupons"):
                print(f"  用券: {[c.get('couponName') for c in r['coupons']]}")
            if r.get("combo"):
                print(f"  组成: {' + '.join(r['combo'])}")
            print(f"  支付: {r['pay_url']}")
            print(f"  截止: {r['pay_expire_at']}  ← 必须在此之前付款")
    return 0


def _by_id(pid: int) -> list[dict]:
    with db.connect() as c:
        row = c.execute("SELECT * FROM plan WHERE id=?", (pid,)).fetchone()
    if not row:
        return []
    d = dict(row)
    d["payload"] = json.loads(d["payload_json"])
    return [d]


if __name__ == "__main__":
    raise SystemExit(main())
