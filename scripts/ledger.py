#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
饮食账本（作品的记忆层）

三条存储规则，都对应"用户懒得输入"这个前提：
  1. 一切写入都是追加，不做补录追问；缺哪顿就缺哪顿。
  2. 每条记录必须带 source（official / photo_estimate / user_utterance）与 confidence，
     因为「麦当劳官方数据」和「照片估算」的可信度差一个量级，混在一起就再也分不开。
  3. 没拍 ≠ 没吃。缺失日期不写零值，只在读取时表现为「无记录」，由 planner 自行降低近期权重。

默认存储：~/.mcd-breakfast/ledger.json（不写进仓库，避免隐私入库）
"""
import argparse
import json
import os
from datetime import date, datetime

STORE_DIR = os.environ.get("MCD_BUTLER_HOME", os.path.join(os.path.expanduser("~"), ".mcd-breakfast"))
LEDGER = os.path.join(STORE_DIR, "ledger.json")
PROFILE = os.path.join(STORE_DIR, "profile.json")

EMPTY = {"meals": [], "contexts": [], "explicit_requests": [], "feedback": []}


def load(path: str, default):
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return default


def save(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def cmd_add_meal(a) -> None:
    led = load(LEDGER, dict(EMPTY))
    items = []
    for pair in a.items.split(","):
        if not pair.strip():
            continue
        name, _, kcal = pair.partition(":")
        items.append({"name": name.strip(), "kcal": float(kcal) if kcal.strip() else None})
    led["meals"].append({
        "date": a.date or date.today().isoformat(),
        "slot": a.slot,
        "items": items,
        "source": a.source,
        "confidence": a.confidence or ("high" if a.source == "mcd_official" else "low"),
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save(LEDGER, led)
    print(f"已记入 {a.slot}：{'、'.join(i['name'] for i in items)}（来源 {a.source}，不追问详情）")


def cmd_add_context(a) -> None:
    led = load(LEDGER, dict(EMPTY))
    led["contexts"].append({
        "date": a.date or date.today().isoformat(),
        "tags": [t.strip() for t in a.tags.split(",") if t.strip()],
        "source": "user_utterance",
        "raw": a.raw or "",
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save(LEDGER, led)
    print(f"已记下情境：{a.tags}（原话：{a.raw or '—'}）")


def cmd_add_request(a) -> None:
    led = load(LEDGER, dict(EMPTY))
    led["explicit_requests"].append({
        "target_date": a.date or (date.today().isoformat()),
        "text": a.text,
        "must": [m.strip() for m in (a.must or "").split(",") if m.strip()],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save(LEDGER, led)
    print(f"已记下：{a.text} → 将在 {a.date or '当天'} 的安排中优先满足")


def cmd_add_feedback(a) -> None:
    led = load(LEDGER, dict(EMPTY))
    led["feedback"].append({
        "date": date.today().isoformat(),
        "name": a.name,
        "positive": bool(a.positive),
        "note": a.note or "",
    })
    save(LEDGER, led)
    print(f"已记下反馈：{a.name} {'喜欢' if a.positive else '不喜欢'}（{a.note or '无备注'}）")


def cmd_summary(a) -> None:
    led = load(LEDGER, dict(EMPTY))
    meals = led["meals"]
    print(f"记录餐次数：{len(meals)}")
    official = sum(1 for m in meals if m.get("source") == "mcd_official")
    est = sum(1 for m in meals if m.get("source") == "photo_estimate")
    print(f"  官方数据 {official} 次 / 照片估算 {est} 次")
    days = sorted({m["date"] for m in meals})
    if days:
        print(f"覆盖日期：{days[0]} ~ {days[-1]}（共 {len(days)} 天有记录）")
        span = (datetime.strptime(days[-1], "%Y-%m-%d").date()
                - datetime.strptime(days[0], "%Y-%m-%d").date()).days + 1
        print(f"覆盖率：{len(days)}/{span} = {len(days)/span:.0%}（缺的日期不视为零摄入）")
    kcals = [sum(i.get("kcal") or 0 for i in m["items"]) for m in meals]
    if kcals:
        print(f"单餐记录均值：{sum(kcals)/len(kcals):.0f} kcal（照片估算项误差约 ±40%，仅供参考）")
    print(f"情境记录：{len(led['contexts'])} 条｜明确要求：{len(led['explicit_requests'])} 条｜反馈：{len(led['feedback'])} 条")


def main() -> None:
    ap = argparse.ArgumentParser(description="麦麦早餐管家 · 饮食账本")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("add-meal", help="记录一餐（可只给照片结论，不填表）")
    m.add_argument("--slot", required=True, choices=["breakfast", "lunch", "dinner", "snack"])
    m.add_argument("--items", required=True, help="食物:热量,食物:热量")
    m.add_argument("--source", default="photo_estimate", choices=["mcd_official", "photo_estimate", "user_utterance"])
    m.add_argument("--confidence", default=None, choices=["high", "medium", "low"])
    m.add_argument("--date", default=None)
    m.set_defaults(func=cmd_add_meal)

    c = sub.add_parser("add-context", help="记录当天情境（如饮酒、生病、熬夜）")
    c.add_argument("--tags", required=True, help="alcohol,sick,late_night 逗号分隔")
    c.add_argument("--raw", default="", help="用户原话")
    c.add_argument("--date", default=None)
    c.set_defaults(func=cmd_add_context)

    r = sub.add_parser("add-request", help="记录临时明确要求")
    r.add_argument("--text", required=True)
    r.add_argument("--must", default="", help="必须命中的关键词，逗号分隔")
    r.add_argument("--date", default=None)
    r.set_defaults(func=cmd_add_request)

    f = sub.add_parser("add-feedback", help="记录吃后反馈")
    f.add_argument("--name", required=True)
    f.add_argument("--positive", action="store_true")
    f.add_argument("--note", default="")
    f.set_defaults(func=cmd_add_feedback)

    s = sub.add_parser("summary", help="查看记忆概况")
    s.set_defaults(func=cmd_summary)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
