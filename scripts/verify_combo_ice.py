#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：套餐内商品能不能单独改造（尤其是给套餐里的饮品去冰）。

以前这里挂着一句"未验证"，因为撞了每日下单额度。现在正面回答三个问题：

  Q1 套餐内的饮品，detail 里有没有 modification？
     —— 有。在 rounds[].choices[].modification.items[].values[]，
        code 恒为 200002，四档靠 selectedKey 区分（0-1标准 / 0-0去冰 / 0-2多冰 / 1-1少冰）

  Q2 calculate-price 认不认 roundList[].comboItemList[].modification？
     —— 认，且去冰不加价。

  Q3 create-order 之后，订单里能不能看到"去冰"？
     —— 这是唯一能证明"店员真的会照做"的证据，必须真下单。

传参路径（create-order schema 原文）：
  items[].roundList[].round                套餐轮次
  items[].roundList[].comboItemList[].code 该轮选中的商品
  items[].roundList[].comboItemList[].modification.values[]

用法：
  python verify_combo_ice.py                          # 只核价，不消耗下单额度
  python verify_combo_ice.py --execute                # 真下单（占每日额度）
  python verify_combo_ice.py --product 9900003458 --round-name 饮料 --item 507411 --ice 去冰
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_call  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = "1450386"
BE = "145038602"
ADDR = "1036586140206618117513916976"
TAKEWAY = "take-in-store"

ICE_KEYS = {"标准": "0-1", "去冰": "0-0", "多冰": "0-2", "少冰": "1-1"}


def dump(tag: str, obj) -> str:
    ts = datetime.now().strftime("%H%M%S")
    d = os.path.join(BASE, "evidence", "combo_ice", f"{datetime.now():%Y%m%d}")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{ts}_{tag}.json")
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return p


def detail_of(code: str, rd: str) -> dict:
    return mcp_call.call_tool("query-meal-detail", {
        "storeCode": STORE, "orderType": 1, "beType": 1,
        "code": code, "reservationDate": rd})


def find_ice_options(choice: dict) -> list[dict]:
    """从 choice.modification 里挖出冰量四档。挖不到返回空。"""
    vals = []
    for it in ((choice.get("modification") or {}).get("items") or []):
        for v in (it.get("values") or []):
            if v.get("code") == "200002" and v.get("name") in ICE_KEYS:
                vals.append({"label": v["name"], "code": v["code"],
                             "key": v.get("selectedKey")})
    return vals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", default="9900003458", help="套餐 code")
    ap.add_argument("--round-name", default="饮料", help="要改的 round 名（子串匹配）")
    ap.add_argument("--item", default="507411", help="round 内要改的商品 code（默认冰美式）")
    ap.add_argument("--ice", default="去冰", choices=list(ICE_KEYS))
    ap.add_argument("--date", default="2026-10-21 07:30")
    ap.add_argument("--channel", default="store", choices=["store", "delivery"])
    ap.add_argument("--execute", action="store_true", help="真下单，占每日额度")
    a = ap.parse_args()

    print("=" * 72)
    print(f"目标：套餐 {a.product} 里的 {a.item} → {a.ice}")
    print("=" * 72)

    # ---------- 1. 读结构 ----------
    det = detail_of(a.product, a.date)
    dump("01_detail", det)
    if not det.get("success"):
        print("读取套餐失败:", det.get("message"))
        raise SystemExit(1)

    data = det["data"]
    rounds = data.get("rounds") or []
    print(f"\n[1] 套餐「{data.get('name')}」共 {len(rounds)} 轮")

    round_list, target = [], None
    for rd in rounds:
        rname = rd.get("name") or ""
        rid = str(rd.get("id"))
        chs = rd.get("choices") or []
        # 指定了 --item 时，主动从全部候选里找它（用户是在「换饮品」，不是接受默认）
        explicit = next((c for c in chs if str(c.get("code")) == a.item), None)
        chosen = [c for c in chs if (c.get("quantity") or 0) > 0]
        pick = explicit or (chosen[0] if chosen else next(
            (c for c in chs if c.get("isDefault")), None))
        if not pick:
            print(f"    · [{rid}] {rname}: 未取到默认项，跳过")
            continue

        mod = None
        ice_opts = find_ice_options(pick)
        default_pick = chosen[0] if chosen else None
        if a.round_name in rname and str(pick.get("code")) == a.item:
            if not ice_opts:
                print(f"    ✗ [{rid}] {rname} → {pick.get('name')} 没有冰量选项（改不了）")
            else:
                key = next(o["key"] for o in ice_opts if o["label"] == a.ice)
                mod = {"values": [{"code": "200002", "key": key, "quantity": 1}]}
                target = {"round": rid, "name": pick.get("name"), "code": pick.get("code"),
                          "ice": a.ice, "key": key, "options": ice_opts}
                print(f"    ★ [{rid}] {rname} → {pick.get('name')} 设「{a.ice}」key={key}")
        else:
            mark = f"可改冰({len(ice_opts)}档)" if ice_opts else "无冰量选项"
            print(f"    · [{rid}] {rname} → {pick.get('name')} [{pick.get('code')}] {mark}")

        # 记录了默认饮品被替换掉这件事——它本身是个结论
        if explicit is not None and default_pick is not None \
                and str(default_pick.get("code")) != a.item:
            print(f"       ⚠ 默认饮品「{default_pick.get('name')}」"
                  f"[{default_pick.get('code')}] 被主动替换")

        entry = {"round": rid, "comboItemList": [
            {"code": str(pick.get("code")), "quantity": 1}]}
        if mod:
            entry["comboItemList"][0]["modification"] = mod
        round_list.append(entry)

    if not target:
        print(f"\n未命中目标 round/item（--round-name {a.round_name} --item {a.item}）")
        raise SystemExit(2)

    item = {"productCode": a.product, "quantity": 1, "roundList": round_list}
    delivery = a.channel == "delivery"
    args = {
        "storeCode": STORE,
        "orderType": 2 if delivery else 1,
        "beType": 2 if delivery else 1,
        "reservationDate": a.date,
        "items": [item],
    }
    if delivery:
        args["beCode"] = BE
        args["addressId"] = ADDR
    else:
        args["takeWayCode"] = TAKEWAY

    print("\n[2] calculate-price 请求体：")
    print(json.dumps(item, ensure_ascii=False, indent=1))

    pr = mcp_call.call_tool("calculate-price", args)
    dump("02_price", pr)
    pd = pr.get("data") or {}
    print(f"\n[3] 核价 success={pr.get('success')} msg={pr.get('message')}")
    if pr.get("success"):
        print(f"    实付 {pd.get('price')} 分 = {(pd.get('price') or 0)/100:.2f} 元")
        for x in (pd.get("productList") or pd.get("items") or [])[:4]:
            print("     ·", json.dumps(x, ensure_ascii=False)[:160])
    else:
        print(json.dumps(pd, ensure_ascii=False)[:600])
        raise SystemExit(3)

    if not a.execute:
        print("\n[4] 未加 --execute，到此为止（未消耗下单额度）")
        print("    已证明 calculate-price 接受 roundList 内的 modification。")
        return

    print("\n[4] 真下单 create-order ……（占每日额度）")
    o = mcp_call.call_tool("create-order", args)
    dump("03_create", o)
    print(f"    success={o.get('success')} msg={o.get('message')}")
    if not o.get("success"):
        print(json.dumps(o.get("data"), ensure_ascii=False)[:800])
        raise SystemExit(4)

    d = o.get("data") or {}
    od = d.get("orderDetail") or {}
    oid = od.get("orderId") or d.get("orderId")
    print(f"    订单号: {oid}")
    print(f"    支付截止: {od.get('expirePayTime')}")
    print(f"    金额: {float(od.get('totalAmount') or 0):.2f} 元")

    print("\n[5] query-order 复查订单里的真实商品行")
    q = mcp_call.call_tool("query-order", {"orderId": oid})
    dump("04_query", q)
    qd = q.get("data") or {}
    for x in (qd.get("orderProductList") or []):
        print(f"    · {x.get('productName')} ×{x.get('quantity')}")
        for c in (x.get("comboItemList") or []):
            extra = {k: v for k, v in c.items()
                     if k not in ("itemName", "itemQuantity") and v not in (None, "", [], {})}
            print(f"        └ {c.get('itemName')} ×{c.get('itemQuantity')} "
                  f"{json.dumps(extra, ensure_ascii=False)[:220] if extra else ''}")

    pay = d.get("payH5Url") or od.get("payH5Url")
    print(f"\n    支付链接: {pay}")
    print(f"\n证据已落盘: evidence/combo_ice/{datetime.now():%Y%m%d}/")


if __name__ == "__main__":
    main()
