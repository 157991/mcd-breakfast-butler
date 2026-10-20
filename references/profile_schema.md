# 用户档案结构

首次设置只收 6 项，其余全部给默认值。**不要一次问太多** —— 用户愿意填的耐心只有一轮。

## 必填（6 项）

| 字段 | 说明 | 默认 | 为什么必须 |
|---|---|---|---|
| `order_type` / `be_type` | 到店取餐柜（1/1）或麦乐送（2/2） | 到店取餐柜 | 决定后续所有接口的参数分支 |
| `store_code` | 常去门店 | 由城市+关键词查得 | 菜单、价格、预约时段都是门店维度的 |
| `meal_time` | 工作日吃饭时间 | `07:30` | 预约槽位必须从这个时间往里选 |
| `budget_cny` | 单餐金额预算 | `18` | 金额约束 |
| `calorie_window` | 热量区间 `[min, max]` | `[350, 500]` | 热量约束，**区间而非目标值** |
| `allergens` / `exclude_keywords` | 忌口与过敏 | `[]` | 硬否决项，误判代价最高 |

## 选填（用户主动提到才问）

| 字段 | 何时问 | 处理方式 |
|---|---|---|
| 身高 / 体重 / 活动水平 | 仅当用户自己说"想减重/想控制体重" | 用于估算 TDEE，**不承诺结果** |
| `prefer_keywords` | 用户说"我喜欢粥/麦满分" | 进入好吃评分 |
| 长期偏好（如"不喝冰的"） | 用户说"我一直不喜欢…" | 写入 `exclude_keywords`，**不是**情境 |

## 完整示例

```json
{
  "city": "上海",
  "store_code": "1450034",
  "store_name": "麦当劳上海徐家汇餐厅",
  "order_type": 1,
  "be_type": 1,
  "meal_time": "07:30",
  "budget_cny": 18.0,
  "calorie_window": [350, 500],
  "goal": "slow_loss",
  "body": { "sex": "male", "height_cm": 175, "weight_kg": 72, "activity_level": "light" },
  "allergens": [],
  "exclude_keywords": [],
  "prefer_keywords": ["麦满分", "粥"]
}
```

## 长期偏好 vs 当日情境：写错位置会一直错

| 用户的话 | 写入位置 | 生效范围 | 写错的后果 |
|---|---|---|---|
| "我一直不喜欢冰饮" | `profile.exclude_keywords` | 永久 | 只当情境 → 明天又给冰饮 |
| "今晚喝酒了，明早别冰咖啡" | `ledger.contexts[].tags=["alcohol"]` | 事件日 + 次日 | 写成永久偏好 → 从此再也不给冰饮 |
| "这个吃不饱" | `ledger.feedback[]` | 影响好吃评分 | 直接判"不喜欢" → 永久不再推荐 |

## 存储位置

- 默认：`~/.mcd-breakfast/profile.json` 与 `ledger.json`
- 可用环境变量 `MCD_BUTLER_HOME` 改目录
- **不写入仓库**：档案含体重、作息、饮酒记录，属于敏感信息
