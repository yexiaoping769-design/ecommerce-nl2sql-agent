-- 电商经营分析数据库（demo 数据，可整体替换为任意业务场景）
-- SQLite 语法，8 张表：users / products / campaigns / coupons / orders / order_items / coupon_usage / refunds
-- 归因链路：campaigns(活动) → coupons(发券) → coupon_usage(核销) → orders(成交) → refunds(退款)

-- 用户表（会员维度）
CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,           -- U0001
    city           TEXT NOT NULL,              -- 城市：福州/厦门/杭州/上海/深圳...
    source_channel TEXT NOT NULL,              -- 注册来源：app / web / mini_program / offline
    member_level   TEXT NOT NULL,              -- 会员等级：normal / silver / gold
    register_time  TEXT NOT NULL               -- 注册日期 YYYY-MM-DD
);

-- 产品表
CREATE TABLE IF NOT EXISTS products (
    product_id   TEXT PRIMARY KEY,             -- P001
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL,                -- 品类：数码 / 服饰 / 美妆 / 食品 / 家居
    price        REAL NOT NULL,                -- 标准售价（元）
    stock        INTEGER NOT NULL              -- 当前库存
);

-- 营销活动表
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id   TEXT PRIMARY KEY,            -- C001
    campaign_name TEXT NOT NULL,               -- 活动名：9月开学季大促
    campaign_type TEXT NOT NULL,               -- 类型：满减 / 折扣 / 秒杀
    start_time    TEXT NOT NULL,               -- 开始日期 YYYY-MM-DD
    end_time      TEXT NOT NULL,               -- 结束日期 YYYY-MM-DD
    budget        REAL NOT NULL                -- 活动预算（元）
);

-- 优惠券表（券挂在活动下发放）
CREATE TABLE IF NOT EXISTS coupons (
    coupon_id        TEXT PRIMARY KEY,         -- CP001
    campaign_id      TEXT NOT NULL REFERENCES campaigns(campaign_id),
    coupon_name      TEXT NOT NULL,            -- 券名：满200减30券
    coupon_type      TEXT NOT NULL,            -- 类型：满减 / 折扣
    threshold_amount REAL NOT NULL,            -- 使用门槛（元），0 = 无门槛
    discount_value   REAL NOT NULL,            -- 满减金额（元）/ 折扣率（如 0.9）
    total_count      INTEGER NOT NULL,         -- 发放总量
    issued_count     INTEGER NOT NULL,         -- 已领取量
    valid_from       TEXT NOT NULL,            -- 生效日期
    valid_to         TEXT NOT NULL             -- 失效日期
);

-- 订单表（销售流水）：一行 = 一笔订单
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,             -- 订单号 O202609010001
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    campaign_id  TEXT REFERENCES campaigns(campaign_id),  -- 参与的活动，未参与为 NULL
    total_amount REAL NOT NULL,                -- 实付金额（元），GMV 口径（已扣券）
    status       TEXT NOT NULL,                -- paid / shipped / completed / refunded
    channel      TEXT NOT NULL,                -- 下单渠道：app / web / mini_program
    create_time  TEXT NOT NULL                 -- 下单时间 YYYY-MM-DD HH:MM
);

-- 订单明细表：一行 = 订单内一个商品
CREATE TABLE IF NOT EXISTS order_items (
    item_id     TEXT PRIMARY KEY,              -- 明细号
    order_id    TEXT NOT NULL REFERENCES orders(order_id),
    product_id  TEXT NOT NULL REFERENCES products(product_id),
    quantity    INTEGER NOT NULL,              -- 购买数量
    unit_price  REAL NOT NULL                  -- 成交单价（元）
);

-- 券核销记录表：一行 = 一次用券
CREATE TABLE IF NOT EXISTS coupon_usage (
    usage_id        TEXT PRIMARY KEY,          -- CU00001
    coupon_id       TEXT NOT NULL REFERENCES coupons(coupon_id),
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    order_id        TEXT NOT NULL REFERENCES orders(order_id),
    discount_amount REAL NOT NULL,             -- 该笔订单券抵扣金额（元）
    used_time       TEXT NOT NULL              -- 核销时间（即下单时间）
);

-- 退款表：一行 = 一次退款申请
CREATE TABLE IF NOT EXISTS refunds (
    refund_id     TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    refund_amount REAL NOT NULL,               -- 退款金额（元）
    reason        TEXT NOT NULL,               -- 原因：质量问题 / 尺寸不符 / 七天无理由 / 物流损坏
    create_time   TEXT NOT NULL
);

-- 常用查询索引
CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(create_time);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_campaign ON orders(campaign_id);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_items_product ON order_items(product_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_usage_order ON coupon_usage(order_id);
CREATE INDEX IF NOT EXISTS idx_coupons_campaign ON coupons(campaign_id);
