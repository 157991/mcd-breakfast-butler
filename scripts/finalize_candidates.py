"""对候选商品做真实核价 + 组成拆解，用于最终定案。

菜单价是早餐卡优惠价，不可信；这里一律用 calculate-price 的权威金额。
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
RD = sys.argv[1] if len(sys.argv) > 1 else "2026-10-21 07:30"

CODES = [
    ("9900011872", "芝芝火腿扒堡组合"),
    ("9900011873", "芝芝火腿扒堡套餐"),
    ("521315", "芝芝火腿扒堡"),
    ("9900013221", "一早现磨超值二件套"),
    ("9900016524", "厚松饼猪柳蛋堡三件套"),
    ("9900003458", "猪柳蛋麦满分套餐"),
    ("9900003432", "吉士蛋麦满分组合"),
    ("9900003451", "大脆鸡扒麦满分套餐"),
    ("5889", "大脆鸡扒麦满分"),
    ("5022", "猪柳蛋麦满分"),
    ("1254", "火腿扒麦满分"),
    ("4825", "脆薯饼"),
]


def price(code, order_type=1, be_code=None):
    args = {"storeCode": STORE, "orderType": order_type,
            "beType": 1 if order_type == 1 else 2, "reservationDate": RD,
            "items": [{"productCode": code, "quantity": 1}]}
    if be_code:
        args["beCode"] = be_code
    return mcp_call.call_tool("calculate-price", args)


def detail(code, order_type=1, be_code=None):
    args = {"storeCode": STORE, "orderType": order_type,
            "beType": 1 if order_type == 1 else 2, "code": code}
    if be_code:
        args["beCode"] = be_code
    return mcp_call.call_tool("query-meal-detail", args)


def parts_of(d):
    """按 quantity>0 取实际组成（实测这才是下单真正给的）。"""
    out = []
    data = d.get("data") or {}
    for rd in (data.get("rounds") or []):
        for ch in (rd.get("choices") or []):
            q = ch.get("quantity") or 0
            if q > 0:
                out.append(f"{ch.get('name')}×{q}")
    return out


def main():
    rows = []
    print(f"门店 {STORE} | 时点 {RD} | 到店自取\n")
    print(f"{'商品':<24}{'菜单示价':>8}{'实付':>9}{'差':>8}   组成")
    print("-" * 100)
    for code, name in CODES:
        p = price(code)
        d = p.get("data") or {}
        cents = d.get("price")
        parts = []
        if cents is not None:
            dd = detail(code)
            parts = parts_of(dd)
            json.dump(dd, open(os.path.join(OUT, f"dt_{code}.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        json.dump(p, open(os.path.join(OUT, f"pp_{code}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        rows.append({"code": code, "name": name, "cents": cents, "parts": parts,
                     "ok": p.get("success"), "msg": p.get("message")})
        fs = f"¥{cents/100:.2f}" if cents is not None else "失败"
        print(f"{name:<24}{'':>8}{fs:>9}{'':>8}   {' + '.join(parts) if parts else '-'}")
    json.dump(rows, open(os.path.join(OUT, "candidates_final.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
