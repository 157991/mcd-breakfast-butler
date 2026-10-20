"""今晚（夜市时段）点餐的真实价格与组合可选内容。

只读：不下单。夜市时段预约单也要当场 15 分钟内付款，
所以"今晚吃"必须今晚下单，这里先给出价格。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_call  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "evidence")
STORE = "1450386"
RD = sys.argv[1] if len(sys.argv) > 1 else "2026-10-20 18:30"

ITEMS = [
    ("9900015568", "精选超值随心配"),
    ("9900013304", "人气经典随心配"),
    ("9900011126", "酥脆无双套餐"),
    ("9900011123", "“辣”么快乐套餐"),
    ("9900016076", "“我就喜欢”鸡薯双全盒"),
    ("9900016077", "蘸酱炸鸡"),
    ("9900016073", "龙焰鸡腿堡三件套"),
    ("507411", "麦咖啡™美式"),
    ("520689", "鲜萃咖啡"),
    ("510479", "燕麦奶铁"),
]


def price(code):
    return mcp_call.call_tool("calculate-price", {
        "storeCode": STORE, "orderType": 1, "beType": 1,
        "reservationDate": RD, "items": [{"productCode": code, "quantity": 1}]})


def detail(code):
    return mcp_call.call_tool("query-meal-detail", {
        "storeCode": STORE, "orderType": 1, "beType": 1, "code": code})


def choices(d):
    """返回各轮可选内容，用于判断组合能换成什么。"""
    out = []
    data = d.get("data") or {}
    for rd in (data.get("rounds") or []):
        opts = []
        for ch in (rd.get("choices") or []):
            opts.append({
                "name": ch.get("name"),
                "qty": ch.get("quantity"),
                "default": ch.get("isDefault"),
                "diff": ch.get("diffPrice"),
            })
        out.append({"round": (rd.get("name") or "").strip(),
                    "qty": rd.get("quantity"), "options": opts})
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    print(f"门店 {STORE} | 今晚 {RD} | 到店自取\n")
    rows = []
    for code, name in ITEMS:
        p = price(code)
        d = p.get("data") or {}
        cents = d.get("price")
        json.dump(p, open(os.path.join(OUT, f"night_{code}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        r = {"code": code, "name": name, "cents": cents, "ok": p.get("success")}
        if cents is not None:
            dd = detail(code)
            json.dump(dd, open(os.path.join(OUT, f"nightdt_{code}.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            r["rounds"] = choices(dd)
        rows.append(r)
        fs = f"¥{cents/100:.2f}" if cents is not None else f"失败({p.get('message')})"
        print(f"{name:<22}{fs:>12}")
    json.dump(rows, open(os.path.join(OUT, "night_candidates.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    for r in rows:
        if not r.get("rounds"):
            continue
        print(f"\n■ {r['name']}  ¥{(r['cents'] or 0)/100:.2f}")
        for rd in r["rounds"]:
            opts = rd["options"]
            names = [f"{o['name']}" + ("*" if o["default"] == 1 else "")
                     for o in opts if o["qty"]]
            print(f"   轮次[{rd['round'] or '-'}] 数量{rd['qty']} → {len(opts)} 个可选")
            print(f"     实际选中: {' / '.join(names) if names else '-'}")
            if len(opts) > 6:
                shown = [o["name"] for o in opts[:8]]
                print(f"     可选示例: {'、'.join(shown)} …共{len(opts)}项")


if __name__ == "__main__":
    main()
