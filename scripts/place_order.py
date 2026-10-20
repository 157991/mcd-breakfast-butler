"""真实下单（不取消）：给用户一张可支付的待付款订单。

与 live_test.py 的区别：这里不调 cancel-order。订单会保持「待支付」，
用户扫码或在 App 内付款后即生效。

硬约束（实测）：create-order 只生成待支付订单，支付窗口 15 分钟，
超时订单自动作废。作废不会扣款，可以重下。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_call  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "evidence", "orders")

STORE = "1450386"
BE = "145038602"
ADDR = "1036586140206618117513916976"

# 最终选品：雪菜脆笋鸡肉粥套餐 = 雪菜脆笋鸡肉粥 + 脆香油条 + 脆薯饼
# 468 kcal / ¥15.50 / 不含任何冷饮
ITEM = {"productCode": "9900005470", "quantity": 1}
TAKEWAY = "take-in-store"
RD = sys.argv[1] if len(sys.argv) > 1 else "2026-10-21 07:30"
CHANNEL = sys.argv[2] if len(sys.argv) > 2 else "store"


def dump(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return p


def main():
    delivery = CHANNEL == "delivery"
    args = {
        "storeCode": STORE,
        "orderType": 2 if delivery else 1,
        "beType": 2 if delivery else 1,
        "reservationDate": RD,
        "items": [ITEM],
    }
    if delivery:
        args["beCode"] = BE
        args["addressId"] = ADDR
        args["remark"] = "放门口即可，谢谢"
    else:
        args["takeWayCode"] = TAKEWAY

    print("提交参数:")
    print(json.dumps(args, ensure_ascii=False, indent=1))

    p = mcp_call.call_tool("calculate-price", args)
    dump("00_price.json", p)
    pd = p.get("data") or {}
    print(f"\n核价: success={p.get('success')} 实付={pd.get('price')} 分 "
          f"({(pd.get('price') or 0)/100:.2f} 元)")
    if not p.get("success"):
        print("核价失败，终止:", p.get("message"))
        return

    print("\n下单中 …")
    o = mcp_call.call_tool("create-order", args)
    dump("01_create.json", o)
    print(f"create-order: success={o.get('success')} msg={o.get('message')} "
          f"traceId={o.get('traceId')}")
    if not o.get("success"):
        print(json.dumps(o.get("data"), ensure_ascii=False, indent=1)[:800])
        return

    d = o.get("data") or {}
    od = d.get("orderDetail") or {}
    order_id = od.get("orderId") or d.get("orderId")
    print(f"\n订单号: {order_id}")
    print(f"状态  : {od.get('orderStatus')} / {od.get('status')}")
    print(f"创建  : {od.get('createTime')}")
    print(f"支付截止: {od.get('expirePayTime')}")
    print(f"金额  : {float(od.get('totalAmount') or 0):.2f} 元")
    print(f"取餐  : {od.get('takeWay')}")
    for x in (od.get("orderProductList") or []):
        print(f"  · {x.get('productName')} ×{x.get('quantity')}")
        for c in (x.get("comboItemList") or []):
            print(f"      └ {c.get('itemName')} ×{c.get('itemQuantity')}")

    # 实测字段名是 payH5Url（不是 payUrl / paymentUrl）
    pay = d.get("payH5Url") or d.get("payUrl") or od.get("payH5Url")
    print(f"\n支付链接: {pay}")

    if order_id:
        q = mcp_call.call_tool("query-order", {"orderId": order_id})
        dump("02_query.json", q)
        qd = q.get("data") or {}
        print(f"复查状态: {qd.get('orderStatus')} / {qd.get('status')}")


if __name__ == "__main__":
    main()
