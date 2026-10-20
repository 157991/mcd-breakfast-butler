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
python scripts/live_test.py --store 1450034 ...      # 真实链路，每步留 traceId
```

`live_test.py` 的设计原则是**读写分离**：默认只跑只读链路（不产生订单），
真下单必须显式加 `--execute-order`，且下单后立即 `cancel-order` 并复查状态。

## 二、能力验证结果

| 项目 | 结论 | 证据文件 |
|---|---|---|
| 服务连通 | `serverInfo: mcd-mcp v1.0.0`，`protocolVersion 2025-03-26` | `tools_list.json` |
| 工具数量 | **35 个** | `tools_list.json` |
| 真机调用 | **12 个**工具完成真实调用（含 `create-order` / `cancel-order` / `query-order` / `order-list`） | `evidence/live/*/timeline.json` |
| 预约能力 | ✅ `query-meals` / `calculate-price` / `create-order` / `query-store-coupons` **均含 `reservationDate`**（`yyyy-MM-dd HH:mm`） | `tools_list.json` |
| 预约窗口 | ⚠️ `query-nearby-stores.reservationTimeOptions` 只返回**今天与明天**两天 | `query-nearby-stores.json` |
| 预约时段 | ⚠️ 时段由**门店生成槽位**，非用户自选整点。门店 1450034 为 `05:14–10:15`，1450323 为 `06:44–10:15` | `query-nearby-stores.json` |
| 菜单 | **100 个**餐品（实测 07:00 时点），**无任何营养字段**；且混入全天供应的甜品（麦旋风、圆筒、新地） | `query-meals.json` |
| 营养库 | **160 条**，以中文名为键，**无商品编码、无份量字段、全部为单品** | `list-nutrition-foods.json` |
| 套餐拆解 | ✅ `query-meal-detail` 返回 `rounds[].choices[]`，含 `isDefault` 与 `quantity` | `query-meal-detail.json` |
| 计价 | 金额在 **`data.price`，单位是分**（1350 = ¥13.50）；`takeWayList[].code` 为 `eat-in` / `locker-in` | `calculate-price.json` |
| **支付窗口** | ⚠️ **下单后 15 分钟**（`createTime` → `expirePayTime`），超时订单作废 | `evidence/live/*/11_create-order.json` |
| 优惠券 | `query-store-coupons` 返回结构化 `couponId/couponCode/products`，**券绑定指定 `productCode`，非通用折扣**；`query-my-coupons` **只有 Markdown、无 traceId** | `query-store-coupons.json` |
| 外送 | ⚠️ 账号地址为空 `{"addresses":[]}`，需先 `delivery-create-address` | `delivery-query-addresses.json` |
| 时间基准 | `GMT+08:00`，服务端时间与本地一致 | `now-time-info.json` |

## 三、营养覆盖率的计算过程

这是本项目最核心的一组数字，计算方式完全公开（`scripts/resolver.py`）。

### 三级解析

| 级别 | 方法 | 说明 |
|---|---|---|
| L1 单品直配 | 菜单名 ↔ 营养库名精确匹配（允许去杯型/冷热标记） | 命中即用官方 kcal |
| L2 套餐拆解 | `query-meal-detail` → `rounds[]` → **取 `quantity>0` 的项** → 各单品回 L1 → 加总 | **必须全部命中才可用** |
| L3 无数据 | 标记 `missing` | 热量显示"未知"，不参与热量预算 |

### 结果（真机复测）

| 指标 | 数值 |
|---|---|
| 早餐菜单餐品 | **100** |
| 仅单品直配可用 | **23**（23%） |
| 套餐拆解后可用 | **30**（30%） |
| 完全无官方数据 | **70**（70%） |

**这个 30% 比第一版的 46% 低，但它是正确的。** 差异来源见下面第 4 条错误。

### 五个被修正的错误（重点）

这五次修正比最终数字更重要 —— 它们都是"程序能跑、结果却错"的情况：

| # | 错误 | 表现 | 修正 |
|---|---|---|---|
| 1 | 子串匹配 | 「**北非蛋风味**猪柳蛋麦满分」被匹配成「猪柳蛋麦满分」，错得 387 kcal | 只允许规格层面变体，禁止子串包含 |
| 2 | 套餐降级成单品 | 「猪柳蛋麦满分套餐」被去后缀匹配成单品 387 kcal，实际 546，**系统性低估** | 套餐一律不参与单品匹配 |
| 3 | 残缺合计 | 「吉士蛋麦满分组合」只认出饮品，合计 15 kcal | 部分命中一律判为"无数据" |
| 4 | **`isDefault` 不代表实际下单组成** | 按 `isDefault=1` 取到「小杯鲜萃咖啡」（热），真实订单给的是「鲜萃冰咖」（冰）。热量错，且"不要冰"规则漏检 | 改按 **`quantity>0`** 取 |
| 5 | 禁用词只匹配商品名 | 「大脆鸡扒麦满分组合」名字里无「冰」，规则放行，实际含「鲜萃冰咖」 | 禁用词穿透到**组成面**（名称 + parts） |

第 2 类的危险性在于**偏差方向恒定**：永远偏低 → 系统总以为"今天还剩很多额度" → 越吃越多。
第 4、5 类的危险性在于**安静**：结果看起来完全合规，只有真机下单才能发现。

### 第 4 条错误的完整证据

```
套餐「大脆鸡扒麦满分组合」detail 的饮品组：
    小杯鲜萃咖啡   isDefault=1   quantity=0
    鲜萃冰咖       isDefault=0   quantity=1

真实订单 1030268980000808540604890948 的 comboItemList：
    大脆鸡扒麦满分 ×1
    鲜萃冰咖       ×1        ← 认的是 quantity>0
```

`create-order` 在不传 `modification` 时，默认选择的是 `quantity>0` 的选项，**不是 `isDefault=1`**。
只按 `isDefault` 拆解，等于把冰饮当热咖啡。

## 四、价格验证：菜单价不是价格

`query-meals` 的 `currentPrice` 是「早餐卡专享价」，与 `calculate-price` 的实际应付不一致：

| 餐品 | 菜单价 | 实际应付（分） | 偏差 |
|---|---|---|---|
| 大脆鸡扒麦满分组合 | ¥8.1 | 1350（¥13.50） | **+67%** |
| 猪柳蛋麦满分组合 | ¥12.6 | 2100（¥21.00） | **+67%** |
| 大脆鸡扒麦满分 | ¥11.5 | 1150（¥11.50） | 0% |

**组合类一律 +67%，单品 0%。** 于是排序会翻转：按菜单价组合更便宜，按真实价单品更便宜。

**任何用 `currentPrice` 做预算判断的逻辑都会稳定地把用户推向更贵的东西。**
引擎因此采用两段式：菜单价仅粗筛，定案前对 top-N 逐条 `calculate-price` 重排。

## 五、决策引擎验证

三个场景均用真实菜单数据运行：

```bash
python scripts/planner.py --ledger data/ledger.normal.json  --date 2026-10-21
python scripts/planner.py --ledger data/ledger.sparse.json  --date 2026-10-21
python scripts/planner.py --ledger data/ledger.request.json --date 2026-10-21
```

真机链路（含精确核价）的输出：

| 场景 | 输出 | 验证的规则 |
|---|---|---|
| 正常三餐 + 昨晚饮酒 | 大脆鸡扒麦满分 · ¥11.50 · 361 kcal | 酒精情境穿透套餐，排除 **52 个**候选 / 真实价核价 / 热量在区间 |
| 一天没拍照片 | 按长期偏好安排 | 不追问 / 不视为零摄入 / 仅降低近期权重 |
| 临时说想吃营养卷 | 优先收窄到营养卷 | 明确要求优先 / 仍受预算与热量约束 |

编码过程中发现并修复的缺陷：

1. **单品与组合被当成不同主体** → 系统认为"单品没吃过"，反复推单品 → 改为按主体名归一。
2. **临时要求生效但卡片未说明** → 用户看不出系统听懂了 → 理由中显式回显。
3. **忌口规则在套餐上失效**（见第 5 条错误）→ 禁用词穿透组成面。
4. **`quantity=0` 被强转成 1** → 用组上的 `quantity` 兜底，不无脑当 1。
5. **菜单混入全天甜品** → 加 `off_meal_keywords` 品类兜底（麦旋风/圆筒/新地/派）。
6. **套餐详情缓存键只用了 `code`** → 改为 `store_orderType_code`，避免跨门店错套组成。

## 六、真机下单验证

两单，均在下单后立即取消，**零扣款**（`create-order` 只生成待支付订单）：

| 项目 | 订单 A | 订单 B |
|---|---|---|
| 订单号 | `1030268980000808540604890948` | `1030525170000795821281521101` |
| 内容 | 大脆鸡扒麦满分组合 ×1 | 大脆鸡扒麦满分 ×1 |
| 金额 | ¥13.50 | ¥11.50 |
| 取餐方式 | `locker-in` 外带 | `locker-in` 外带 |
| 取消前状态 | `待支付 / 10` | `待支付 / 10` |
| 取消后状态 | `订单已取消 / 60` | `订单已取消 / 60` |
| 支付窗口 | 09:50:50 → 10:05:50（15 分钟） | 09:52:22 → 10:07:22（15 分钟） |

订单 A 是**错误的**：它含「鲜萃冰咖」，而账本里明确写了"明早不要冰咖啡"。
正是这一单暴露了第 4、5 条错误。订单 B 是修正后重跑的，内容正确。

### 关于预约与支付的关键结论

```
reservationDate   2026-10-21 07:00     取餐时间（明早）
createTime        2026-10-20 09:52:22  下单时间（现在）
expirePayTime     2026-10-20 10:07:22  支付截止（15 分钟后）
```

**`reservationDate` 只锁取餐时间，不推迟付款。** 下单那一刻开始 15 分钟倒计时。

因此"前一晚自动下单、明早直接取"在接口层面不成立。可行形态只有：
① 早上起床前 15 分钟内下单；② 停在下单前一步等用户点。本项目选 ②。

## 七、尚未验证的部分（诚实列出）

| 项目 | 状态 | 原因 | 下一步 |
|---|---|---|---|
| 支付链接跳转后的完成态 | ❌ 未验证 | 需要真实付款 | 不在自动化范围内，需用户手动 |
| 麦乐送整链路 | ❌ 未验证 | 账号无地址，需先 `delivery-create-address` | 需用户提供真实地址信息 |
| 重复提交是否幂等 | ❌ 未验证 | 未做连续两次同参下单对照 | **这是自动重试的前置条件** |
| 优惠券实际抵扣 | ⚠️ 部分 | 可用券均为"指定商品 7 折"，与早餐组合不匹配 | 需构造匹配商品的订单 |
| 跨日菜单变更幅度 | ⚠️ 部分 | 观察到同一时点菜单从 70 项变为 100 项，但未连续观察 | 连续多日采样 |
| `pickupCode` 生成时机 | ⚠️ 推断 | 未支付订单中为空字符串，推断付款后生成 | 需真实付款验证 |

**在没有验证"重复提交不会重复扣款"之前，本项目不实现自动重试。**

## 八、复现步骤

```bash
echo "YOUR_MCP_TOKEN" > scripts/.token
python scripts/mcp_call.py list
python scripts/live_test.py --store 1450034 --date 2026-10-21 --time 07:00
python scripts/resolver.py <storeCode> "2026-10-21 07:00"
python scripts/planner.py --ledger data/ledger.normal.json --date 2026-10-21
```

`scripts/resolver.py` 会把每个餐品的解析过程逐行打印，
并对每个 `query-meal-detail` 结果做本地缓存（`evidence/details/`，键为 `store_orderType_code`），
重复运行不会重复消耗接口。
