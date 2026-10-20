"""对比外送（麦乐送）与到店取餐柜：菜单、价格、配送费、用券差异。

只做只读查询，不下单。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_call  # noqa: E402

STORE = "1450386"
BE = "145038602"
ADDR = "1036586140206618117513916976"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evidence")


def dump(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return p


def call(tool, args):
    r = mcp_call.call_tool(tool, args)
    return r


def menu(order_type, be_type, be_code, rd):
    args = {"storeCode": STORE, "orderType": order_type, "beType": be_type,
            "reservationDate": rd}
    if be_code:
        args["beCode"] = be_code
    r = call("query-meals", args)
    return r


def main():
    rd = sys.argv[1] if len(sys.argv) > 1 else "2026-10-21 07:30"

    print("=" * 76)
    print(f"门店 {STORE}  预约时点 {rd}")
    print("=" * 76)

    d = menu(2, 2, BE, rd)
    dump("cmp_delivery_menu.json", d)
    print("外送菜单 success:", d.get("success"), "| code:", d.get("code"), "|", d.get("message"))
    if not d.get("success"):
        print("  错误:", json.dumps(d.get("data"), ensure_ascii=False)[:300])

    s = menu(1, 1, None, rd)
    dump("cmp_store_menu.json", s)
    print("到店菜单 success:", s.get("success"), "| code:", s.get("code"), "|", s.get("message"))

    for tag, resp in (("外送", d), ("到店", s)):
        data = resp.get("data") or {}
        meals = data.get("meals") or {}
        cats = data.get("categories") or []
        print(f"\n--- {tag}：{len(meals)} 个餐品，{len(cats)} 个分类 ---")
        for c in cats:
            codes = [x.get("code") for x in (c.get("meals") or [])]
            print(f"  ■ {c.get('name')} ({len(codes)})")

    # 交集对比：同名餐品的菜单价
    dm = ((d.get("data") or {}).get("meals") or {})
    sm = ((s.get("data") or {}).get("meals") or {})
    dmap = {v["name"]: (v, k) for k, v in dm.items()}
    smap = {v["name"]: (v, k) for k, v in sm.items()}
    both = [n for n in smap if n in dmap]
    print(f"\n--- 同名餐品 {len(both)} 个，菜单价对比 ---")
    for n in both[:25]:
        a = smap[n][0].get("currentPrice")
        b = dmap[n][0].get("currentPrice")
        mark = "" if str(a) == str(b) else "  ← 不同"
        print(f"  {n:<30} 到店 {a:>6} | 外送 {b:>6}{mark}")


if __name__ == "__main__":
    main()
