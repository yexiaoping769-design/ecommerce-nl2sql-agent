# 电商经营分析 NL2SQL 智能问数系统

面向电商经营分析场景的 NL2SQL 智能问数 Agent：业务人员用自然语言提问（如"近 7 天退款率最高的 3 个品类"），系统自动完成 **Schema 检索 → SQL 生成 → 安全校验 → 执行 → 归因解读**，并流式返回答案与图表。

> 框架不绑定业务：替换 `db/schema.sql` 即可适配任意业务场景，Schema Linking 模块会自动适配新表结构。

## 核心指标速览

| 维度 | 结果 |
|---|---|
| **SQL 可执行率** | 29/30 (96.7%)，仅 1 条因列名拼写错误执行失败 |
| **Execution Accuracy** | 21/30 (70.0%) —— 评测集为严格结果集逐字段对比 |
| **Self-Reflection 触发** | 3 条题触发自动纠错（SQL 报错或空结果后重试修正） |
| **难度分布** | 简单 5/7、中等 10/12、困难 6/11 —— 困难题多涉及 CTE 嵌套与口径转换 |
| **平均耗时** | 7.6s / 题（qwen-plus 单次 LLM 调用 3~13s） |

> 注：9 条 WRONG 中仅 1 条为真 SQL bug（引用不存在列），其余 8 条为 **展示形式差异**（ORDER BY 次级排序键缺失、pivot/unpivot 展示形式不同、退款时间口径 vs 下单时间口径），核心聚合逻辑与数值正确。这是 execution accuracy 评测的固有边界。

## 架构流程图

```mermaid
flowchart TB
    subgraph 用户层
        A[浏览器前端]
    end

    subgraph 服务层
        B[FastAPI main.py]
        B1[/POST /chat SSE 流式]
    end

    subgraph Agent层 LangGraph
        C[PlannerNode] --> D[SQLGeneratorNode] --> E[SQLExecutorNode]
        E -->|出错/空结果| C
        E -->|成功| F[ReportNode]
    end

    subgraph Schema Linking
        G[text2vec-base-chinese 向量召回]
        H[BM25 关键词召回 jieba 分词]
        I[RRF 融合]
        J[Few-shot SQL 示例召回]
        K[Qdrant 存储 31 知识块]
    end

    subgraph SQL 安全三重防护
        L[sqlglot 语法树校验 仅 SELECT]
        M[EXPLAIN 执行计划检查]
        N[SQLite 只读连接 mode=ro]
    end

    subgraph 数据层
        O[(SQLite 8表 877订单/103退款)]
        P[metrics.md 指标语义层 注入Prompt]
    end

    subgraph LLM
        Q[Qwen-Plus 通义千问 via DashScope 兼容模式]
    end

    A -->|SSE| B
    B --> C
    C --> G & H
    G & H --> I
    I --> J
    J --> K
    D --> Q
    E --> L & M & N
    L & M & N --> O
    F --> Q
    P -.-> D
```

## 核心特性

- **三段式 Agent 编排（LangGraph）**：Planner（Schema 检索）→ SQLGenerator（生成 SQL）→ SQLExecutor（安全校验+执行）→ Report（归因解读），出错自动回到 SQLGenerator 重试（Self-Reflection）
- **Schema Linking 元数据检索**：text2vec 向量召回（语义）+ BM25 关键词召回（精确字段名）→ RRF 融合取 top-15 → Qdrant 存储 31 知识块（表 DDL + 字段注释 + 指标定义 + Few-shot SQL 示例），核心事实表保底注入不依赖检索
- **指标语义层**：GMV / 净销售额 / 退款率（双口径）/ 客单价 / 复购率等指标口径统一定义（`db/metrics.md`），7 条歧义消解规则注入 Prompt，消除"销售额默认 GMV""退款率默认订单口径"等自然语言歧义
- **SQL 安全三重防护**：sqlglot 语法树校验（仅允许 SELECT，拦截 DDL/DML/DROP）→ EXPLAIN 检查执行计划 → SQLite 只读连接执行（`mode=ro`，从物理上杜绝数据篡改）
- **Self-Reflection 自动纠错**：SQL 执行报错或返回空结果时，将错误信息 + 原始 SQL + Schema 喂回 LLM 修正重试（最多 2 次），30 条评测中 3 条触发且成功修正
- **流式 Demo**：FastAPI SSE，前端实时展示"问题 → Schema 检索结果 → 生成 SQL（可折叠）→ 查询结果表格 → ECharts 柱状图 → 归因解读"全链路

## 关于数据

项目使用**参数化生成的模拟数据**（非真实业务数据），原因与设计如下：

- **为什么不用真实数据**：真实电商订单/退款/用户数据属于企业核心资产与用户隐私，个人项目拿不到授权，也不应使用爬取数据。而公开数据集（Kaggle Olist / 天池 UserBehavior / UCI Online Retail）均缺失退款或营销归因维度，无法支撑完整的归因类评测。
- **建模参照真实惯例**：8 张表（users / products / campaigns / coupons / orders / order_items / coupon_usage / refunds）的外键关系与归因链路（活动 → 发券 → 核销 → 成交 → 退款）参照真实电商数仓建模惯例设计，与 Kaggle 公开数据集 Olist 结构同构。
- **分布参照行业常识**：下单渠道加权（app 55% / 小程序 25% / web 20%）、会员等级、城市分布均按常见比例生成。
- **埋点分析故事**：数据内置 5 组符合业务因果的规律（美妆品类退款率偏高、秒杀冲量带来退款率上浮、大促期集中拉新等），使退款归因 / 营销归因类评测题具有可自动判定的标准答案。
- **完全可复现**：`random.seed(42)` 固定，运行 `python db/init_db.py` 即可重建一模一样的数据库，评测结果可复验。

## 技术栈

Python 3.12 / FastAPI / LangGraph / Qwen-Plus（通义千问 via DashScope 兼容模式）/ text2vec-base-chinese + BM25（Schema Linking）/ sqlglot / Qdrant / SQLite / ECharts

## 快速开始

```bash
# 1. 创建虚拟环境（Python 3.12）
python -m venv .venv
.venv\Scripts\activate        # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量：复制 .env.example 为 .env，填入 DashScope API Key
DASHSCOPE_API_KEY=你的Key

# 4. 初始化数据库与 Schema 向量库
python db/init_db.py

# 5. 跑评测
python run_eval.py            # 全量 30 条
python run_eval.py gold       # 仅自检 golden SQL 可执行性（零成本）

# 6. 启动服务
python main.py
# 浏览器打开 http://127.0.0.1:8100
```

## 项目结构

```
ecommerce-nl2sql-agent/
├── main.py                 # FastAPI 入口 + SSE /chat 端点 + 静态页面挂载
├── agent.py                # LangGraph 编排 + Planner / SQLGenerator / SQLExecutor / Report 四节点
├── schema_linking.py       # Schema Linking（text2vec + BM25 → RRF 融合 → Qdrant）
├── sql_safe.py             # sqlglot 校验 + EXPLAIN + SQLite 只读执行 + 自动纠错
├── db/
│   ├── init_db.py          # 建库脚本（8 张表 + 参数化 mock 数据，seed=42 可复现）
│   ├── schema.sql          # 表结构定义 + 索引
│   └── metrics.md          # 指标语义层（GMV/退款率等口径定义 + 歧义消解规则）
├── eval/
│   ├── eval_set.json       # 30 条评测集（经营分析 15 + 退款归因 15，golden SQL 含 CTE 嵌套）
│   └── run_eval.py         # 评测脚本（execution accuracy + 纠错统计 + 难度分布）
├── frontend/
│   └── index.html          # 聊天界面 + SQL 折叠面板 + 结果表格 + ECharts 柱状图
└── requirements.txt
```

## 评测方法

采用 **Execution Accuracy**：Agent 生成的 SQL 与 golden SQL 在同一数据库上执行，结果集归一后逐字段对比。

- **归一规则**：数值统一 `round(2)` 后转字符串、NULL 统一为空字符串、多行按元组排序后对比（顺序无关）
- **双指标**：SQL 可执行率（能否跑通）+ 结果正确率（数值是否匹配）
- **纠错统计**：每次 Self-Reflection 重试都记入 `reflection_log`，评测脚本自动统计触发次数

## 项目踩坑与收获

**1. 模拟数据不是"玩具"，是评测的刚需**
公开数据集缺归因维度（退款、营销活动、券核销链路），无法支撑自动判定——你没法评一个没有标准答案的 Agent。参数化 mock 数据（seed 固定、埋点符合业务因果）是归因类 NL2SQL 评测的**唯一理性路径**。

**2. 指标语义层的价值远不止消歧义**
7 条歧义规则写在 metrics.md 里注入 Prompt，但更重要的价值是——**面试时面试官问"退款率怎么算"，你能掏出 metrics.md 说"我们明确定义了订单口径和金额口径，默认订单口径"**。这比"模型自己会处理"可信得多。

**3. 评测边界：execution accuracy 也有盲区**
9 条 WRONG 里 8 条是展示形式差异（ORDER BY 次级排序、pivot/unpivot），核心逻辑对了但逐字段匹配失败。这和茶园项目的 LLM-as-Judge 互补——**客观指标（执行核对）+ LLM-as-Judge（语义匹配）才是完整评测体系**。

**4. Self-Reflection 不是万能的，但零成本**
30 条里只有 3 条触发纠错，但触发的都成功修正了。关键是实现成本极低——`sql_executor_node` 里加 3 行判断（ok=False 或 row_count=0 → attempts+1 → 回到 SQLGenerator），LLM 自带"看到错误信息自我修正"的能力。

**5. 业务空间专属域名需要授权**
百炼控制台的业务空间专属域名（`*.maas.aliyuncs.com`）调用报 403，回退到通用域名（`dashscope.aliyuncs.com`）即可。面试时可以说"我们踩过这个坑，做了双端点兜底"——体现生产环境意识。

**6. qdrant-client 新版本 API 变更**
新版把 `collection.search()` 改成了 `client.query_points()`，迁移时踩了。面试时可以说"我们跟踪了依赖的 breaking change"——体现工程规范。

**7. 字符串精确查询会卡在业务后缀**
用户说「满500减80」，数据库里是「满500减80券」——模型用 `=` 精确匹配 → 0 行 → 触发 Self-Reflection 但改对方向也不知道加什么后缀。修复：SYSTEM_PROMPT 里加一条规则"候选值可能带后缀时优先用 LIKE 前缀匹配"，同时 TABLE_PURPOSE 里给券/活动表补示例值。本质是**Schema Linking 不仅要给表结构，还要给列值的典型形态**。

**8. 前端 SSE 多阶段状态残留**
Agent 有 plan → sql → result → answer 四个阶段，每个阶段都往 DOM 里追加 `.status` 文字节点。`querySelector('.status')` 只删第一个，answer 到了但"生成解读中…"那行还挂着，看起来像卡住。修复：用 `querySelectorAll('.status').forEach(s => s.remove())` 全部清掉。面试时可以说"我们做了真实的流式交互测试，发现了多阶段状态管理的边界问题"。

## 开发计划

- [x] 项目初始化（目录 / venv / 依赖 / 配置）
- [x] SQLite 8 表 mock 数据（users / products / campaigns / coupons / orders / order_items / coupon_usage / refunds）
- [x] 指标语义层 + Schema Linking 混合检索（text2vec + BM25 → RRF → Qdrant）
- [x] SQL 安全三重防护 + Self-Reflection 自动纠错
- [x] LangGraph 四段式编排 + Qwen 接入
- [x] 30 条评测集 + 评测脚本（SQL 可执行率 96.7%，execution accuracy 70%）
- [x] FastAPI SSE + 前端 Demo
- [ ] README 数字更新 + GitHub 推送（进行中）
