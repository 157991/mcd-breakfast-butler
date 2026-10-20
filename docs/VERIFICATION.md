# 验证报告

本文件记录所有实测结论、对应数字、以及**尚未验证的部分**。
原始响应全部保存在 [`evidence/`](../evidence/)，每条结论都可复核。

## 一、验证方法

不依赖任何 IDE 的 MCP 宿主，用 `scripts/mcp_call.py` 直接对 `https://mcp.mcd.cn`
发 JSON-RPC 2.0 请求（Streamable HTTP）。这样做的目的：

- 结论可复现，不依赖某个客户端的行为；
- 能拿到原始响应并落盘，而不是只看渲染后的结果；
- 能对数据做程序化断言（匹配率、覆盖率都必须算出来，而不是"看起来差不多"）。

```
python scripts/mcp_call.py list                      # 枚举 35 个工具
python scripts/mcp_call.py schema create-order       # 查看参数
python scripts/mcp_call.py call query-meals '{...}'  # 实际调用并落盘到 evidence/
```

## 二、能力验证结果

| 项目 | 结论 | 证据文件 |
|---|---|---|
| 服务连通 | `serverInfo: mcd-mcp v1.0.0`，`protocolVersion 2025-03-26` | `tools_list.json` |
| 工具数量 | **35 个** | `tools_list.json` |
| 预约能力 | ✅ `query-meals` / `calculate-price` / `create-order` / `query-store-coupons` **均含 `reservationDate`**（`yyyy-MM-dd HH:mm`），预约是全链路原生能力 | `tools_list.json` |
| 预约窗口 | ⚠️ `query-nearby-stores.reservationTimeOptions` 只返回**今天与明天**两天 | `query-nearby-stores.json` |
| 预约时段 | ⚠️ 时段由**门店生成槽位**，非用户自选整点。实测门店早餐为 `06:44–10:15`，另一店为 `05:14–10:15` | `query-nearby-stores.json` |
| 菜单 | 70 个早餐餐品，**无任何营养字段** | `query-meals.json` |
| 营养库 | **160 条**，以中文名为键，**无商品编码、无份量字段** | `list-nutrition-foods.json` |
| 套餐拆解 | ✅ `query-meal-detail` 返回 `rounds[].choices[]`，含 `isDefault` | `query-meal-detail.json` |
| 计价 | 返回**分**（2550 = ¥25.50），含 `takeWayList`（`eat-in` / `locker-in`） | `calculate-price.json` |
| 优惠券 | `query-store-coupons` 返回结构化 `couponId/couponCode/products`；`query-my-coupons` **只有 Markdown** | `query-store-coupons.json`、`query-my-coupons.json` |
| 外送 | ⚠️ 新账号地址为空 `{"addresses":[]}`，需先 `delivery-create-address` | `delivery-query-addresses.json` |
| 支付 | ⚠️ `create-order` 返回**订单详情与支付链接**，无代扣能力 | `tools_list.json`（schema 与描述） |
| 时间基准 | `GMT+08:00` | `now-time-info.json` |

## 三、营养覆盖率的计算过程

这是本项目最核心的一组数字，计算方式完全公开（`scripts/resolver.py`）。

### 三级解析

| 级别 | 方法 | 说明 |
|---|---|---|
| L1 单品直配 | 菜单名 ↔ 营养库名精确匹配（允许去杯型/冷热标记） | 命中即用官方 kcal |
| L2 套餐拆解 | `query-meal-detail` → `rounds[].choices[isDefault=1]` → 各单品回 L1 → 加总 | **必须全部命中才可用** |
| L3 无数据 | 标记 `missing` | 热量显示"未知"，不参与热量预算 |

### 结果

| 指标 | 数值 |
|---|---|
| 早餐相关餐品 | **70** |
| 仅单品直配可用 | **11**（16%） |
| 套餐拆解后可用 | **32**（46%） |
| 完全无官方数据 | **20**（29%） |

**套餐拆解把覆盖率从 16% 提升到 46%，接近三倍。**
原因是菜单里大量商品是「套餐/组合」，其热量不等于任何单个营养表条目；
但拆开后，组成单品大多能在营养库中找到。

实测样例：

```
猪柳蛋麦满分套餐 546 kcal = 猪柳蛋麦满分(387) + 脆薯饼 + 大杯鲜萃咖啡
```

### 三个被修正的错误（重点）

这三次修正比最终数字更重要 —— 它们都是"程序能跑、结果却错"的情况：

| # | 错误 | 表现 | 修正 |
|---|---|---|---|
| 1 | 子串匹配 | 「**北非蛋风味**猪柳蛋麦满分」被匹配成「猪柳蛋麦满分」，错得 387 kcal | 只允许规格层面变体，禁止子串包含 |
| 2 | 套餐降级成单品 | 「猪柳蛋麦满分套餐」被去后缀匹配成单品 387 kcal，实际 546，**系统性低估** | 套餐一律不参与单品匹配 |
| 3 | 残缺合计 | 「吉士蛋麦满分组合」只认出饮品，合计 15 kcal | 部分命中一律判为"无数据" |

第 2 类的危险性在于**偏差方向恒定**：永远偏低 → 系统总以为"今天还剩很多额度" → 越吃越多。

## 四、决策引擎验证

三个场景均用真实菜单数据（`data/breakfast_menu.json`，来自实测）运行：

```bash
python scripts/planner.py --ledger data/ledger.normal.json  --date 2026-10-21
python scripts/planner.py --ledger data/ledger.sparse.json  --date 2026-10-21
python scripts/planner.py --ledger data/ledger.request.json --date 2026-10-21
```

| 场景 | 输出 | 验证的规则 |
|---|---|---|
| 正常三餐 + 昨晚饮酒 | 大脆鸡扒麦满分组合 · ¥8.1 · 376 kcal | 酒精情境排除冰饮 / 组合比单品便宜 / 热量在区间 |
| 一天没拍照片 | 猪柳蛋麦满分组合 · ¥12.6 · 402 kcal | 不追问 / 不视为零摄入 / 仅降低近期权重 |
| 临时说想吃营养卷 | 火腿扒早安营养卷组合 · ¥11.7 · 459 kcal | 明确要求优先 / 仍受预算与热量约束 |

编码过程中发现并修复的两个缺陷：

1. **单品与组合被当成不同主体** → 系统认为"单品没吃过"，反复推单品，而用户实际吃的是组合 → 改为按主体名归一。
2. **临时要求生效但卡片未说明** → 用户看不出系统听懂了 → 理由中显式回显。

## 五、尚未验证的部分（诚实列出）

| 项目 | 状态 | 原因 | 下一步 |
|---|---|---|---|
| `create-order` 实际创建订单 | ❌ 未执行 | 会产生真实订单，涉及用户资金与账户 | 需用户明确授权后执行一次并立即 `cancel-order` |
| 支付链接跳转后的完成态 | ❌ 未验证 | 同上 | 同上 |
| 麦乐送整链路 | ❌ 未验证 | 账号无地址，需先 `delivery-create-address`（涉及创建用户数据） | 由用户自行创建地址后验证 |
| 重复提交是否幂等 | ❌ 未验证 | 需要真实下单两次做对照 | 在授权测试中一并验证，**这是自动重试的前置条件** |
| 跨日菜单变更幅度 | ⚠️ 部分 | 只对比了 10-20 与 10-21 两天的预约时段 | 连续观察多日 |

**在没有验证"重复提交不会重复扣款"之前，本项目不实现自动重试。**

## 六、复现步骤

```bash
echo "YOUR_MCP_TOKEN" > scripts/.token
python scripts/mcp_call.py list
python scripts/resolver.py <storeCode> "2026-10-21 07:00"
python scripts/planner.py --ledger data/ledger.normal.json --date 2026-10-21
```

`scripts/resolver.py` 会把 70 个餐品的解析过程逐行打印，
并对每个 `query-meal-detail` 结果做本地缓存（`evidence/details/`），重复运行不会重复消耗接口。
