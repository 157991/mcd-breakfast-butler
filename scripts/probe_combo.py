#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测套餐 detail 的 rounds/choices 结构，确认「套餐内商品」能否被单独改造。

要回答的问题只有一个：
  套餐里那个「鲜萃冰咖」，在 query-meal-detail 的返回里
  是**只有名字的字符串**，还是**带 modification 的完整对象**？
  只有后者才谈得上"给套餐内的饮品去冰"。

用法：
  python probe_combo.py 9900003458
  python probe_combo.py 9900003458 --raw        # 打印原始 JSON 片段
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_call  # noqa: E402

STORE = "1450386"


def probe(code: str, store: str, order_type: int = 1, be_code: str = "",
          reservation_date: str = "") -> dict:
    params = {"storeCode": store, "orderType": order_type, "beType": 1, "code": code}
    if be_code:
        params["beCode"] = be_code
    if reservation_date:
        # 实测：不传这个，非当前供餐时段会返回「未匹配到商品」。
        # 不是商品不存在，是营业时段对不上——一律带上。
        params["reservationDate"] = reservation_date
    detail = mcp_call.call_tool("query-meal-detail", params)
    return detail


def walk(obj, depth: int = 0, path: str = "$"):
    """把所有出现 modification / rounds / choices 的节点找出来。"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if k in ("modification", "rounds", "choices", "comboItemList", "comboGroups"):
                hits.append((p, k, type(v).__name__, len(v) if hasattr(v, "__len__") else 1))
            hits.extend(walk(v, depth + 1, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:6]):
            hits.extend(walk(v, depth + 1, f"{path}[{i}]"))
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--store", default=STORE)
    ap.add_argument("--order-type", type=int, default=1)
    ap.add_argument("--be-code", default="")
    ap.add_argument("--date", default="2026-10-21 07:30", help="预约时间，早餐时段必传")
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()

    d = probe(a.code, a.store, a.order_type, a.be_code, a.date)
    print(f"success={d.get('success')}  message={d.get('message')}")
    if not d.get("success"):
        return

    data = d.get("data") or {}
    print(f"商品: {data.get('name')}  supportModify={data.get('supportModify')}")
    print(f"顶层字段: {sorted(data.keys())}\n")

    print("=== 命中 modification / rounds / choices 的节点 ===")
    for p, k, t, n in walk(data):
        print(f"  {p:<60} {k:<16} {t}({n})")

    rounds = data.get("rounds") or []
    print(f"\n=== rounds 共 {len(rounds)} 组 ===")
    for i, rd in enumerate(rounds):
        chs = rd.get("choices") or []
        print(f"\n[{i}] name={rd.get('name')!r} id={rd.get('id')} "
              f"maxQty={rd.get('maxQuantity')} minQty={rd.get('minQuantity')} "
              f"choices={len(chs)}")
        if chs:
            print(f"    choice 字段: {sorted(chs[0].keys())}")
            for c in chs[:8]:
                flags = []
                if c.get("isDefault"):
                    flags.append("DEFAULT")
                if (c.get("quantity") or 0) > 0:
                    flags.append(f"qty={c.get('quantity')}")
                has_mod = "modification" in c
                print(f"      - {str(c.get('name'))[:28]:<30} code={c.get('code')} "
                      f"{' '.join(flags)} mod={'YES' if has_mod else 'no'}")
                if has_mod and a.raw:
                    print("        modification:", json.dumps(c["modification"], ensure_ascii=False)[:400])

    if a.raw:
        print("\n=== 原始 data（截断 4000）===")
        print(json.dumps(data, ensure_ascii=False, indent=1)[:4000])


if __name__ == "__main__":
    main()
