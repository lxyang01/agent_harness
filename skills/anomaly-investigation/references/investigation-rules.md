# 账单异常调查规则

## 四类异常判定口径

| 维度 | 判定口径 | 阈值 |
| --- | --- | --- |
| spike 类别激增 | 类别当前周期金额 ≥ spike_ratio × 上一周期金额，且 ≥ spike_min | spike_ratio = 2，spike_min = ¥100 |
| duplicate 重复扣费 | 同一商户、同一金额在 duplicate_window_days 内再次扣费 | duplicate_window_days = 3 |
| price_hike 订阅涨价 | 订阅最近一次扣费与 expected_amount 偏差超过 max(hike_min_abs, hike_ratio × expected_amount) | hike_min_abs = ¥1，hike_ratio = 20% |
| outlier 大额离群 | 单笔金额 ≥ outlier_min，且 ≥ outlier_ratio × 该类别平均单笔 | outlier_min = ¥200，outlier_ratio = 5 |

## 基本计算

- 绝对变化 = 当前周期金额 − 基线周期金额。
- 变化率 = 绝对变化 / 基线周期金额。
- 基线为零且当前大于零：标记为“新出现”，不计算百分比。
- 两个周期均为零：不是异常。

## 低基数与小额保护

当类别笔数很少（例如基线只有 1–2 笔）或金额基数很小（例如单笔低于 ¥10 的订阅）时，变化率容易被放大。此时优先展示绝对金额，并把结论标为低置信度或观察项。

## 下钻顺序

先由 `bill_compare` 确认整体口径，再按维度用 `bill_anomalies` 定位对象，最后用 `bill_search`/`bill_samples` 核对具体交易。只有数据支持时才继续细分（按商户、支付方式、金额区间），避免在小样本上过度切片。

## 停止条件

- 当前周期没有交易数据。
- 基线范围不可比较（不等长周期或数据缺失）。
- 工具结果互相矛盾且无法验证。
- 样本不足以支持进一步推断。
