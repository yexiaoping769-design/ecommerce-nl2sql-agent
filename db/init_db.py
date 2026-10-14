"""
初始化 SQLite demo 数据库：建表 + 生成 mock 数据。
- 固定随机种子(42)，数据可复现
- 时间跨度 90 天（2026-07-13 ~ 2026-10-10）
- 8 张表：users / products / campaigns / coupons / orders / order_items / coupon_usage / refunds
- 内置分析故事（供归因类评测题使用）：
  1. 美妆品类退款率偏高（0.22，其他品类 0.04~0.10）
  2. 秒杀活动订单退款率上浮 1.8 倍 —— 冲量带来退款反噬
  3. 9 月开学季大促：活动期间订单量上浮 + 集中拉新 12 人
  4. 新客集中在大促期间注册，支持新老客 GMV 占比归因
用法：python db/init_db.py
"""
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "ecommerce.sqlite"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

random.seed(42)

CATEGORIES = {
    "数码": [("无线蓝牙耳机", 299), ("智能手环", 499), ("充电宝", 129), ("机械键盘", 399)],
    "服饰": [("纯棉T恤", 89), ("牛仔裤", 259), ("羽绒服", 899), ("运动鞋", 429)],
    "美妆": [("保湿面霜", 219), ("防晒霜", 159), ("口红", 189), ("香水", 459)],
    "食品": [("坚果礼盒", 99), ("咖啡豆", 79), ("巧克力", 69), ("茶叶礼盒", 199)],
    "家居": [("四件套", 349), ("收纳箱", 59), ("台灯", 149), ("加湿器", 199)],
}
CHANNELS = ["app", "web", "mini_program"]
CHANNEL_WEIGHTS = [0.55, 0.2, 0.25]
REASONS = ["质量问题", "尺寸不符", "七天无理由", "物流损坏"]
# 每个品类的退款概率（美妆故意偏高 → 归因分析有故事）
CATEGORY_REFUND_RATE = {"数码": 0.05, "服饰": 0.10, "美妆": 0.22, "食品": 0.04, "家居": 0.07}

CITIES = ["福州", "厦门", "杭州", "上海", "深圳", "广州", "成都", "武汉", "南京", "北京"]
SOURCE_CHANNELS = ["app", "web", "mini_program", "offline"]
SOURCE_WEIGHTS = [0.45, 0.15, 0.30, 0.10]
MEMBER_LEVELS = ["normal", "silver", "gold"]
MEMBER_WEIGHTS = [0.60, 0.25, 0.15]

TODAY = datetime(2026, 10, 10)
DATA_START = TODAY - timedelta(days=89)  # 2026-07-13

# (campaign_id, 名称, 类型, 开始, 结束, 预算)
CAMPAIGNS = [
    ("C001", "夏日清仓特卖", "折扣", "2026-07-15", "2026-07-31", 8000),
    ("C002", "9月开学季大促", "满减", "2026-09-01", "2026-09-14", 20000),
    ("C003", "中秋国庆秒杀", "秒杀", "2026-09-25", "2026-10-05", 15000),
]
# (coupon_id, campaign_id, 名称, 类型, 门槛, 优惠值, 发放总量)
COUPONS = [
    ("CP001", "C001", "满100减15券", "满减", 100, 15, 500),
    ("CP002", "C002", "满200减30券", "满减", 200, 30, 800),
    ("CP003", "C002", "满500减80券", "满减", 500, 80, 300),
    ("CP004", "C003", "9折秒杀券", "折扣", 0, 0.9, 600),
]


def gen_users(cur):
    """40 个老客（数据期前注册）+ 12 个大促拉新（9/1~9/14）+ 8 个日常新客"""
    rows = []
    for i in range(1, 61):
        if i <= 40:  # 老客：2026-01-01 ~ 2026-07-10
            reg = datetime(2026, 1, 1) + timedelta(days=random.randint(0, 190))
        elif i <= 52:  # 大促拉新：开学季活动期注册
            reg = datetime(2026, 9, 1) + timedelta(days=random.randint(0, 13))
        else:  # 日常新客：数据期内、非活动期
            reg = DATA_START + timedelta(days=random.randint(0, 49))
        rows.append((f"U{i:04d}", random.choice(CITIES),
                     random.choices(SOURCE_CHANNELS, weights=SOURCE_WEIGHTS)[0],
                     random.choices(MEMBER_LEVELS, weights=MEMBER_WEIGHTS)[0],
                     f"{reg:%Y-%m-%d}"))
    cur.executemany("INSERT INTO users VALUES (?,?,?,?,?)", rows)
    return {r[0]: r[4] for r in rows}  # user_id -> 注册日期


def gen_campaigns_coupons(cur):
    cur.executemany("INSERT INTO campaigns VALUES (?,?,?,?,?,?)", CAMPAIGNS)
    coupon_rows = [
        (cid, cam, name, ctype, th, val, total, 0, start, end)
        for cid, cam, name, ctype, th, val, total in COUPONS
        for start, end in [(c[3], c[4]) for c in CAMPAIGNS if c[0] == cam]
    ]
    cur.executemany("INSERT INTO coupons VALUES (?,?,?,?,?,?,?,?,?,?)", coupon_rows)


def gen_products(cur):
    rows = []
    pid = 0
    for cat, items in CATEGORIES.items():
        for name, price in items:
            pid += 1
            rows.append((f"P{pid:03d}", name, cat, price, random.randint(30, 500)))
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?)", rows)
    return {r[0]: r for r in rows}


def main():
    # 全量重建：先删除旧库，避免 CREATE TABLE IF NOT EXISTS 沿用旧表结构
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    cur = conn.cursor()

    users_reg = gen_users(cur)
    gen_campaigns_coupons(cur)
    products = gen_products(cur)

    orders, items, refunds, usages = [], [], [], []
    order_seq = item_seq = refund_seq = usage_seq = 0
    product_ids = list(products.keys())

    for day in range(90):
        d = DATA_START + timedelta(days=day)
        ds = f"{d:%Y-%m-%d}"
        # 当天是否有进行中的活动
        active = next((c for c in CAMPAIGNS if c[3] <= ds <= c[4]), None)
        # 活动期间订单量上浮（活动驱动增长的归因故事）
        daily = random.randint(5, 12) + (4 if active else 0)
        # 当日可用用户（注册时间 <= 当日）
        avail = [uid for uid, reg in users_reg.items() if reg <= ds]

        for _ in range(daily):
            order_seq += 1
            oid = f"O{ds.replace('-', '')}{order_seq:04d}"
            ts = f"{ds} {random.randint(8, 22):02d}:{random.randint(0, 59):02d}"
            channel = random.choices(CHANNELS, weights=CHANNEL_WEIGHTS)[0]
            uid = random.choice(avail)

            # 60% 的活动期订单参与活动
            cid = active[0] if active and random.random() < 0.6 else None

            # 每笔订单 1~3 个商品
            picked = random.sample(product_ids, k=random.randint(1, 3))
            goods = 0.0
            cats = set()
            for pid in picked:
                item_seq += 1
                _, name, cat, price, _ = products[pid]
                qty = random.choices([1, 2, 3], weights=[0.7, 0.2, 0.1])[0]
                # 成交价 = 标准价 × 随机折扣(0.8~1.0)
                paid = round(price * qty * random.uniform(0.8, 1.0), 2)
                goods += paid
                cats.add(cat)
                items.append((f"I{item_seq:06d}", oid, pid, qty, round(paid / qty, 2)))
            goods = round(goods, 2)

            # 用券：参与活动的订单 45% 概率使用该活动下的可用券
            discount = 0.0
            if cid and random.random() < 0.45:
                cands = [c for c in COUPONS if c[1] == cid and goods >= c[4]]
                if cands:
                    cp_id, _, _, ctype, _, val, *_ = random.choice(cands)
                    discount = val if ctype == "满减" else round(goods * (1 - val), 2)
                    usage_seq += 1
                    usages.append((f"CU{usage_seq:05d}", cp_id, uid, oid, discount, ts))
            total = round(goods - discount, 2)

            # 退款判定：按订单内品类取最高退款率；秒杀活动冲量 → 退款率上浮 1.8 倍
            rate = max(CATEGORY_REFUND_RATE[c] for c in cats)
            if cid == "C003":
                rate = min(rate * 1.8, 0.9)
            if random.random() < rate:
                refund_seq += 1
                refunded = round(total * random.uniform(0.5, 1.0), 2)
                # 退款发生在下单后 1~3 天
                rd = d + timedelta(days=random.randint(1, 3))
                refunds.append((f"R{refund_seq:05d}", oid, refunded,
                                random.choice(REASONS), f"{rd:%Y-%m-%d} 10:00"))
                status = "refunded"
            else:
                # 非退款订单状态按时间推进
                if (TODAY - d).days <= 2:
                    status = random.choice(["paid", "shipped"])
                else:
                    status = random.choice(["shipped", "completed", "completed"])
            orders.append((oid, uid, cid, total, status, channel, ts))

    cur.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)
    cur.executemany("INSERT INTO coupon_usage VALUES (?,?,?,?,?,?)", usages)
    cur.executemany("INSERT INTO refunds VALUES (?,?,?,?,?)", refunds)

    # 回填券领取量：核销量 + 未核销余量（模拟大量领取未核销）
    for cp_id, _, _, _, _, _, total_cnt, *_ in COUPONS:
        used = cur.execute(
            "SELECT COUNT(*) FROM coupon_usage WHERE coupon_id=?", (cp_id,)
        ).fetchone()[0]
        issued = min(total_cnt, used + random.randint(50, 300))
        cur.execute("UPDATE coupons SET issued_count=? WHERE coupon_id=?", (issued, cp_id))

    conn.commit()

    # ===== 自检输出 =====
    for t in ("users", "products", "campaigns", "coupons",
              "orders", "order_items", "coupon_usage", "refunds"):
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n} 行")

    gmv = cur.execute("SELECT ROUND(SUM(total_amount),2) FROM orders").fetchone()[0]
    rf = cur.execute("SELECT ROUND(SUM(refund_amount),2) FROM refunds").fetchone()[0]
    print(f"\nGMV 总额: {gmv} | 退款总额: {rf} | 金额退款率: {rf / gmv:.1%}")

    print("\n[埋点1] 品类退款率 top3（金额口径）:")
    for row in cur.execute("""
        WITH order_cat AS (
          SELECT i.order_id, p.category,
                 ROW_NUMBER() OVER (PARTITION BY i.order_id ORDER BY i.quantity*i.unit_price DESC) rn
          FROM order_items i JOIN products p ON p.product_id = i.product_id)
        SELECT oc.category,
               ROUND(SUM(f.refund_amount) / SUM(o.total_amount) * 100, 1) AS refund_rate
        FROM orders o
        JOIN order_cat oc ON oc.order_id = o.order_id AND oc.rn = 1
        LEFT JOIN refunds f ON f.order_id = o.order_id
        GROUP BY oc.category ORDER BY refund_rate DESC LIMIT 3"""):
        print(f"  {row[0]}: {row[1]}%")

    print("\n[埋点2] 秒杀 vs 非活动订单退款率（订单口径）:")
    for label, cond in [("秒杀活动(C003)", "campaign_id='C003'"),
                        ("非活动订单", "campaign_id IS NULL")]:
        n, r = cur.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN status='refunded' THEN 1 ELSE 0 END) "
            f"FROM orders WHERE {cond}").fetchone()
        print(f"  {label}: {r}/{n} = {r / n:.1%}")

    print("\n[埋点3] 各活动 GMV 贡献:")
    for row in cur.execute("""
        SELECT c.campaign_name, COUNT(*) AS orders,
               ROUND(SUM(o.total_amount), 2) AS gmv
        FROM orders o JOIN campaigns c ON c.campaign_id = o.campaign_id
        GROUP BY c.campaign_name"""):
        print(f"  {row[0]}: {row[1]} 单 / {row[2]} 元")

    print("\n[埋点4] 新客（数据期内注册）GMV 占比:")
    new_gmv, all_gmv = cur.execute("""
        SELECT ROUND(SUM(CASE WHEN u.register_time >= ? THEN o.total_amount ELSE 0 END), 2),
               ROUND(SUM(o.total_amount), 2)
        FROM orders o JOIN users u ON u.user_id = o.user_id""",
        (f"{DATA_START:%Y-%m-%d}",)).fetchone()
    print(f"  新客 GMV {new_gmv} / 总 GMV {all_gmv} = {new_gmv / all_gmv:.1%}")

    print("\n[埋点5] 券核销情况:")
    for row in cur.execute("""
        SELECT cp.coupon_name, cp.issued_count, COUNT(u.usage_id) AS used
        FROM coupons cp LEFT JOIN coupon_usage u ON u.coupon_id = cp.coupon_id
        GROUP BY cp.coupon_id"""):
        rate = row[2] / row[1] if row[1] else 0
        print(f"  {row[0]}: 领取 {row[1]} / 核销 {row[2]} = {rate:.1%}")

    conn.close()
    print(f"\n数据库已生成: {DB_PATH}")


if __name__ == "__main__":
    main()
