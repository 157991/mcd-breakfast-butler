#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实链路测试（Live Test）

设计原则
--------
1. **读写分离**：默认只跑只读链路（不产生任何订单）。真下订单必须显式加
   `--execute-order`，且脚本会先打印将要提交的内容，再自行撤销。
2. **每步留证**：每一步的请求参数、完整响应、服务端 traceId、本地时间戳
   全部落盘到 evidence/live/<时间戳>/，便于逐条复核。
3. **不做假设**：所有判断只依赖本次调用真实返回的字段。任何拿不到的字段
   一律标 UNKNOWN，不用默认值冒充。

用法
----
    # 只读链路（安全，可反复跑）
    python scripts/live_test.py --store 1450034 --date 2026-10-21 --time 07:00

    # 加上真实下单 + 立即取消（零扣款：create-order 只生成待支付订单）
    python scripts/live_test.py --store 1450034 --date 2026-10-21 --time 07:00 \
        --execute-order

    # 强制重新解析套餐组成（忽略本地缓存）
    python scripts/live_test.py --store 1450034 --fresh
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import mcp_call              # noqa: E402
import resolver as rs        # noqa: E402
import planner               # noqa: E402


class Recorder:
    """把每一步调用原样落盘，形成可复核的时间线。"""

    def __init__(self, run_dir: str):
        self.dir = run_dir
        self.steps: list[dict] = []
        os.makedirs(run_dir, exist_ok=True)

    def call(self, name: str, args: dict, label: str = "") -> dict:
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.time()
        ok, err, resp = True, None, {}
        try:
            resp = mcp_call.call_tool(name, args, save=False)
        except BaseException as e:      # 网络/解析异常都要留证，不能让测试中断
            ok, err = False, f"{type(e).__name__}: {e}"
            resp = {}
        ms = int((time.time() - t0) * 1000)

        step = {
            "seq": len(self.steps) + 1,
            "label": label or name,
            "tool": name,
            "args": args,
            "started_at": started,
            "elapsed_ms": ms,
            "ok": ok,
            "error": err,
            "traceId": resp.get("traceId"),
            "code": resp.get("code"),
            "success": resp.get("success"),
            "response": resp,
        }
        self.steps.append(step)

        fn = f"{step['seq']:02d}_{name}.json"
        with open(os.path.join(self.dir, fn), "w", encoding="utf-8") as f:
            json.dump(step, f, ensure_ascii=False, indent=2)

        flag = "OK " if ok and resp.get("success") is not False else "ERR"
        print(f"  [{flag}] {step['seq']:>2}. {label or name:<34} {ms:>5}ms  "
              f"trace={str(step['traceId'])[:12]}")
        if err:
            print(f"         error: {err}")
        return resp

    def save(self) -> str:
        p = os.path.join(self.dir, "timeline.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"steps": self.steps}, f, ensure_ascii=False, indent=2)
        return p


def money(cents) -> str:
    """计价接口的单位是「分」，统一换算成元再展示。"""
    try:
        return f"¥{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return "UNKNOWN"


def find_store(rec: Recorder, store_code: str, city: str, keyword: str) -> dict | None:
    resp = rec.call("query-nearby-stores",
                    {"beType": 1, "searchType": 2, "city": city, "keyword": keyword},
                    "查门店(到店自取)")
    for s in (resp.get("data") or []):
        if str(s.get("storeCode")) == str(store_code):
            return s
    data = resp.get("data") or []
    return data[0] if data else None


def pick_slot(store: dict, target: date) -> str | None:
    """从门店槽位里取出目标日期实际的可预约文案。"""
    for opt in (store.get("reservationTimeOptions") or []):
        if str(opt.get("date")) == target.isoformat():
            return opt.get("reservationOptionText")
    return None


def build_menu(rec: Recorder, store_code: str, reservation: str,
               fresh: bool) -> list[dict]:
    """拉菜单并为每个餐品解析热量。返回 planner 可用的 menu。"""
    resp = rec.call("query-meals",
                    {"storeCode": store_code, "orderType": 1, "beType": 1,
                     "reservationDate": reservation},
                    "查早餐菜单(预约时点)")
    data = resp.get("data") or {}
    meals = data.get("meals") or {}

    if fresh:
        rs.DETAILS = os.path.join(rec.dir, "details")
        os.makedirs(rs.DETAILS, exist_ok=True)

    idx = rs.load_nutrition()
    items: list[dict] = []
    for i, (code, m) in enumerate(meals.items(), 1):
        name = m.get("name", "")
        r = rs.resolve_meal(idx, store_code, 1, 1, code, name, reservation)
        items.append({
            "code": code, "name": name,
            "price": m.get("currentPrice"),
            "original_price": m.get("originalPrice"),
            "kcal": r.get("kcal"),
            "kcal_source": r.get("source"),
            "kcal_confidence": r.get("confidence"),
            "parts": r.get("parts"),
            "note": r.get("note"),
            # 组合是否被完整拆开：False 意味着「不知道里面含什么」，
            # 禁用词无法穿透，planner 会把这类候选降权并标注。
            "is_combo": rs.is_combo(name),
            "parts_complete": r.get("source") == "official_combo",
        })
        if i % 20 == 0:
            print(f"         …已解析 {i}/{len(meals)} 个餐品")
    return items


def coverage(items: list[dict]) -> dict:
    total = len(items)
    known = [i for i in items if i["kcal"] is not None]
    by_src: dict[str, int] = {}
    for i in known:
        by_src[i["kcal_source"]] = by_src.get(i["kcal_source"], 0) + 1
    return {
        "total": total,
        "known": len(known),
        "coverage_pct": round(len(known) / total * 100, 1) if total else 0.0,
        "by_source": by_src,
        "unknown_names": [i["name"] for i in items if i["kcal"] is None],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="1450034")
    ap.add_argument("--city", default="上海")
    ap.add_argument("--keyword", default="徐家汇")
    ap.add_argument("--date", default="")
    ap.add_argument("--time", default="07:00")
    ap.add_argument("--execute-order", action="store_true",
                    help="真实创建订单并立即取消（create-order 不扣款，仅生成待支付订单）")
    ap.add_argument("--fresh", action="store_true", help="忽略本地套餐缓存")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(ROOT, "evidence", "live", stamp)
    rec = Recorder(run_dir)

    print("=" * 74)
    print(f"真实链路测试  run={stamp}  门店={args.store}")
    print("=" * 74)

    report: dict = {"run": stamp, "store": args.store, "phases": {}}
    ok_chain = True

    # ---------- 阶段 1：只读链路 ----------
    print("\n[阶段 1] 只读链路（不产生订单）")

    t = rec.call("now-time-info", {}, "服务端时间基准")
    server_date = (t.get("data") or {}).get("date")
    target = date.fromisoformat(args.date) if args.date else \
        (date.fromisoformat(server_date) if server_date else date.today())
    reservation = f"{target.isoformat()} {args.time}"
    report["reservation"] = reservation
    report["server_date"] = server_date

    acct = rec.call("query-my-account", {}, "账户状态")
    report["account_bound"] = bool((acct.get("data") or {}).get("accountId"))

    addr = rec.call("delivery-query-addresses", {}, "外送地址")
    addresses = (addr.get("data") or {}).get("addresses") or []
    report["delivery_address_count"] = len(addresses)

    store = find_store(rec, args.store, args.city, args.keyword)
    if not store:
        print("\n致命：找不到门店，测试终止")
        rec.save()
        sys.exit(1)
    report["store"] = {
        "storeCode": store.get("storeCode"),
        "storeName": store.get("storeName"),
        "reservation": store.get("reservation"),
        "businessStart": store.get("businessStartTime"),
        "businessEnd": store.get("businessEndTime"),
        "slot_text": pick_slot(store, target),
    }
    print(f"         门店: {store.get('storeName')} ({store.get('storeCode')})")
    print(f"         目标槽位: {report['store']['slot_text']}")

    items = build_menu(rec, str(store.get("storeCode")), reservation, args.fresh)
    cov = coverage(items)
    report["phases"]["menu"] = {"item_count": len(items), "coverage": cov}
    print(f"         餐品 {cov['total']} 个，官方热量可算 {cov['known']} 个 "
          f"({cov['coverage_pct']}%)  sources={cov['by_source']}")

    profile = planner.load_json(os.path.join(ROOT, "data", "profile.example.json"), {})
    ledger = planner.load_json(os.path.join(ROOT, "data", "ledger.normal.json"), {})
    res = planner.plan(profile, ledger, items, target)
    report["phases"]["plan"] = {
        "pick": {'name': res['pick']['name'], 'code': res['pick']['code'],
                 'price': res['pick']['price'], 'kcal': res['pick']['kcal']} if res.get("pick") else None,
        "trace": res.get("trace"),
        "reason": res.get("reason"),
    }
    if not res.get("pick"):
        print("\n决策引擎无输出，测试终止")
        rec.save()
        sys.exit(1)

    pick = res["pick"]
    print(f"\n         初选: {pick['name']}  菜单价¥{pick['price']}  {pick['kcal']} kcal")

    # ---------- 精确核价：菜单价不可信，必须逐项用 calculate-price 复核 ----------
    # 实测：菜单 currentPrice 是「早餐卡专享价」，与实际应付差 60%+。
    # 直接用菜单价做预算判断会让订单真实金额超预算，因此对候选逐个核价。
    cands = [pick] + [a for a in (res.get("alternatives") or [])[:2]]
    priced: list[dict] = []
    for c in cands:
        pr = rec.call("calculate-price",
                      {"storeCode": str(store.get("storeCode")), "orderType": 1,
                       "beType": 1, "reservationDate": reservation,
                       "items": [{"productCode": c["code"], "quantity": 1}]},
                      f"核价 {c['name'][:16]}")
        pd = pr.get("data") or {}
        priced.append({
            "name": c["name"], "code": c["code"], "kcal": c["kcal"],
            "menu_price": c["price"],
            "real_cents": pd.get("price"),
            "original_cents": pd.get("originalPrice"),
            "discount_cents": pd.get("discount"),
            "takeWayList": [{"code": x.get("code"), "title": x.get("title")}
                            for x in (pd.get("takeWayList") or [])],
        })

    print("\n         菜单价 vs 实际应付：")
    for p in priced:
        if p["real_cents"] is None:
            print(f"           {p['name'][:20]:<22} 菜单¥{p['menu_price']} → 核价失败")
            continue
        try:
            mp = float(p["menu_price"])
        except (TypeError, ValueError):
            mp = 0.0
        delta = (p["real_cents"] / 100) / max(mp, 0.01) - 1
        print(f"           {p['name'][:20]:<22} 菜单¥{mp:>6.1f} → "
              f"{money(p['real_cents']):>8}  ({delta:+.0%})")

    budget = float(profile.get("budget_cny", 18))
    affordable = [p for p in priced
                  if p["real_cents"] is not None and p["real_cents"] / 100 <= budget]
    final = (affordable or priced)[0]
    report["phases"]["price"] = {
        "budget_cny": budget,
        "candidates": priced,
        "final": final,
        "switched": final["code"] != pick["code"],
        "takeWayList": final["takeWayList"],
    }
    if final["code"] != pick["code"]:
        print(f"         ⚠ 实际应付超预算，改选 {final['name']}")
    print(f"         最终: {final['name']} · {money(final['real_cents'])} · {final['kcal']} kcal")
    print(f"         取餐方式: {[(x['code'], x['title']) for x in final['takeWayList']]}")
    pick = {"name": final["name"], "code": final["code"], "kcal": final["kcal"],
            "price": final["menu_price"]}
    if not final["takeWayList"]:
        ok_chain = False
        print("         ⚠ takeWayList 为空，无法继续下单（takeWayCode 必填）")

    coupons = rec.call("query-store-coupons",
                       {"storeCode": str(store.get("storeCode")), "orderType": 1,
                        "beType": 1, "reservationDate": reservation},
                       "门店可用券")
    cdata = coupons.get("data")
    clist = cdata if isinstance(cdata, list) else (cdata or {}).get("data") or []
    report["phases"]["coupons"] = {"count": len(clist) if isinstance(clist, list) else "UNKNOWN"}
    print(f"         可用券: {report['phases']['coupons']['count']}")

    my_coupons = rec.call("query-my-coupons", {}, "我的券")
    report["phases"]["my_coupons_raw_type"] = type(my_coupons.get("data")).__name__

    # ---------- 阶段 2：写操作 ----------
    if not args.execute_order:
        print("\n[阶段 2] 跳过真实下单（未加 --execute-order）")
        report["phases"]["order"] = {"skipped": True}
    else:
        print("\n[阶段 2] 真实下单 → 查状态 → 取消 → 复查（零扣款）")
        tws = report["phases"]["price"]["takeWayList"] or []
        # 产品场景是「在家吃」，优先选外带取餐柜（locker-in）而非堂食。
        preferred = next((x for x in tws if x.get("code") == "locker-in"), None) or \
            (tws[0] if tws else None)
        take_way = (preferred or {}).get("code")
        if not take_way:
            print("         无 takeWayCode，跳过")
            report["phases"]["order"] = {"skipped": True, "reason": "no takeWayCode"}
        else:
            order_args = {
                "storeCode": str(store.get("storeCode")),
                "orderType": 1,
                "beType": 1,
                "takeWayCode": take_way,
                "reservationDate": reservation,
                "items": [{"productCode": pick["code"], "quantity": 1}],
            }
            print(f"         取餐方式: {take_way}")
            print(f"         提交内容: {json.dumps(order_args, ensure_ascii=False)}")
            created = rec.call("create-order", order_args, "创建订单(预约)")
            odata = created.get("data") or {}
            order_id = (odata.get("orderId") or odata.get("orderNo")
                        or odata.get("orderCode"))
            report["phases"]["order"] = {
                "created": created.get("success"),
                "order_id": order_id,
                "pay_url": odata.get("payUrl") or odata.get("paymentUrl"),
                "keys": sorted(odata.keys()),
                "raw": odata,
            }
            print(f"         订单号: {order_id}")

            if order_id:
                q1 = rec.call("query-order", {"orderId": order_id}, "查订单(下单后)")
                d1 = q1.get("data") or {}
                report["phases"]["order"]["status_after_create"] = \
                    d1.get("orderStatus") or d1.get("status") or "UNKNOWN"

                c = rec.call("cancel-order",
                             {"orderId": order_id, "cancelReasonCode": "1"},
                             "取消订单")
                report["phases"]["order"]["cancel_success"] = c.get("success")
                report["phases"]["order"]["cancel_raw"] = c.get("data") or c.get("message")

                time.sleep(3)
                q2 = rec.call("query-order", {"orderId": order_id}, "查订单(取消后)")
                d2 = q2.get("data") or {}
                report["phases"]["order"]["status_after_cancel"] = \
                    d2.get("orderStatus") or d2.get("status") or "UNKNOWN"
                print(f"         取消前状态: {report['phases']['order']['status_after_create']}"
                      f" → 取消后: {report['phases']['order']['status_after_cancel']}")

                ol = rec.call("order-list", {}, "订单列表(复查)")
                report["phases"]["order"]["order_list_ok"] = ol.get("success")

    # ---------- 汇总 ----------
    report["ok_chain"] = ok_chain
    tl = rec.save()
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 74)
    print(f"时间线: {tl}")
    print(f"报告:   {os.path.join(run_dir, 'report.json')}")
    print("=" * 74)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
