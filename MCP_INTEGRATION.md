# MCP 集成说明

本文档记录本项目**实际调用过**的麦当劳 MCP 能力、调用链路、以及对接中发现的接口事实。
所有结论均来自真实请求，原始响应保存在 [`evidence/`](evidence/) 中，可逐条复核。

## 一、接入信息

| 项 | 值 |
|---|---|
| MCP Server | 麦当劳中国 MCP（`https://mcp.mcd.cn`） |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0） |
| 鉴权 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 能力声明 | `capabilities.tools`，`serverInfo: mcd-mcp v1.0.0` |
| 工具总数 | **35**（实测 `tools/list` 枚举，见 `evidence/tools_list.json`） |
| 限流 | 600 请求/分钟/Token，超限返回 429 |

本项目不依赖任何特定 IDE 宿主。`scripts/mcp_call.py` 直接对端点发 JSON-RPC，
因此验证结果可以在任意环境复现 —— 这也是本文档所有数字可信的前提。

## 二、实际使用的 Tool

### 核心链路（8 个）

| Tool | 在本项目中的作用 | 关键参数 | 实测结论 |
|---|---|---|---|
| `query-nearby-stores` | 定位用户常去的门店，拿到 `storeCode` 与 `beCode` | `beType=1`、`searchType=2`、`city`、`keyword` | 返回 `reservationTimeOptions`，内含**门店实际的早餐时段**（实测门店为 `06:44–10:15`），且通常只给**今明两天** |
| `query-meals` | 拉取指定时段在售菜单 | `storeCode`、`orderType=1`、`beType=1`、`reservationDate` | 返回 `categories[]`（分类下只给 code+tags）与 `meals{}`（code→名称/价格）。**没有任何营养字段**，且 `currentPrice` 是"随单购早餐卡"优惠价，不等于真实售价 |
| `query-meal-detail` | 把套餐拆成单品，用于热量加总 | `storeCode`、`orderType`、`beType`、`code` | 返回 `rounds[].choices[]`，每个 choice 带 `code / quantity / isDefault / diffPrice`。**`isDefault=1` 即套餐的默认组成**，这是热量可算的关键 |
| `calculate-price` | 获取权威价格、取餐方式、优惠 | `storeCode`、`orderType`、`beType`、`items[]`、`reservationDate` | 金额单位为**分**（2550 = ¥25.50）。`takeWayList` 返回可选取餐方式（`eat-in` 堂食 / `locker-in` 取餐柜），其 `code` 是 `create-order` 的 `takeWayCode` 来源 |
| `list-nutrition-foods` | 官方营养数据源 | 无参数 | 一次返回 **160 条**，格式为**以中文名为键的文本表**，字段 `energyKj / energyKcal / protein / fat / carbohydrate / sodium / calcium`。**无商品编码、无份量字段** |
| `query-store-coupons` | 门店维度可用券（结构化） | `storeCode`、`orderType`、`beType`、`reservationDate` | 返回 `couponId / couponCode / products[].productCode`，可直接用于 `calculate-price` 的 `items[].couponId` |
| `query-my-coupons` | 账户维度券 | 无参数 | 只返回 Markdown 文本（含图片），**没有结构化 couponId**，不能直接用于计价。需要券码时优先用 `query-store-coupons` |
| `now-time-info` | 服务器时间基准 | 无参数 | 返回 `GMT+08:00` 的格式化时间。所有 `reservationDate` 应以此为准，避免本地时钟偏差 |

### 评估但未进入首版链路

| Tool | 未使用原因 |
|---|---|
| `create-order` | 会创建真实订单并返回支付链接。首版把订单填到**支付前一步**交给用户确认，避免自动化误下单 |
| `delivery-query-addresses` | 实测新账号返回 `{"addresses":[]}`。外送链路需先 `delivery-create-address`，属于首次设置步骤，不进日常链路 |
| `cancel-order` / `query-order` | 作为异常兜底预留，不在正常路径调用 |
| `available-coupons` / `auto-bind-coupons` | 领券动作涉及账户变更，须用户显式要求才执行 |
| `order-list` | 历史订单可作偏好冷启动的补充信号，但首版以用户主动记录为主 |
| `campaign-calendar` / `mall-*` / `draw-lottery` / `party-*` | 与"工作日早餐"场景无关，不纳入 |

## 三、完整调用链路

「安排明早早餐」的一次完整执行：

```
1. now-time-info
   └─ 取服务器时间，确定 target_date 与 reservationDate 的合法区间

2. query-nearby-stores(beType=1, searchType=2, city, keyword)
   └─ 取 storeCode
   └─ 读 reservationTimeOptions → 确认目标时间落在「早餐」时段内
      （实测：门店只给今明两天，早餐 06:44–10:15，因此不能承诺提前一周）

3. query-meals(storeCode, orderType=1, beType=1, reservationDate="YYYY-MM-DD HH:mm")
   └─ 得到当日在售：categories[] + meals{code: {name, currentPrice, tags}}

4. 对每个早餐候选（scripts/resolver.py）
   ├─ a. list-nutrition-foods（进程内缓存 160 条）按中文名精确匹配 → 命中即得官方 kcal
   ├─ b. 未命中则 query-meal-detail(code) → 取 rounds[].choices[isDefault=1]
   │     └─ 组成里的每个单品再回 a 匹配 → 全部命中才加总
   │     └─ 部分命中一律判为「无官方数据」，不输出残缺合计
   └─ c. 仍未命中 → 标记 missing（热量未知），不得参与热量预算

5. query-store-coupons(storeCode, orderType, beType, reservationDate)
   └─ 取可用券的 couponId / couponCode，交给后续计价

6. scripts/planner.py
   └─ 按严格优先级筛出 1 个方案 + 3 个备选，输出决策 trace

7. calculate-price(storeCode, orderType, beType, items[], reservationDate)
   └─ 权威价格（分→元）、优惠、takeWayList

8. （用户确认后）create-order(...)
   └─ 返回订单详情 + 支付链接 → 交由用户本人完成付款
```

## 四、对接中必须处理的四个工程问题

这些问题如果不处理，程序会**静默给出错误答案**，比报错更危险。

### 1. 返回体是「Markdown 说明 + 原始 JSON」拼接体

多数工具返回的 `content[].text` 并非纯 JSON，而是字段说明 Markdown 后直接拼一段 JSON，
且说明文字里含 `{data[].title}` 这类模板花括号。直接 `json.loads` 必然失败；
"找第一个 `{`" 也会命中模板花括号。

`mcp_call.py` 的解法：优先定位 `## Original Response` 标记，否则搜索 `{"success"` / `{"code"` 作为起点，
再用 `raw_decode` 截取。**只有拿到结构化 data，才能对决策做程序化断言**，
否则整个产品会退化成"让模型读一段自然语言再猜"。

### 2. 菜单价 ≠ 真实售价

`query-meals` 的 `currentPrice` 是"随单购早餐卡优惠"后的价格。
实测「猪柳蛋麦满分套餐」菜单显示 ¥15.3，`calculate-price` 返回 **2550 分 = ¥25.50**。

因此：菜单价只能用于粗筛，**对用户报的每一分钱都必须来自 `calculate-price`**。
同时金额单位是**分**，展示前必须除以 100。

### 3. 营养库与菜单的键不相交

菜单只有 `code`，营养库只有中文名。这不是"匹配一下就行"，而是有量化成本的：

| 处理方式 | 可算热量餐品 | 占比 |
|---|---|---|
| 仅单品名称精确匹配 | 11 / 70 | 16% |
| + 套餐拆解（`query-meal-detail`） | 32 / 70 | **46%** |
| 完全无官方数据 | 20 / 70 | 29% |

套餐拆解是本项目对 MCP 能力的关键利用方式：`rounds[].choices[isDefault=1]` 给出默认组成，
把「套餐」还原成营养库里存在的单品再求和。实测示例：

```
猪柳蛋麦满分套餐 546 kcal
  = 猪柳蛋麦满分 387 + 脆薯饼 + 大杯鲜萃咖啡
```

对照：若把「套餐」误按单品匹配，会得到 387 kcal —— 低估 159 kcal，且方向恒定偏低估，
会让系统误判"今天还剩很多额度"。这类偏差比随机误差更危险，必须从规则上禁止。

### 4. 参数互斥会造成硬失败

`query-meal-detail` 的 `code` 参数曾被误写成 `mealCode`，直接返回 `400 code不得为空白`；
`orderType=1`（到店自取）若传 `beCode` 也会报错。这类错误不会降级，只会中断，
因此 `SKILL.md` 里以「陷阱清单」形式固化，避免每次重新踩。

## 五、业务价值

麦当劳 MCP 提供的是**事实层**（在售什么、多少钱、能不能预约），
本项目在其上补的是**决策层**（在约束下替你选一个，并说清为什么）。

| MCP 提供 | 本项目补上 |
|---|---|
| 当日在售 70 个早餐餐品 | 从 70 个里收敛到 1 个，且不需要用户做选择 |
| 官方营养数据（按名称） | 套餐拆解后与可售餐品对齐，给出该餐品的真实热量 |
| 预约时段与价格 | 把时段、预算、热量区间、口味、当日情境合成一个可行解 |
| 券与优惠 | 在热量区间内比价，而不是为了省钱牺牲热量目标 |
| 支付链接 | 停在支付前一步，把最后一步留给人 |

**一句话**：MCP 让"能不能买、多少钱"变成可编程的事实；这个 Skill 让"该不该买这一个"变成可解释的决策。

## 六、合规与安全

- 仓库内**不含任何真实 Token**。`scripts/.token` 已在 `.gitignore` 中排除，配置文件仅使用占位符。
- 所有验证均为只读查询（`query-*` / `list-*` / `calculate-price`），未创建、未取消任何真实订单。
- 付款环节始终交回用户本人，Skill 不具备也不索取代扣能力。
- 营养与热量信息均标注来源（官方 / 估算 / 未知），不将估算包装为官方数据。
