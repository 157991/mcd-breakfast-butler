"""麦麦早餐管家的本地记忆层（SQLite）。

设计原则：只存**确定性事实**，不存模型的自由解释。
  - 存：档案、三餐记录、情境、反馈、订单、券快照、菜单快照、决策计划
  - 不存：模型对照片的自由描述、模型的推理过程

原因很直接：SQLite 里的东西要能被程序按条件查询、去重、复算；
模型的描述无法查询、会漂移、每次调用可能不一样。两者混在一张表里，
数据就会同时失去"可查"和"可信"。

路径：~/.mcd-breakfast/butler.db（可用 MCD_BUTLER_HOME 覆盖）
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    goal          TEXT,              -- cut_slow / maintain / muscle
    budget_cny    REAL,              -- 单餐金额上限（元）
    kcal_low      INTEGER,           -- 热量区间下限
    kcal_high     INTEGER,           -- 热量区间上限
    slots_json    TEXT,              -- {"breakfast":"07:30","dinner":"18:30"}
    store_code    TEXT,
    store_name    TEXT,
    be_code       TEXT,
    address_id    TEXT,
    likes_json    TEXT,              -- ["麦满分","粥","油条"]
    avoids_json   TEXT,              -- 忌口/过敏，硬否决
    offmeal_json  TEXT,              -- 非本餐段品类词，如 ["麦旋风","圆顶","新地"]
    body_json     TEXT,              -- {"height_cm":175,"weight_kg":72,"activity":"light"}
    updated_at    TEXT
);

-- 三餐记录：一条记录 = 一次进食，照片只是它的来源之一
CREATE TABLE IF NOT EXISTS meal_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    eaten_on   TEXT NOT NULL,        -- YYYY-MM-DD
    slot       TEXT NOT NULL,        -- breakfast / lunch / dinner / snack
    items_json TEXT NOT NULL,        -- [{"name":"炒饭","kcal":500,"portion":"1份"}]
    kcal_low   INTEGER,              -- 估算区间下界（照片估算时必填）
    kcal_high  INTEGER,              -- 估算区间上界
    source     TEXT NOT NULL,        -- photo / user_text / order / assumed
    confidence TEXT NOT NULL,        -- high / mid / low / assumed
    photo      TEXT,                 -- 本地相对路径，可为空
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meal_date ON meal_log(eaten_on, slot);

-- 临时情境：带自然失效日期，过期自动不参与决策
CREATE TABLE IF NOT EXISTS context (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ctx_on       TEXT NOT NULL,      -- 发生日
    applies_to   TEXT NOT NULL,      -- 影响哪一顿：YYYY-MM-DD
    tags_json    TEXT NOT NULL,      -- ["alcohol","late_night"]
    exclude_json TEXT,               -- ["冰","冷萃"]，本情境下禁用的词
    raw_text     TEXT,               -- 用户原话，用于回显核对
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ctx_apply ON context(applies_to);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    eaten_on   TEXT NOT NULL,
    item_name  TEXT NOT NULL,
    positive   INTEGER NOT NULL,     -- 1 好吃 / 0 不好吃 / 2 量不够
    note       TEXT,
    created_at TEXT NOT NULL
);

-- 订单：唯一能和"真付了钱"对齐的事实来源
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT UNIQUE NOT NULL,
    store_code     TEXT,
    order_type     INTEGER,          -- 1 到店 / 2 外送
    items_json     TEXT,             -- 请求的商品
    combo_json     TEXT,             -- 订单返回的 comboItemList（真实组成）
    coupon_json    TEXT,             -- 订单返回的 couponList（真实用券）
    total_cents    INTEGER,          -- 原价
    discount_cents INTEGER,          -- 抵扣
    real_cents     INTEGER,          -- 实付
    status         TEXT,             -- 订单已取消 / 待支付 / ...
    reserve_at     TEXT,             -- reservationDate
    pay_url        TEXT,
    pay_expire_at  TEXT,             -- expirePayTime，15 分钟窗口
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_reserve ON orders(reserve_at);

-- 券快照：query-store-coupons 按门店+时段返回，会随时间变
CREATE TABLE IF NOT EXISTS coupon_snap (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    coupon_id     TEXT NOT NULL,
    coupon_code   TEXT,
    title         TEXT,
    product_codes TEXT,              -- 绑定商品码，逗号分隔
    valid_from    TEXT,
    valid_to      TEXT,
    store_code    TEXT,
    order_type    INTEGER,
    captured_at   TEXT NOT NULL,
    UNIQUE(coupon_id, store_code, order_type)
);

-- 菜单快照：用于价格漂移对比与"是否还是同一份菜单"
CREATE TABLE IF NOT EXISTS menu_snap (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    store_code   TEXT NOT NULL,
    order_type   INTEGER NOT NULL,
    slot_at      TEXT NOT NULL,
    product_code TEXT NOT NULL,
    name         TEXT,
    menu_price   REAL,               -- 菜单页价格（不可信，仅参考）
    real_cents   INTEGER,            -- calculate-price 实付（可信）
    kcal         REAL,
    kcal_source  TEXT,               -- official_single / official_combo / missing
    parts_json   TEXT,
    captured_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_menu_slot ON menu_snap(store_code, order_type, slot_at);

-- 决策计划：前一晚算好，早上由定时任务执行
CREATE TABLE IF NOT EXISTS plan (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_at    TEXT NOT NULL,      -- 计划的用餐时刻 YYYY-MM-DD HH:MM
    store_code   TEXT NOT NULL,
    order_type   INTEGER NOT NULL,
    payload_json TEXT NOT NULL,      -- create-order 的完整参数，可直接执行
    reason_json  TEXT,               -- 为什么这么选（含排除了什么）
    discount_json TEXT,              -- 计划使用的券
    status       TEXT NOT NULL,      -- planned / executed / expired / superseded
    order_id     TEXT,               -- 执行后回填
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_target ON plan(target_at, status);
"""


def home() -> str:
    h = os.environ.get("MCD_BUTLER_HOME") or os.path.join(
        os.path.expanduser("~"), ".mcd-breakfast")
    os.makedirs(h, exist_ok=True)
    return h


def db_path() -> str:
    return os.path.join(home(), "butler.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- profile ----------------

def get_profile() -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("slots_json", "likes_json", "avoids_json", "offmeal_json", "body_json"):
            if d.get(k):
                d[k.replace("_json", "")] = json.loads(d[k])
        return d


def save_profile(**kw) -> None:
    fields = {
        "goal": kw.get("goal"),
        "budget_cny": kw.get("budget_cny"),
        "kcal_low": kw.get("kcal_low"),
        "kcal_high": kw.get("kcal_high"),
        "slots_json": json.dumps(kw.get("slots") or {}, ensure_ascii=False),
        "store_code": kw.get("store_code"),
        "store_name": kw.get("store_name"),
        "be_code": kw.get("be_code"),
        "address_id": kw.get("address_id"),
        "likes_json": json.dumps(kw.get("likes") or [], ensure_ascii=False),
        "avoids_json": json.dumps(kw.get("avoids") or [], ensure_ascii=False),
        "offmeal_json": json.dumps(kw.get("offmeal") or [], ensure_ascii=False),
        "body_json": json.dumps(kw.get("body") or {}, ensure_ascii=False),
        "updated_at": _now(),
    }
    with connect() as c:
        cols = ",".join(fields)
        ph = ",".join("?" * len(fields))
        upd = ",".join(f"{k}=excluded.{k}" for k in fields)
        c.execute(
            f"INSERT INTO profile (id,{cols}) VALUES (1,{ph}) "
            f"ON CONFLICT(id) DO UPDATE SET {upd}",
            list(fields.values()),
        )


# ---------------- meal_log ----------------

def add_meal(eaten_on: str, slot: str, items: list[dict], source: str,
             confidence: str = "mid", kcal_low: int | None = None,
             kcal_high: int | None = None, photo: str | None = None,
             note: str | None = None) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO meal_log (eaten_on,slot,items_json,kcal_low,kcal_high,"
            "source,confidence,photo,note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eaten_on, slot, json.dumps(items, ensure_ascii=False), kcal_low,
             kcal_high, source, confidence, photo, note, _now()),
        )
        return cur.lastrowid


def meals_between(start: str, end: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM meal_log WHERE eaten_on>=? AND eaten_on<=? ORDER BY eaten_on DESC,id DESC",
            (start, end),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d["items_json"])
        out.append(d)
    return out


def meal_count() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) n FROM meal_log").fetchone()["n"]


# ---------------- context ----------------

def add_context(ctx_on: str, applies_to: str, tags: list[str],
                exclude: list[str] | None = None, raw: str | None = None) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO context (ctx_on,applies_to,tags_json,exclude_json,raw_text,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (ctx_on, applies_to, json.dumps(tags, ensure_ascii=False),
             json.dumps(exclude or [], ensure_ascii=False), raw, _now()),
        )
        return cur.lastrowid


def contexts_for(target_day: str) -> list[dict]:
    """只返回对目标日有效的语境。过期的不再参与决策。"""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM context WHERE applies_to=? ORDER BY id DESC", (target_day,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags_json"])
        d["exclude"] = json.loads(d["exclude_json"] or "[]")
        out.append(d)
    return out


def expire_contexts(before_day: str) -> int:
    with connect() as c:
        cur = c.execute("DELETE FROM context WHERE applies_to < ?", (before_day,))
        return cur.rowcount


# ---------------- feedback ----------------

def add_feedback(eaten_on: str, item_name: str, positive: int,
                 note: str | None = None) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO feedback (eaten_on,item_name,positive,note,created_at)"
            " VALUES (?,?,?,?,?)",
            (eaten_on, item_name, positive, note, _now()),
        )
        return cur.lastrowid


def feedback_for(item_name: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM feedback WHERE item_name LIKE ? OR ? LIKE '%'||item_name||'%'",
            (f"%{item_name}%", item_name),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- orders ----------------

def save_order(order: dict) -> None:
    """order 必须是 create-order 返回的 orderDetail 原样字段，不做加工。"""
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO orders (order_id,store_code,order_type,items_json,"
            "combo_json,coupon_json,total_cents,discount_cents,real_cents,status,"
            "reserve_at,pay_url,pay_expire_at,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.get("orderId"), order.get("storeCode"), order.get("orderType"),
                json.dumps(order.get("items"), ensure_ascii=False),
                json.dumps(order.get("comboItemList"), ensure_ascii=False),
                json.dumps(order.get("couponList"), ensure_ascii=False),
                _cents(order.get("totalAmount")),
                _cents(order.get("totalDiscountAmount")),
                _cents(order.get("realTotalAmount")),
                order.get("orderStatus"), order.get("reservationDate"),
                order.get("payH5Url") or order.get("payUrl"), order.get("expirePayTime"),
                _now(),
            ),
        )


def _cents(v: Any) -> int | None:
    """金额字段实测是字符串（"15.5"）或分（1580），统一成「分」。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # 订单里的 totalAmount 单位是元（"15.5"）；calculate-price 的 price 单位是分（1580）
    return int(round(f * 100)) if f == int(f) and f < 1000 and "." in str(v) else int(round(f))


def orders_since(days: int = 60) -> list[dict]:
    start = (date.today() - timedelta(days=days)).isoformat()
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE substr(created_at,1,10)>=? ORDER BY id DESC", (start,)
        ).fetchall()
    return [dict(r) for r in rows]


def paid_products(days: int = 60) -> list[str]:
    """真实付过钱的商品名 —— 比照片可靠的偏好来源。"""
    names: list[str] = []
    for o in orders_since(days):
        try:
            for it in json.loads(o.get("combo_json") or "[]"):
                if it.get("itemName"):
                    names.append(it["itemName"])
        except json.JSONDecodeError:
            pass
    return names


# ---------------- coupon ----------------

def upsert_coupons(store_code: str, order_type: int, rows: list[dict]) -> None:
    with connect() as c:
        for r in rows:
            prods = ",".join(str(p.get("productCode")) for p in (r.get("products") or []))
            c.execute(
                "INSERT INTO coupon_snap (coupon_id,coupon_code,title,product_codes,"
                "valid_from,valid_to,store_code,order_type,captured_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(coupon_id,store_code,order_type) DO UPDATE SET"
                " coupon_code=excluded.coupon_code,title=excluded.title,"
                " product_codes=excluded.product_codes,captured_at=excluded.captured_at",
                (r.get("couponId"), r.get("couponCode"), r.get("title"), prods,
                 None, r.get("tradeDateTime"), store_code, order_type, _now()),
            )


def coupons_for(product_code: str, store_code: str, order_type: int) -> list[dict]:
    """只返回**绑定该商品**的券。实测券是单品级绑定，不是通用折扣。"""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM coupon_snap WHERE store_code=? AND order_type=?"
            " AND (','||product_codes||',') LIKE ?",
            (store_code, order_type, f"%,{product_code},%"),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- plan ----------------

def save_plan(target_at: str, store_code: str, order_type: int, payload: dict,
              reason: dict | None = None, discount: dict | None = None) -> int:
    with connect() as c:
        c.execute("UPDATE plan SET status='superseded' WHERE target_at=? AND status='planned'",
                  (target_at,))
        cur = c.execute(
            "INSERT INTO plan (target_at,store_code,order_type,payload_json,reason_json,"
            "discount_json,status,created_at) VALUES (?,?,?,?,?,?, 'planned', ?)",
            (target_at, store_code, order_type,
             json.dumps(payload, ensure_ascii=False),
             json.dumps(reason or {}, ensure_ascii=False),
             json.dumps(discount or {}, ensure_ascii=False), _now()),
        )
        return cur.lastrowid


def due_plans(within_min: int = 20) -> list[dict]:
    """取出即将到点、需要执行的计划。定时任务只认这个。"""
    now = datetime.now()
    lo = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    hi = (now + timedelta(minutes=within_min)).strftime("%Y-%m-%d %H:%M")
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM plan WHERE status='planned' AND target_at BETWEEN ? AND ?"
            " ORDER BY target_at", (lo, hi),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload_json"])
        out.append(d)
    return out


def mark_plan(plan_id: int, status: str, order_id: str | None = None) -> None:
    with connect() as c:
        c.execute("UPDATE plan SET status=?, order_id=? WHERE id=?",
                  (status, order_id, plan_id))


def summary() -> dict:
    with connect() as c:
        n_meal = c.execute("SELECT COUNT(*) n FROM meal_log").fetchone()["n"]
        n_ctx = c.execute("SELECT COUNT(*) n FROM context").fetchone()["n"]
        n_ord = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
        n_plan = c.execute("SELECT COUNT(*) n FROM plan WHERE status='planned'").fetchone()["n"]
        n_cp = c.execute("SELECT COUNT(*) n FROM coupon_snap").fetchone()["n"]
    return {"meals": n_meal, "contexts": n_ctx, "orders": n_ord,
            "plans": n_plan, "coupons": n_cp, "db": db_path()}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="麦麦早餐管家 · 本地记忆层")
    ap.add_argument("cmd", choices=["init", "summary", "meals", "orders", "plans", "coupons"])
    a = ap.parse_args()

    if a.cmd == "init":
        connect().close()
        print(f"已初始化: {db_path()}")
    elif a.cmd == "summary":
        print(json.dumps(summary(), ensure_ascii=False, indent=2))
    elif a.cmd == "meals":
        for m in meals_between("2000-01-01", "2099-12-31")[:20]:
            print(m["eaten_on"], m["slot"], m["source"], m["confidence"],
                  [i["name"] for i in m["items"]])
    elif a.cmd == "orders":
        for o in orders_since(365)[:20]:
            print(o["order_id"], o["status"], o["real_cents"], o["reserve_at"])
    elif a.cmd == "plans":
        for p in due_plans(100000):
            print(p["id"], p["target_at"], p["status"], p["store_code"])
    elif a.cmd == "coupons":
        with connect() as c:
            for r in c.execute("SELECT * FROM coupon_snap ORDER BY id DESC LIMIT 20"):
                print(dict(r))
