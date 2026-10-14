# 指标语义层（Metrics Dictionary）

> 作用：统一指标口径，消除自然语言歧义。此文件内容会注入 Prompt，Agent 生成 SQL 时必须按以下定义取数。

## 销售类指标

| 指标名 | 口径定义 | SQL 口径 |
|---|---|---|
| **GMV（成交总额）** | 统计周期内所有订单的支付金额，不论是否退款 | `SUM(orders.total_amount)`（不过滤 status） |
| **净销售额** | GMV 扣除退款金额后的销售额 | `SUM(orders.total_amount) - SUM(refunds.refund_amount)`（LEFT JOIN） |
| **订单量** | 统计周期内订单条数 | `COUNT(DISTINCT orders.order_id)` |
| **客单价** | GMV 除以订单量 | `SUM(total_amount) / COUNT(DISTINCT order_id)` |

## 电商运营指标

| 指标名 | 口径定义 | SQL 口径 |
|---|---|---|
| **退款率（金额口径）** | 退款总额占 GMV 比例 | `SUM(refunds.refund_amount) / SUM(orders.total_amount)` |
| **退款率（订单口径）** | 发生退款的订单数占总订单数比例 | `COUNT(DISTINCT refunds.order_id) / COUNT(DISTINCT orders.order_id)` |
| **热销商品** | 按销量（件数）排序的商品 | `SUM(order_items.quantity)` 排序，**不是**按销售额 |
| **品类** | 商品所属类目 | `products.category`（数码/服饰/美妆/食品/家居） |

## 用户与营销指标

| 指标名 | 口径定义 | SQL 口径 |
|---|---|---|
| **新老客** | 注册日期在统计周期内 = 新客；此前注册 = 老客 | `users.register_time >= 统计周期起点` 判定 |
| **新客 GMV 占比** | 新客订单实付金额占总 GMV 比例 | orders JOIN users 后按注册日期分组汇总 |
| **活动订单** | 参与营销活动的订单 | `orders.campaign_id IS NOT NULL`（NULL = 自然订单） |
| **活动 GMV** | 活动订单的实付金额合计 | `SUM(orders.total_amount) WHERE campaign_id = ?` |
| **活动 ROI** | 活动带来的 GMV 与活动预算之比 | 活动 GMV / `campaigns.budget` |
| **券核销率** | 已核销张数占已领取张数比例 | `COUNT(coupon_usage) / coupons.issued_count` |
| **用券订单** | 有核销记录的订单 | orders JOIN coupon_usage ON order_id |
| **客单价（用券/自然）** | 对比用券订单与自然订单的客单价 | 按 `coupon_usage.order_id IS NOT NULL` 分组分别计算 |

## 歧义消解规则（Agent 必读）

1. 用户说"**销售额**"未指明口径 → 默认用 **GMV**，并在回答中注明"按支付金额口径，未扣除退款"
2. 用户说"**退款率**"未指明口径 → 默认用**订单口径**，并注明
3. 用户说"**最近/近期**" → 默认**最近 7 天**（以数据集中最大日期为"今天"）
4. 用户说"**Top N / 前 N**" → 按对应指标降序取前 N
5. 统计金额保留 **2 位小数**；比例类指标用**百分比**（1 位小数）呈现
6. 用户说"**活动期间/大促期间**" → 按 `campaigns.start_time ~ end_time` 过滤订单日期，或直接按 `orders.campaign_id` 关联（两者可能不同：活动期内也有自然订单）
7. `orders.total_amount` 为**实付金额（已扣券）**；券抵扣金额在 `coupon_usage.discount_amount`，商品原价需按明细 `quantity × unit_price` 汇和
