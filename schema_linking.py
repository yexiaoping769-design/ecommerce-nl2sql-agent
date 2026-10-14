"""
Schema Linking 混合检索模块：
- 知识块三类：表 DDL + 业务用途说明 / 指标口径（metrics.md 行级切分）/ Few-shot SQL 示例
- 双路召回：text2vec 向量语义召回（Qdrant）+ BM25 关键词召回（jieba 分词，专治精确字段名匹配）
- RRF 融合排序后按类型分组返回（tables / metrics / few_shots），供 Agent 组装 Prompt
单测复现：python schema_linking.py
"""
import os

# 国内 HF 镜像，必须在模型库（text2vec/transformers）加载之前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import re
from pathlib import Path

import jieba
import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from rank_bm25 import BM25Okapi

BASE_DIR = Path(__file__).parent
ROOT = BASE_DIR  # 项目根目录
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"
METRICS_PATH = BASE_DIR / "db" / "metrics.md"

COLLECTION = "schema_knowledge"
VECTOR_DIM = 768      # text2vec-base-chinese 输出维度
CANDIDATE_K = 15      # 单路召回候选数
RRF_K = 60            # RRF 融合常数（与茶园项目一致）
CORE_TABLES = ("orders", "order_items")  # 核心事实表保底注入，不依赖检索召回

# 每张表的一句话业务用途（拼进表块文本，增强语义检索命中率）
TABLE_PURPOSE = {
    "users": "用户维度表：城市、注册渠道、会员等级、注册日期。用于新老客判定（注册日期在统计周期内为新客）、用户分群、城市与渠道分析",
    "products": "商品维表：品类（数码/服饰/美妆/食品/家居）、标准售价、当前库存",
    "campaigns": "营销活动表：活动类型（满减/折扣/秒杀）、起止日期、预算。用于活动 GMV 归因与活动 ROI 分析。示例活动名：9月开学季大促、中秋国庆秒杀、夏日清仓特卖。活动名查询用 LIKE 模糊匹配",
    "coupons": "优惠券表：券挂在活动下发放，含使用门槛、优惠值、发放总量与已领取量。用于券效果分析。示例券名：满200减30券、满300减50券、满500减80券、9折券。查询券名时用 LIKE '<关键词>%' 模糊匹配（避免精确匹配带后缀的完整名称）",
    "orders": "订单事实表（销售流水）：实付金额 total_amount（已扣券，GMV 口径）、状态（paid/shipped/completed/refunded）、下单渠道、下单时间、参与活动 campaign_id（未参与为 NULL）",
    "order_items": "订单明细表：订单内每个商品的购买数量与成交单价。商品原价 = quantity × unit_price",
    "coupon_usage": "券核销记录表：一次用券一行，含券抵扣金额 discount_amount 与核销时间。用券订单 = 有核销记录的订单",
    "refunds": "退款事实表：退款金额与退款原因（质量问题/尺寸不符/七天无理由/物流损坏）",
}

# Few-shot SQL 示例库：问题 + 标准答案 SQL + 口径说明（检索命中后注入 Prompt）
FEW_SHOTS = [
    {
        "question": "各品类的退款率是多少？哪个品类退款率最高？",
        "note": "口径关键：分母必须是全部订单的 GMV，用 orders 为主表 LEFT JOIN refunds；订单按主品类归属（ROW_NUMBER 取金额最大明细），避免 JOIN 膨胀与内连接分母缩小两个口径陷阱",
        "sql": """WITH order_cat AS (
  SELECT i.order_id, p.category,
         ROW_NUMBER() OVER (PARTITION BY i.order_id ORDER BY i.quantity * i.unit_price DESC) AS rn
  FROM order_items i JOIN products p ON p.product_id = i.product_id)
SELECT oc.category,
       ROUND(SUM(f.refund_amount) / SUM(o.total_amount) * 100, 1) AS refund_rate
FROM orders o
JOIN order_cat oc ON oc.order_id = o.order_id AND oc.rn = 1
LEFT JOIN refunds f ON f.order_id = o.order_id
GROUP BY oc.category ORDER BY refund_rate DESC""",
    },
    {
        "question": "最近 7 天每天的 GMV 趋势是怎样的？",
        "note": "'最近'以数据集内最大日期为今天，向前推 6 天共 7 天；按下单日期分组汇总实付金额",
        "sql": """WITH latest AS (SELECT MAX(substr(create_time, 1, 10)) AS today FROM orders)
SELECT substr(o.create_time, 1, 10) AS dt, ROUND(SUM(o.total_amount), 2) AS gmv
FROM orders o, latest
WHERE o.create_time >= date(latest.today, '-6 day')
GROUP BY dt ORDER BY dt""",
    },
    {
        "question": "新客贡献了多少 GMV？新老客 GMV 占比是多少？",
        "note": "新客定义：注册日期在数据统计周期内的用户（metrics.md 口径）；统计周期起点取订单表最早日期",
        "sql": """WITH period AS (SELECT MIN(substr(create_time, 1, 10)) AS start_day FROM orders)
SELECT ROUND(SUM(CASE WHEN u.register_time >= p.start_day THEN o.total_amount ELSE 0 END) * 100.0
             / SUM(o.total_amount), 1) AS new_user_gmv_pct
FROM orders o, period p
JOIN users u ON u.user_id = o.user_id""",
    },
    {
        "question": "各营销活动带来了多少 GMV？活动 ROI 是多少？",
        "note": "活动 GMV 只统计参与活动的订单（campaign_id 关联）；ROI = 活动 GMV / 活动预算",
        "sql": """SELECT c.campaign_name, COUNT(DISTINCT o.order_id) AS orders,
       ROUND(SUM(o.total_amount), 2) AS gmv,
       ROUND(SUM(o.total_amount) / c.budget, 2) AS roi
FROM orders o JOIN campaigns c ON c.campaign_id = o.campaign_id
GROUP BY c.campaign_id ORDER BY gmv DESC""",
    },
    {
        "question": "哪些优惠券核销率比较低？券的核销情况怎么样？",
        "note": "核销率 = 核销张数 / 已领取张数；用 LEFT JOIN 保证零核销的券也出现在结果里",
        "sql": """SELECT cp.coupon_name, cp.issued_count, COUNT(u.usage_id) AS used,
       ROUND(COUNT(u.usage_id) * 100.0 / cp.issued_count, 1) AS usage_rate
FROM coupons cp LEFT JOIN coupon_usage u ON u.coupon_id = cp.coupon_id
GROUP BY cp.coupon_id ORDER BY usage_rate""",
    },
    {
        "question": "卖得最好的商品是哪几个？热销商品 Top5",
        "note": "热销按销量件数（SUM(quantity)）排序，不是按销售额（metrics.md 规则）",
        "sql": """SELECT p.product_name, p.category, SUM(i.quantity) AS total_qty
FROM order_items i JOIN products p ON p.product_id = i.product_id
GROUP BY i.product_id ORDER BY total_qty DESC LIMIT 5""",
    },
]

METRIC_ROW_RE = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|", re.M)


def load_schema_chunks(sql_text: str) -> list[dict]:
    """按 CREATE TABLE 语句切分 schema.sql，每张表一个知识块（DDL + 用途说明）"""
    chunks = []
    for m in re.finditer(r"CREATE TABLE[^;]+;", sql_text, re.S):
        ddl = m.group(0).strip()
        name = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl).group(1)
        text = f"【表 {name}】{TABLE_PURPOSE.get(name, '')}\n{ddl}"
        chunks.append({"kind": "table", "text": text,
                       "payload": {"kind": "table", "table": name, "ddl": ddl}})
    return chunks


def load_metric_chunks(md_text: str) -> list[dict]:
    """metrics.md 行级切分：每个指标定义一个知识块，歧义消解规则整体一个知识块"""
    chunks = []
    for name, definition, sql_rule in METRIC_ROW_RE.findall(md_text):
        text = f"【指标 {name}】口径：{definition}。SQL 口径：{sql_rule}"
        chunks.append({"kind": "metric", "text": text,
                       "payload": {"kind": "metric", "text": text}})
    m = re.search(r"##\s*歧义消解规则.*", md_text, re.S)
    if m:
        text = m.group(0).strip()
        chunks.append({"kind": "metric", "text": text,
                       "payload": {"kind": "metric", "text": text}})
    return chunks


def load_few_shot_chunks() -> list[dict]:
    chunks = []
    for i, fs in enumerate(FEW_SHOTS):
        text = f"【示例SQL {i + 1}】{fs['question']}。说明：{fs['note']}"
        chunks.append({"kind": "few_shot", "text": text,
                       "payload": {"kind": "few_shot", "question": fs["question"], "sql": fs["sql"]}})
    return chunks


class SchemaLinker:
    """Schema 元数据 + Few-shot 示例的混合检索器（Agent 各节点共享一个实例）"""
    _model = None  # 类级单例：text2vec 加载耗时数秒，避免重复加载

    def __init__(self):
        self.chunks = (
            load_schema_chunks(SCHEMA_PATH.read_text(encoding="utf-8"))
            + load_metric_chunks(METRICS_PATH.read_text(encoding="utf-8"))
            + load_few_shot_chunks()
        )
        self.client = QdrantClient(path=str(ROOT / "qdrant_storage"))  # 持久化到磁盘，重启不用重建
        self._build()

    @classmethod
    def _get_model(cls):
        if cls._model is None:
            from text2vec import SentenceModel
            cls._model = SentenceModel("shibing624/text2vec-base-chinese")
        return cls._model

    def _encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._get_model().encode(texts, convert_to_numpy=True)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / (norms + 1e-12)).astype("float32")  # L2 归一化，余弦相似等价内积

    def _build(self):
        """全量重建索引（幂等）：Qdrant 向量库 + BM25 内存索引"""
        if self.client.collection_exists(COLLECTION):
            self.client.delete_collection(COLLECTION)
        self.client.create_collection(
            COLLECTION, vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE))
        vecs = self._encode([c["text"] for c in self.chunks])
        self.client.upsert(COLLECTION, points=[
            PointStruct(id=i, vector=v.tolist(), payload=c["payload"])
            for i, (v, c) in enumerate(zip(vecs, self.chunks))
        ])
        self._bm25 = BM25Okapi([list(jieba.cut_for_search(c["text"])) for c in self.chunks])
        self._table_ddl = {c["payload"]["table"]: c["payload"]["ddl"]
                           for c in self.chunks if c["kind"] == "table"}

    def retrieve(self, query: str, top_tables: int = 4, top_metrics: int = 3,
                 top_few_shots: int = 2) -> dict:
        """双路召回 + RRF 融合，按类型分组返回注入 Prompt 的上下文包"""
        # 路 1：向量语义召回
        qv = self._encode([query])[0]
        vec_hits = self.client.query_points(
            COLLECTION, query=qv.tolist(), limit=CANDIDATE_K).points
        vec_rank = {h.id: rank for rank, h in enumerate(vec_hits)}
        # 路 2：BM25 关键词召回
        bm_scores = self._bm25.get_scores(list(jieba.cut_for_search(query)))
        bm_rank = {idx: rank for rank, idx in enumerate(
            sorted(range(len(bm_scores)), key=lambda i: -bm_scores[i])[:CANDIDATE_K])}
        # RRF 融合：score = Σ 1/(k + rank)
        rrf: dict[int, float] = {}
        for pid, rank in vec_rank.items():
            rrf[pid] = rrf.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        for pid, rank in bm_rank.items():
            rrf[pid] = rrf.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        fused = sorted(rrf.items(), key=lambda x: -x[1])
        # 按类型分组截断
        result = {"tables": [], "metrics": [], "few_shots": []}
        seen_tables = set()
        for pid, _score in fused:
            c = self.chunks[pid]
            if c["kind"] == "table" and len(result["tables"]) < top_tables:
                result["tables"].append(c["payload"]["ddl"])
                seen_tables.add(c["payload"]["table"])
            elif c["kind"] == "metric" and len(result["metrics"]) < top_metrics:
                result["metrics"].append(c["payload"]["text"])
            elif c["kind"] == "few_shot" and len(result["few_shots"]) < top_few_shots:
                result["few_shots"].append(
                    {"question": c["payload"]["question"], "sql": c["payload"]["sql"]})
        # 核心事实表保底注入：绝大多数查询都要经过事实表，不赌检索召回
        for t in CORE_TABLES:
            if t not in seen_tables and len(result["tables"]) < top_tables + 2:
                result["tables"].append(self._table_ddl[t])
                seen_tables.add(t)
        return result


if __name__ == "__main__":
    linker = SchemaLinker()
    print(f"知识块总数: {len(linker.chunks)}\n")
    for q in ["近7天退款率最高的3个品类", "新客贡献了多少GMV", "哪张券核销率最低",
              "各营销活动的GMV和ROI", "卖得最好的商品top5"]:
        r = linker.retrieve(q)
        tables = [re.search(r"EXISTS (\w+)", t).group(1) for t in r["tables"]]
        print(f"Q: {q}")
        print(f"  tables   ({len(tables)}): {tables}")
        print(f"  metrics  ({len(r['metrics'])}): {[m[:30] for m in r['metrics']]}")
        print(f"  few_shots({len(r['few_shots'])}): {[f['question'][:24] for f in r['few_shots']]}")
