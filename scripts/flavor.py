"""风味引擎：决定「吃什么风味、去不去冰、配什么酱」。

三层职责，边界清晰：
  1. 抽取 —— 从商品名和 query-meal-detail 的选项里，读出这个商品**本来**有什么风味
  2. 选择 —— 按 flavor_rules.json 的规则，在真实可选项里挑（不创造不存在的选项）
  3. 组装 —— 生成 create-order 能直接吃的 items / roundList

为什么风味必须由词典+规则决定，而不是让模型"感觉这个配那个好吃"：
  酱料选择会真实写进订单，店员照着做。模型每次可能给不同答案，用户吃到的是随机的。
  风味偏好是可积累的（用户会说"这个酱太冲"），必须能落库、能复算。

已知限制（实测）：
  - 冰量组 code 恒为 200002，四档共用同一 code、靠 key 区分
  - calculate-price 接受 modification（已验，去冰不改变价格）
  - create-order 带 modification 的效果未能实测：触及「今日下单数已经上限」(600039)
"""

from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(ROOT, "data", "flavor_rules.json")

CAFFEINE_ORDER = {"none": 0, "low": 1, "mid": 2, "high": 3}

_rules: dict | None = None


def rules() -> dict:
    global _rules
    if _rules is None:
        with open(RULES_PATH, encoding="utf-8") as f:
            _rules = json.load(f)
    return _rules


# ---------------- 抽取 ----------------

def tags_of(name: str) -> set[str]:
    """从商品名推导风味标签。只认词典里的词，认不出就是空集。"""
    r = rules()
    out: set[str] = set()
    for tag, kws in r["dish_keywords"].items():
        if any(k in name for k in kws):
            out.add(tag)
    return out


def drink_profile(name: str) -> dict:
    """饮品的风味 + 咖啡因等级 + 温度形态。认不出时保守返回 mid。"""
    for pat, prof in rules()["drink_flavor"].items():
        if pat in name:
            return {"matched": pat, **prof}
    return {"matched": None, "flavor": [], "caffeine": "mid", "temp": "unknown"}


def caffeine_of(name: str) -> str:
    """认不出就返回 unknown，不要默认 mid。

    实测踩过：默认 mid 会让所有**套餐**都吃一条"含咖啡因"的误报 ——
    因为套餐名里当然不会有饮品名。宁可说不知道，也不要吓人。
    """
    p = drink_profile(name)
    if p["matched"] is None:
        return "unknown"
    return p["caffeine"]


def is_drink(name: str) -> bool:
    return drink_profile(name)["matched"] is not None


# ---------------- 解析 detail 里的真实可选项 ----------------

def parse_detail(detail: dict) -> dict:
    """把一个 query-meal-detail 的 data 拆成「能改什么」。

    返回：
      ice     : [{label, code, key}]  冰量四档（有就是能改）
      sauces  : [{name, code, qty}]   rounds 里名称含「酱」的那一组
      mods    : [{group, items:[{name, code, sel, unsel}]}] 配料增减组
    """
    d = detail.get("data") if "data" in detail else detail
    ice, sauces, mods = [], [], []

    # 1) 冰量：modification 里同一组同时含「标准」和「去冰」
    for it in ((d.get("modification") or {}).get("items") or []):
        vals = it.get("values") or []
        names = [v.get("name") for v in vals]
        if "标准" in names and "去冰" in names:
            for v in vals:
                if v.get("name") in ("标准", "去冰", "多冰", "少冰"):
                    ice.append({"label": v["name"], "code": v.get("code"),
                                "key": v.get("selectedKey")})
        elif len({v.get("code") for v in vals}) > 1 or not names:
            mods.append({
                "group": "配料",
                "items": [{"name": v.get("name"), "code": v.get("code"),
                           "sel": v.get("selectedQuantity"),
                           "sel_key": v.get("selectedKey"),
                           "unsel_key": v.get("unselectedKey"),
                           "price": v.get("price")} for v in vals],
            })

    # 2) 酱料：rounds 里名称含「酱」的组，或 choice 名本身是酱
    for rd in (d.get("rounds") or []):
        rname = (rd.get("name") or "")
        chs = rd.get("choices") or []
        if "酱" in rname or (chs and all("酱" in (c.get("name") or "") for c in chs)):
            for c in chs:
                sauces.append({"name": c.get("name"), "code": c.get("code"),
                               "qty": c.get("quantity"), "round_id": rd.get("id")})
    return {"ice": ice, "sauces": sauces, "mods": mods,
            "support_modify": bool(d.get("supportModify"))}


def sauce_flavor(sauce_name: str) -> dict:
    prof = rules()["sauce_profile"].get(sauce_name)
    if prof:
        return prof
    t = tags_of(sauce_name)
    return {"flavor": list(t), "pairs_with": [], "why": "按名称推导"}


# ---------------- 选择 ----------------

def pairing_prefs(dish_tags: set[str]) -> tuple[set[str], set[str]]:
    """把所有命中的搭配规则，汇总成「偏好风味」和「要避开的 flavor」。"""
    prefs: set[str] = set()
    avoids: set[str] = set()
    for rule in rules()["pairing_rules"]:
        cond = rule["when"]
        if "dish_has" in cond and not set(cond["dish_has"]).issubset(dish_tags):
            continue
        if "dish_lacks" in cond and (set(cond["dish_lacks"]) & dish_tags):
            continue
        then = rule["then"]
        prefs |= set(then.get("prefer_sauce_flavor") or [])
        avoids |= set(then.get("avoid_sauce_flavor") or [])
    return prefs, avoids


def choose_sauce(sauces: list[dict], dish_tags: set[str]) -> dict | None:
    """在真实存在的酱里挑一个。挑不到就返回 None，绝不编一个。

    打分必须真的走 pairing_rules，否则会出现"油炸鸡配芝士酱"这种反搭配 ——
    实测踩过：早期版本只按 pairs_with 加分，没扣 avoid，
    结果给炸鸡选了牧场酱（奶香），正是这条规则明确要避开的。
    """
    if not sauces:
        return None
    prefs, avoids = pairing_prefs(dish_tags)
    best, best_score = None, -99
    for s in sauces:
        prof = sauce_flavor(s["name"])
        flav = set(prof.get("flavor") or [])
        score = 0
        score += 3 * len(flav & prefs)       # 命中偏好风味
        score -= 3 * len(flav & avoids)      # 命中要避开的
        score -= 1 * len(flav & dish_tags)   # 与主菜同风味 = 重复
        score += 1 * len(set(prof.get("pairs_with") or []) & dish_tags)
        if best is None or score > best_score:
            best, best_score = s, score
    if best is None:
        return None
    prof = sauce_flavor(best["name"])
    return {"name": best["name"], "code": best["code"],
            "why": prof.get("why", ""), "score": best_score,
            "prefs": sorted(prefs), "avoids": sorted(avoids)}


def choose_ice(ice_options: list[dict], contexts: list[str],
               dish_tags: set[str]) -> dict | None:
    """温度规则优先级最高：宿醉/晚间的「去冰」压过一切风味考虑。"""
    if not ice_options:
        return None
    by_label = {o["label"]: o for o in ice_options}
    r = rules()

    for rule in r["temperature_rules"]:
        cond = rule["when"]
        # 三个条件维度都要校验，缺一个就会误触发。
        # 实测踩过：只校验了 context、没校验 dish_has，
        # 结果「辣味配少冰」这条规则被套到了一杯红茶上。
        if "context_tags" in cond and not (set(cond["context_tags"]) & set(contexts)):
            continue
        if "context_tags_not" in cond and (set(cond["context_tags_not"]) & set(contexts)):
            continue
        if "dish_has" in cond and not set(cond["dish_has"]).issubset(dish_tags):
            continue
        if "dish_lacks" in cond and (set(cond["dish_lacks"]) & dish_tags):
            continue
        want = rule["then"].get("temperature")
        if want is None:
            continue
        label = {"no_ice": "去冰", "less_ice": "少冰", "more_ice": "多冰",
                 "standard": "标准"}.get(want)
        if label and label in by_label:
            return {**by_label[label], "why": rule["then"]["why"], "rule": rule["id"]}
    return by_label.get("标准")


def choose_drink(choices: list[dict], contexts: list[str]) -> dict | None:
    """在套餐可选饮料里挑。晚间排除咖啡因中高，宿醉排除冰镇形态。"""
    if not choices:
        return None
    avoid_caffeine = bool({"night", "sleep", "alcohol"} & set(contexts))
    scored = []
    for c in choices:
        nm = c.get("name") or ""
        prof = drink_profile(nm)
        cf = CAFFEINE_ORDER.get(prof["caffeine"], 2)
        if avoid_caffeine and cf >= 2:
            continue
        score = 0
        if prof["caffeine"] == "none":
            score += 3
        if "ice" in str(prof.get("temp", "")) and "alcohol" in contexts:
            score -= 2
        if any(k in nm for k in ("无糖", "去冰")):
            score += 1
        scored.append((score, cf, nm, c))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    s = scored[0]
    return {"name": s[2], "code": s[3].get("code"),
            "caffeine": {0: "none", 1: "low", 2: "mid", 3: "high"}.get(s[1], "unknown"),
            "why": "晚间避开咖啡因" if avoid_caffeine else "按风味与清爽度"}


# ---------------- 组装 ----------------

def build_modification(ice: dict | None, keep: list[str] | None = None,
                       drop: list[str] | None = None,
                       mods: list[dict] | None = None) -> dict | None:
    """生成 create-order 的 modification。规则来自 schema 原文：
       选中的用 selectedKey，未选中的用 unselectedKey，且该组所有项都要传。

    默认**不传**配料组。实测教训：把五个配料全传 selectedKey="0-1"
    等于把所有东西按默认再确认一遍，既没意义、又给了接口一次无谓的出错机会。
    只有用户明确说"不要芥末"时，才传那一组。
    """
    values: list[dict] = []
    if ice:
        values.append({"code": ice["code"], "key": ice["key"], "quantity": 1})

    for grp in (mods or []):
        targets = [it for it in grp["items"]
                   if (drop and it["name"] in drop) or (keep and it["name"] in keep)]
        if not targets:
            continue
        for it in grp["items"]:
            if it.get("unsel_key") is None:
                continue
            use = it["unsel_key"] if (drop and it["name"] in drop) else it["sel_key"]
            values.append({"code": it["code"], "key": use, "quantity": 1})
    return {"values": values} if values else None


def warnings(name: str, contexts: list[str]) -> list[str]:
    """那些"不该悄悄放过"的事。风味可以商量，咖啡因不行。

    用户说过"晚上不喝咖啡睡不着"。咖啡因不只藏在咖啡里：
    红茶 mid、可乐 mid、鲜萃/美式/奶铁 high。
    只按名字里有没有"咖啡"两个字来判，会漏掉一整批。
    """
    out = []
    cf = caffeine_of(name)
    if {"night", "sleep"} & set(contexts) and cf in ("mid", "high"):
        out.append(f"「{name}」咖啡因等级 {cf}，晚间会拖入睡时间；"
                   f"改选雪碧 / 怡泉+C / 美汁源 / 玉米杯可避开")
    if "alcohol" in contexts and "ice" in str(drink_profile(name).get("temp", "")):
        out.append(f"「{name}」本身是冰镇形态，宿醉场景建议去冰或换热饮")
    return out


def explain(dish_name: str, ice: dict | None, sauce: dict | None,
            drink: dict | None) -> list[str]:
    lines = []
    t = tags_of(dish_name)
    if t:
        zh = rules()["flavor_axes"]
        lines.append(f"主风味：{'、'.join(zh.get(x, x) for x in sorted(t))}")
    if sauce:
        lines.append(f"配酱：{sauce['name']} —— {sauce.get('why') or '按搭配规则'}")
    if ice:
        lines.append(f"冰量：{ice['label']} —— {ice.get('why') or '按当前情境'}")
    if drink:
        lines.append(f"饮品：{drink['name']}（咖啡因 {drink.get('caffeine')}）"
                     f" —— {drink.get('why') or ''}")
    return lines


def configure(store_code: str, product_code: str, contexts: list[str],
              dish_name: str = "", drop: list[str] | None = None,
              keep: list[str] | None = None) -> dict:
    """对一个商品算出完整风味配置。会真的去查 detail，不猜。"""
    import mcp_call  # 延迟导入，便于单测

    detail = mcp_call.call_tool("query-meal-detail", {
        "storeCode": store_code, "orderType": 1, "beType": 1, "code": product_code})
    if not detail.get("success"):
        return {"ok": False, "reason": detail.get("message"), "product_code": product_code}

    d = detail.get("data") or {}
    name = dish_name or d.get("name") or ""
    tags = tags_of(name) | tags_of(d.get("name") or "")
    opts = parse_detail(d)

    ice = choose_ice(opts["ice"], contexts, tags)
    sauce = choose_sauce(opts["sauces"], tags)
    drink = choose_drink(
        [c for rd in (d.get("rounds") or []) if "饮料" in (rd.get("name") or "")
         for c in (rd.get("choices") or [])], contexts)

    return {
        "ok": True,
        "product_code": product_code,
        "name": name,
        "tags": sorted(tags),
        "caffeine": caffeine_of(name),
        "ice": ice,
        "sauce": sauce,
        "drink": drink,
        "modification": build_modification(ice, keep=keep, drop=drop, mods=opts["mods"]),
        "explain": explain(name, ice, sauce, drink),
        "dropped": drop or [],
        "warnings": warnings(name, contexts),
        "support_modify": opts["support_modify"],
        "raw_options": opts,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="风味引擎：风味 / 冰量 / 酱料")
    ap.add_argument("code", help="商品编码")
    ap.add_argument("--store", default="1450386")
    ap.add_argument("--ctx", default="", help="逗号分隔的情境，如 alcohol,night")
    ap.add_argument("--drop", default="", help="要去掉的配料，逗号分隔，如 芥末酱,洋葱粒")
    a = ap.parse_args()

    ctxs = [x.strip() for x in a.ctx.split(",") if x.strip()]
    drops = [x.strip() for x in a.drop.split(",") if x.strip()]
    res = configure(a.store, a.code, ctxs, drop=drops)
    if not res["ok"]:
        print("失败:", res["reason"])
        raise SystemExit(1)
    print(f"商品: {res['name']}  [{res['product_code']}]")
    print(f"标签: {res['tags']}  咖啡因: {res['caffeine']}")
    print("可选项:")
    print("  冰量:", [(x['label'], x['key']) for x in res['raw_options']['ice']] or "无")
    print("  酱料:", [x['name'] for x in res['raw_options']['sauces']] or "无")
    print("  配料组:", [[i['name'] for i in g['items']] for g in res['raw_options']['mods']] or "无")
    print("\n配置建议:")
    for line in res["explain"]:
        print("  ·", line)
    if res.get("warnings"):
        print("\n⚠ 警告:")
        for w in res["warnings"]:
            print("  ·", w)
    print("\nmodification:", json.dumps(res["modification"], ensure_ascii=False))
