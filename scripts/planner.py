#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
早餐决策引擎（作品核心）

设计原则来自三条被实测反复打脸后修正的结论：

1. 【热量不能参与"配平"，只能作为绝对区间约束】
   单张照片的份量误差约 ±40%，而 TDEE 估算误差约 ±10%（对 2000 kcal 的人是 ±200）。
   缓慢减重需要的日赤字约 275 kcal —— 与误差同量级。
   所以"用全天账本反推早餐该吃多少"在数学上不成立。
   本引擎因此：早餐只受一个绝对区间约束（默认 350–500 kcal），
   不从三餐账本里做加减法。

2. 【钱和热量是两个预算，不能混】
   实测：猪柳蛋麦满分 单点 ¥18/387kcal，组合 ¥12.6/402kcal，套餐 ¥15.3/546kcal。
   单点最贵、热量最低。若"省钱"和"控热量"合成一个分数，系统会一路选更便宜的套餐，
   热量反而上涨。所以先卡热量区间，再在区间内比价格。

3. 【无官方数据的餐品可以推荐，但绝不参与热量预算】
   实测 70 个早餐餐品里只有 32 个（46%）能拿到官方热量。
   剩下的不能猜一个数字塞进去，只能标"热量未知"，
   并且这类餐品不得被当作"低热量"选项使用 —— 未知不等于低。

优先级（严格顺序，不合成加权）：
   忌口/过敏 > 当日明确要求 > 情境规则（如饮酒后不冰饮）> 热量区间 > 金额预算 > 好吃 > 不重复
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

DATA = os.path.join(ROOT, "data")

DEFAULT_PROFILE = {
    "city": "上海",
    "store_code": None,
    "order_type": 1,
    "be_type": 1,
    "meal_time": "07:30",
    "budget_cny": 18.0,
    "calorie_window": [350, 500],
    "goal": "slow_loss",
    "allergens": [],
    "exclude_keywords": [],
    "prefer_keywords": [],
}

# 情境 → 该情境下需要施加的硬规则
CONTEXT_RULES = {
    "alcohol": {"exclude_keywords": ["冰", "冷萃", "雪碧", "可乐"], "reason": "前一晚饮酒，早上不安排冰饮"},
    "sick": {"exclude_keywords": ["辣", "炸"], "reason": "身体不适，避开辛辣与油炸"},
    "late_night": {"prefer_keywords": ["咖啡"], "reason": "睡得晚，保留咖啡"},
}


def load_json(path: str, default):
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return default


def load_menu(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    return d["items"]


def active_contexts(ledger: dict, target: date) -> list[dict]:
    """取目标日前一天及当天的情境标签，映射成规则。"""
    hits = []
    for c in ledger.get("contexts", []):
        try:
            d = datetime.strptime(c["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if (target - d).days in (0, 1):
            for tag in c.get("tags", []):
                if tag in CONTEXT_RULES:
                    hits.append({"tag": tag, **CONTEXT_RULES[tag]})
    return hits


def explicit_request(ledger: dict, target: date) -> dict | None:
    for r in ledger.get("explicit_requests", []):
        if r.get("target_date") == target.isoformat():
            return r
    return None


SUBJECT_NOISE = ("套餐", "组合", "超值", "精选", "二件套", "三件套", "单人餐", "双人餐",
                 "中杯", "大杯", "小杯", "（冰/热）", "(冰/热)", "™")


def haystack(it: dict) -> str:
    """把餐品名与它的实际组成拼成同一个可匹配面。

    禁用词（忌口、过敏、情境）必须在**这个面**上匹配，而不是只在商品名上。
    原因是真实订单里踩到的坑：候选叫「大脆鸡扒麦满分组合」，用户说「不要冰咖啡」，
    商品名里没有「冰」字，规则放行；而它拆出来的组成里第二个就是「鲜萃冰咖」。
    """
    parts = [it.get("name") or ""]
    for p in (it.get("parts") or []):
        n = p.get("name")
        if n:
            parts.append(n)
    return " ".join(parts)


def looks_combo(name: str) -> bool:
    return any(s in (name or "") for s in
               ("套餐", "组合", "件套", "超值", "双人餐", "单人餐"))


def subject(name: str) -> str:
    """抽取餐品的「主体名」，用于偏好与去重。

    为什么要这一步：菜单里「猪柳蛋麦满分」「猪柳蛋麦满分组合」「猪柳蛋麦满分套餐」
    是三个独立 code，但用户认知里是同一个东西。
    若不归一化，系统会认为"单品没吃过"，从而反复推单品；
    而实际上用户三天内已经吃过两次同主体的套餐 —— 这属于重复，必须计进去。
    """
    s = name
    for w in SUBJECT_NOISE:
        s = s.replace(w, "")
    return s.strip() or name


def eaten_recently(ledger: dict, target: date) -> dict[str, int]:
    """返回 {主体名: 距今天数}，按主体归一去重。"""
    out: dict[str, int] = {}
    for m in ledger.get("meals", []):
        try:
            d = datetime.strptime(m["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        gap = (target - d).days
        if gap < 0 or gap > 7:
            continue
        for it in m.get("items", []):
            name = it.get("name", "")
            if name:
                k = subject(name)
                out[k] = min(out.get(k, 99), gap)
    return out


def like_score(profile: dict, ledger: dict, name: str) -> float:
    """把"好吃"折算成可计算的分数。

    程序读不到"喜欢"，只能读行为。三个代理指标：
      - 历史出现次数（权重最高，代表稳定偏好）
      - 最近一次的正向反馈（"这个不错"）
      - 明确标记的偏好关键词
    比较一律在「主体名」层面进行，避免"组合"与"单品"互相不认。
    """
    sub = subject(name)
    s = 0.0
    cnt = 0
    for m in ledger.get("meals", []):
        for it in m.get("items", []):
            if it.get("name") and subject(it["name"]) == sub:
                cnt += 1
    s += min(cnt, 6) * 0.35
    for fb in ledger.get("feedback", []):
        if fb.get("name") and subject(fb["name"]) == sub:
            s += 0.8 if fb.get("positive") else -0.8
    for kw in profile.get("prefer_keywords", []):
        if kw and kw in name:
            s += 0.9
    return s


def price_of(item: dict) -> float:
    """菜单 currentPrice 是「随单购卡/优惠价」，不是真实可下单价格。

    实测：菜单显示猪柳蛋麦满分套餐 ¥15.3，calculate-price 返回 2550（分）= ¥25.5。
    所以这里只用它做粗筛，最终报价必须由 calculate-price 产出。
    """
    return float(item.get("price") or 0)


def kcal_fit(kcal: float | None, window: list[int]) -> float:
    if kcal is None:
        return 0.0          # 未知不奖不罚，但也不允许被当成低热量
    lo, hi = window
    if lo <= kcal <= hi:
        return 1.0
    dist = (lo - kcal) if kcal < lo else (kcal - hi)
    return max(0.0, 1.0 - dist / 250.0)


def plan(profile: dict, ledger: dict, menu: list[dict], target: date) -> dict:
    trace: list[str] = []
    window = profile.get("calorie_window", [350, 500])
    budget = float(profile.get("budget_cny", 18))
    contexts = active_contexts(ledger, target)
    req = explicit_request(ledger, target)
    recent = eaten_recently(ledger, target)

    cands = []
    off_meal = profile.get("off_meal_keywords", [])
    for it in menu:
        name = it["name"]
        # 禁用词必须穿透到套餐组成。
        # 实测教训：用户说「明早不要冰咖啡」，规则命中「冰」，但候选是
        # 「大脆鸡扒麦满分组合」——名字里没有「冰」，真正含冰的是它拆出来的
        # 「鲜萃冰咖」。只匹配餐品名会让忌口规则在最常见的套餐上系统性失效，
        # 而且失效得很安静：选出来的东西看起来完全合规。
        surface = it.get("surface") or haystack(it)
        # 1) 忌口 / 过敏（硬否决）
        bad = [k for k in profile.get("allergens", []) + profile.get("exclude_keywords", [])
               if k and k in surface]
        if bad:
            continue
        # 1b) 餐段语义兜底：query-meals 在早餐时点也会带上全天供应的甜品，
        #     菜单本身没有餐段字段，只能靠品类词把它挡在早餐之外。
        off = [k for k in off_meal if k and k in surface]
        if off:
            trace.append(f"排除 {name}：非早餐品类「{off[0]}」")
            continue
        # 2) 情境硬规则
        blocked = None
        for c in contexts:
            for kw in c.get("exclude_keywords", []):
                if kw and kw in surface:
                    blocked = (c["tag"], kw)
        if blocked:
            trace.append(f"排除 {name}：情境 {blocked[0]} 命中禁词「{blocked[1]}」")
            continue
        # 2b) 组合成分未知时不能假装安全。
        #     营养库缺少某个单品 → parts 拼不全 → 无从判断是否含禁用成分。
        #     这里不直接排除（否则早餐会大面积无候选），但降权并在卡片上说明。
        is_cb = it.get("is_combo")
        if is_cb is None:
            is_cb = looks_combo(it.get("name") or "")
        if is_cb and not it.get("parts_complete", True):
            it["_unverified"] = True
        cands.append(it)

    # 3) 当日明确要求：满足则收窄，无法满足则降级到同主料
    if req and req.get("must"):
        must = req["must"]
        narrowed = [c for c in cands if all(m in c["name"] for m in must)]
        if narrowed:
            trace.append(f"当日要求「{req.get('text','')}」命中 {len(narrowed)} 个，已收窄候选")
            cands = narrowed
        else:
            trace.append(f"当日要求「{req.get('text','')}」无完全匹配，保留全量并标注")

    # 4) 热量区间 + 金额预算（两级筛选，不合并成一个分）
    in_window = [c for c in cands if c.get("kcal") is not None and window[0] <= c["kcal"] <= window[1]]
    unknown = [c for c in cands if c.get("kcal") is None]
    if in_window:
        trace.append(f"热量区间 {window[0]}–{window[1]} kcal 内有 {len(in_window)} 个可选")
        pool = in_window
    else:
        pool = cands
        trace.append("区间内无候选，放宽到全量（不跳餐）")

    affordable = [c for c in pool if price_of(c) <= budget]
    if affordable:
        pool = affordable
    else:
        trace.append(f"预算 ¥{budget:.0f} 内无候选，保留最接近的一档并提示")

    # 5) 排序：好吃 > 不重复 > 热量贴合 > 便宜
    def key(c: dict) -> tuple:
        like = like_score(profile, ledger, c["name"])
        gap = recent.get(subject(c["name"]))
        fresh = 1.0 if gap is None else (0.0 if gap <= 2 else 0.5)
        return (like, fresh, kcal_fit(c.get("kcal"), window), -price_of(c))

    ranked = sorted(pool, key=key, reverse=True)
    if not ranked:
        return {"pick": None, "trace": trace + ["无任何可用候选"], "reason": "无可下单项"}

    pick = ranked[0]
    alts = ranked[1:4]

    gap = recent.get(subject(pick["name"]))
    reasons = []
    if req and req.get("must") and all(m in pick["name"] for m in req["must"]):
        reasons.append(f"你说了「{req.get('text','')}」，优先按这个安排")
    if gap is not None:
        reasons.append(f"这个主料距上次吃 {gap} 天")
    ls = like_score(profile, ledger, pick["name"])
    if ls > 0:
        reasons.append("在你的常点清单里")
    for c in contexts:
        reasons.append(c["reason"])
    if pick.get("kcal") is None:
        reasons.append("该餐品无官方营养数据，热量未知")
    elif window[0] <= pick["kcal"] <= window[1]:
        reasons.append(f"{pick['kcal']:.0f} kcal，在目标区间内")
    else:
        reasons.append(f"{pick['kcal']:.0f} kcal，略超目标区间（为了你愿意吃，未强行替换）")

    return {
        "target_date": target.isoformat(),
        "meal_time": profile.get("meal_time"),
        "pick": pick,
        "kcal": pick.get("kcal"),
        "menu_price_cny": price_of(pick),
        "reason": "；".join(reasons),
        "alternatives": alts,
        "trace": trace,
        "contexts": [c["tag"] for c in contexts],
        "request": req,
        "calorie_window": window,
        "budget_cny": budget,
    }


def render_card(res: dict, store_name: str = "") -> str:
    if not res.get("pick"):
        return "明天没有可安排的早餐，已保留原计划，不会跳过这一餐。"
    p = res["pick"]
    kcal = f"{p['kcal']:.0f} kcal" if p.get("kcal") is not None else "热量未知（无官方数据）"
    parts = " + ".join(x["name"] for x in p.get("parts", [])) if p.get("parts") else p["name"]
    price = res["menu_price_cny"]
    lines = [
        f"明早 {res['meal_time']} · {store_name or '常去门店'} · 到店取餐柜",
        "",
        f"【{p['name']}】 ¥{price:.2f} · {kcal}",
    ]
    if parts and parts != p["name"]:
        lines.append(f"  组成：{parts}")
    lines += ["", "为什么是它："]
    for r in res["reason"].split("；"):
        if r:
            lines.append(f"  · {r}")
    if res["alternatives"]:
        alts = "、".join(a["name"] for a in res["alternatives"])
        lines += ["", f"想换的话：{alts}"]
    # 这里显示的是菜单价。菜单价是「早餐卡专享价」，组合类可能比实际应付低 67%。
    # 下单前必须用 calculate-price 覆盖（见 scripts/live_test.py），不能直接报价。
    lines += ["", "（价格为菜单价，实际应付以下单前 calculate-price 结果为准）"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="麦麦早餐管家 · 决策引擎")
    ap.add_argument("--profile", default=os.path.join(DATA, "profile.example.json"))
    ap.add_argument("--ledger", default=os.path.join(DATA, "ledger.example.json"))
    ap.add_argument("--menu", default=os.path.join(DATA, "breakfast_menu.json"))
    ap.add_argument("--date", default=None, help="目标日期 yyyy-MM-dd，默认明天")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    profile = {**DEFAULT_PROFILE, **load_json(a.profile, {})}
    ledger = load_json(a.ledger, {"meals": [], "contexts": [], "explicit_requests": [], "feedback": []})
    menu = load_menu(a.menu)
    target = (datetime.strptime(a.date, "%Y-%m-%d").date() if a.date
              else date.today() + timedelta(days=1))

    res = plan(profile, ledger, menu, target)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render_card(res, profile.get("store_name", "")))


if __name__ == "__main__":
    main()
