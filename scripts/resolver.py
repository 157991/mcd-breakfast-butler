#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
餐品 → 热量 解析器（作品核心模块）

问题背景（来自实测，不是推测）：
  1. query-meals 给出的菜单只有 code / name / price，**没有任何营养字段**。
  2. list-nutrition-foods 给出的是以「餐品中文名」为键的文本表，**没有 code**，
     两边的键不相交，只能靠名称对齐；直接对齐只有 16% 命中。
  3. 菜单里大量是「套餐/组合」，其热量不等于任何单个营养表条目。

解法（三级解析，每一级都记录来源与置信度）：
  L1 单品直配   ：菜单名 == 营养库名            → 官方数据，置信度高
  L2 套餐拆解   ：query-meal-detail 取 rounds[].choices[isDefault=1]
                  → 组合内单品逐个走 L1 → 加总          → 官方数据，置信度高
  L3 无官方数据 ：明确标记 missing，仅在展示时给区间，**绝不伪装成精确值**

产出：
  data/breakfast_menu.json    带热量的早餐菜单（含来源与置信度）
  evidence/resolve_report.md  覆盖率报告
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import mcp_call  # noqa: E402

EV = os.path.join(ROOT, "evidence")
DATA = os.path.join(ROOT, "data")
DETAILS = os.path.join(EV, "details")

# 早餐相关分类关键词
BREAKFAST_KEYS = ("早餐", "麦满分", "松饼", "营养卷", "粥", "麦咖啡", "现磨", "豆浆")


def normalize(name: str) -> str:
    n = re.sub(r"[（(].*?[)）]", "", name)
    n = re.sub(r"[™\s·・]", "", n)
    return n.strip()


def strip_combo(name: str) -> str:
    """去掉「套餐/组合/超值/6折」等营销后缀，回到商品本体名。"""
    n = normalize(name)
    for suf in ("套餐", "组合", "超值", "早餐", "单人餐", "双人餐"):
        if n.endswith(suf) and len(n) > len(suf) + 1:
            n = n[: -len(suf)]
    return n


def load_nutrition() -> dict:
    rows = json.load(open(os.path.join(DATA, "nutrition.json"), encoding="utf-8"))
    idx = {}
    for r in rows:
        for key in (r["productName"], normalize(r["productName"])):
            idx.setdefault(key, r)
    return idx


SPEC_NOISE = ("中杯", "大杯", "小杯", "（冰/热）", "(冰/热)", "冰/热", "™", "®")

# 「套餐 / 组合」不是营销后缀，是商品结构差异：
# 套餐 = 主食 + 小食 + 饮品，其热量必然高于同名单品。
# 实测中若按去后缀匹配，「猪柳蛋麦满分套餐」会拿到单品 387 kcal，
# 而实际套餐通常 500-600 kcal —— 这是系统性低估，且方向恒定（永远偏低），
# 比随机误差更危险，因为它会让系统误判"今天还剩很多额度"。
COMBO_MARKERS = ("套餐", "组合", "二件套", "三件套", "四件套")


def is_combo(name: str) -> bool:
    return any(m in name for m in COMBO_MARKERS)


def name_variants(name: str) -> set[str]:
    """只生成「规格层面的等价变体」：去掉杯型、冷热标记、商标符。

    刻意不做两件事：
      1. 子串包含匹配 —— 「北非蛋风味猪柳蛋麦满分」会错配到「猪柳蛋麦满分」；
      2. 剥离「套餐/组合」—— 会把套餐热量错当单品热量。
    宁可判定无数据，也不能给用户一个方向错误的官方数字。
    """
    base = normalize(name)
    out = {base}
    v = base
    for w in SPEC_NOISE:
        v = v.replace(w, "")
    out.add(v.strip())
    return {x for x in out if x}


def lookup(idx: dict, name: str) -> tuple[dict | None, str]:
    """只做变体精确匹配。套餐一律不参与单品匹配。"""
    if is_combo(name):
        return None, "combo"
    for v in sorted(name_variants(name), key=len, reverse=True):
        if v in idx:
            return idx[v], ("exact" if v == normalize(name) else "normalized")
    return None, "miss"


def get_detail(store: str, order_type: int, be_type: int, code: str,
               reservation: str | None) -> dict | None:
    os.makedirs(DETAILS, exist_ok=True)
    # 缓存键必须带上门店与订单类型：同一个 code（尤其套餐）在不同门店、不同
    # orderType 下的可选组与默认项并不保证一致，只用 code 做键会把 A 店的
    # 组合结构套到 B 店，热量的来源就错了。
    cache = os.path.join(DETAILS, f"{store}_{order_type}_{code}.json")
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    args = {"storeCode": store, "orderType": order_type, "beType": be_type, "code": code}
    if reservation:
        args["reservationDate"] = reservation
    try:
        d = mcp_call.call_tool("query-meal-detail", args, save=False)
    except SystemExit:
        return None
    time.sleep(0.05)
    json.dump(d, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return d


def resolve_meal(idx: dict, store: str, order_type: int, be_type: int,
                 code: str, name: str, reservation: str | None) -> dict:
    """返回 {kcal, source, parts, confidence, note}"""
    rec, how = lookup(idx, name)
    if rec:
        return {"kcal": rec["energyKcal"], "source": "official_single",
                "parts": [{"name": rec["productName"], "kcal": rec["energyKcal"], "via": how}],
                "confidence": "high" if how == "exact" else "medium", "note": None}

    # L2：拆解套餐
    detail = get_detail(store, order_type, be_type, code, reservation)
    if detail and detail.get("success") and detail.get("data", {}).get("rounds"):
        parts, total, missing = [], 0.0, []
        for rd in detail["data"]["rounds"]:
            # 默认组成必须按 quantity>0 取，不能按 isDefault 取。
            # 实测反例（订单 1030268980000808540604890948）：
            #   round「选择早餐组合饮料」里
            #     小杯鲜萃咖啡  isDefault=1  quantity=0
            #     鲜萃冰咖      isDefault=0  quantity=1
            #   真实下单给出的 comboItemList 是「鲜萃冰咖」——即 quantity>0 的那个。
            #   若按 isDefault 取，会把冰饮当成热咖啡：热量算错，且「不要冰」这类
            #   忌口/情境词会直接漏检。这是"安静的错"，比报错危险得多。
            picked = [c for c in rd.get("choices", []) if (c.get("quantity") or 0) > 0]
            if not picked:
                picked = [c for c in rd.get("choices", []) if c.get("isDefault") == 1]
            if not picked:
                picked = rd.get("choices", [])[:1]
            for ch in picked:
                # choice.quantity 可能缺省，用组上的 quantity 兜底；不能当 0，
                # 也不能无脑当 1（数量为 2 的组会少算一半）。
                qty = ch.get("quantity") or rd.get("quantity") or 1
                sub, subhow = lookup(idx, ch["name"])
                if sub:
                    parts.append({"name": sub["productName"], "qty": qty,
                                  "kcal": sub["energyKcal"], "via": subhow})
                    total += sub["energyKcal"] * qty
                else:
                    parts.append({"name": ch["name"], "qty": qty, "kcal": None, "via": "miss"})
                    missing.append(ch["name"])
        if parts and not missing:
            return {"kcal": round(total, 1), "source": "official_combo",
                    "parts": parts, "confidence": "high", "note": "按套餐默认组合拆解加总"}
        if parts and total > 0:
            # 关键：组合只拆出一部分时，**不能把部分和当热量**。
            # 例如「吉士蛋麦满分组合」只认出饮品 15 kcal，实际套餐 500+ kcal，
            # 若直接展示 15 kcal，会让系统误判"今天几乎没吃"。
            return {"kcal": None, "partial_kcal": round(total, 1),
                    "source": "incomplete_combo", "parts": parts, "confidence": "none",
                    "note": "组合内 " + "、".join(missing) + " 无官方数据，合计不可用"}
        return {"kcal": None, "source": "missing", "parts": parts,
                "confidence": "none", "note": "组合内单品均无官方数据"}

    return {"kcal": None, "source": "missing", "parts": [], "confidence": "none",
            "note": "营养库无对应条目"}


def main() -> None:
    store = sys.argv[1] if len(sys.argv) > 1 else "1450034"
    reservation = sys.argv[2] if len(sys.argv) > 2 else None
    idx = load_nutrition()

    args = {"storeCode": store, "orderType": 1, "beType": 1}
    if reservation:
        args["reservationDate"] = reservation
    menu = mcp_call.call_tool("query-meals", args)["data"]

    # 用菜单全量，不按分类名预筛。
    # 理由：query-meals 在早餐时点返回的是"当前可点单集合"，本身不区分餐段，
    # 里面既有早餐也有全天供应的甜品（麦旋风、圆筒、新地）。哪些能用是**决策层**
    # 的规则问题；解析层如果提前裁剪，算出来的就是"我自己挑过的子集覆盖率"，
    # 既不可比也会让两处脚本口径打架。
    codes = list((menu.get("meals") or {}).keys())

    out, stat = [], {}
    for code in codes:
        m = menu["meals"].get(code)
        if not m:
            continue
        r = resolve_meal(idx, store, 1, 1, code, m["name"], reservation)
        stat[r["source"]] = stat.get(r["source"], 0) + 1
        out.append({"code": code, "name": m["name"], "price": m["currentPrice"],
                    "originalPrice": m.get("originalPrice") or 0,
                    "is_combo": is_combo(m["name"]),
                    "parts_complete": r["source"] == "official_combo",
                    **r})
        print(f"{r['source']:<18} {r['kcal'] if r['kcal'] is not None else '-':>7} "
              f"{m['name']}")

    json.dump({"storeCode": store, "reservationDate": reservation, "items": out},
              open(os.path.join(DATA, "breakfast_menu.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    total = len(out)
    n_single = stat.get("official_single", 0)
    n_combo = stat.get("official_combo", 0)
    n_incomplete = stat.get("incomplete_combo", 0)
    n_missing = stat.get("missing", 0)
    n_known = n_single + n_combo
    lines = [
        "# 早餐餐品 → 热量 解析覆盖率",
        "",
        f"- 门店：`{store}`　预约时点：`{reservation or '即时'}`",
        f"- 菜单全量餐品：**{total}**",
        f"- 可用官方数据（单品直配）：**{n_single}**",
        f"- 可用官方数据（套餐拆解）：**{n_combo}**",
        f"- 官方数据不完整（组合含缺失单品，不可用）：**{n_incomplete}**",
        f"- 营养库无对应条目：**{n_missing}**",
        "",
        f"**官方数据覆盖率（单品直配 + 套餐拆解）：{n_known}/{total}"
        f" = {n_known / total:.0%}**",
        "",
        "## 完整可计算清单（kcal 与套餐组成均已验证）",
        "",
        "| 餐品 | 价格 | kcal | 来源 | 组成 |",
        "|---|---|---|---|---|",
    ]
    for it in sorted(out, key=lambda x: (x["kcal"] is None, x["kcal"] or 0)):
        if it["kcal"] is None:
            continue
        parts = " + ".join(f"{p['name']}×{p.get('qty',1)}({p['kcal']})" for p in it["parts"])
        lines.append(f"| {it['name']} | ¥{it['price']} | {it['kcal']:.0f} | {it['source']} | {parts} |")
    lines += ["", "## 无官方数据清单", "", "| 餐品 | 价格 | 原因 |", "|---|---|---|"]
    for it in out:
        if it["kcal"] is None:
            lines.append(f"| {it['name']} | ¥{it['price']} | {it['note']} |")

    open(os.path.join(EV, "resolve_report.md"), "w", encoding="utf-8").write("\n".join(lines))
    print("\n" + "\n".join(lines[3:16]))


if __name__ == "__main__":
    main()
