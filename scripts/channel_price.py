"""渠道实测：同一餐品在「到店取餐柜」与「麦乐送到家」的真实应付金额对比。

关键点：菜单价不等于应付价。这里全部用 calculate-price 取权威金额（单位：分）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_call  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "evidence")

STORE = "1450386"
BE = "145038602"
ADDR = "1036586140206618117513916976"
RD = sys.argv[1] if len(sys.argv) > 1 else "2026-10-21 07:30"

WANT = ["猪柳蛋麦满分", "火腿扒麦满分", "原味板烧鸡腿麦满分",
        "大脆鸡扒麦满分", "脆薯饼", "吉士蛋麦满分"]


def load(name):
    p = os.path.join(OUT, name)
    return json.load(open(p, encoding="utf-8"))


def dump(name, obj):
    json.dump(obj, open(os.path.join(OUT, name), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def price(items, order_type, be_code):
    args = {"storeCode": STORE, "orderType": order_type, "beType": 1 if order_type == 1 else 2,
            "reservationDate": RD, "items": items}
    if be_code:
        args["beCode"] = be_code
    return mcp_call.call_tool("calculate-price", args)


def main():
    sm = ((load("cmp_store_menu.json").get("data") or {}).get("meals") or {})
    dm = ((load("cmp_delivery_menu.json").get("data") or {}).get("meals") or {})

    smap = {v["name"]: k for k, v in sm.items()}
    dmap = {v["name"]: k for k, v in dm.items()}

    print(f"门店 {STORE} | 时点 {RD}\n")
    rows = []
    for n in WANT:
        sc, dc = smap.get(n), dmap.get(n)
        if not sc and not dc:
            print(f"跳过 {n}：两边都没有")
            continue
        r = {"name": n, "store_code": sc, "delivery_code": dc}
        # 到店
        if sc:
            p = price([{"productCode": sc, "quantity": 1}], 1, None)
            d = p.get("data") or {}
            r["store_cents"] = d.get("price")
            r["store_ok"] = p.get("success")
            dump(f"cp_store_{sc}.json", p)
        # 外送
        if dc:
            p = price([{"productCode": dc, "quantity": 1}], 2, BE)
            d = p.get("data") or {}
            r["delivery_cents"] = d.get("price")
            r["delivery_ok"] = p.get("success")
            if not p.get("success"):
                r["delivery_err"] = json.dumps(p.get("data"), ensure_ascii=False)[:200]
            dump(f"cp_delivery_{dc}.json", p)
        rows.append(r)
        a = r.get("store_cents")
        b = r.get("delivery_cents")
        fa = f"¥{a/100:.2f}" if a is not None else "-"
        fb = f"¥{b/100:.2f}" if b is not None else "-"
        gap = ""
        if a and b:
            gap = f"  差 +¥{(b-a)/100:.2f} ({(b/a-1):+.0%})"
        print(f"{n:<20} 到店 {fa:>8} | 外送 {fb:>8}{gap}")
        if r.get("delivery_err"):
            print(f"    外送错误: {r['delivery_err']}")

    dump("cp_summary.json", rows)

    # 外送费用明细：单点一个小额商品，看是否有配送费/打包费
    print("\n--- 外送费用拆解（看配送费与起送）---")
    if "脆薯饼" in dmap:
        p = price([{"productCode": dmap["脆薯饼"], "quantity": 1}], 2, BE)
        d = p.get("data") or {}
        print("商品: 脆薯饼")
        for k, v in d.items():
            if k in ("takeWayList",):
                continue
            print(f"   {k} = {v}")
    if smap:
        p = price([{"productCode": list(smap.values())[0], "quantity": 1}], 1, None)
        d = p.get("data") or {}
        print("\n--- 到店费用拆解（同一字段口径）---")
        for k, v in d.items():
            if k in ("takeWayList",):
                continue
            print(f"   {k} = {v}")
        print("   takeWayList =", json.dumps(d.get("takeWayList"), ensure_ascii=False))


if __name__ == "__main__":
    main()
