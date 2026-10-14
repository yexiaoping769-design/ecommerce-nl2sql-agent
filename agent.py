"""
LangGraph 三段式 Agent：Planner（Schema 检索）→ SQLGenerator（生成 SQL + Self-Reflection 纠错）→ ReportGenerator（归因解读）
- LLM：Qwen（通义千问），经 DashScope OpenAI 兼容模式接入（langchain-openai）
- Self-Reflection：SQL 执行失败或返回空结果时，错误信息 + 原始 SQL + Schema 喂回 LLM 修正，最多重试 2 次
- 纠错过程记录在 reflection_log，供前端可视化展示
调用：run_agent("近7天退款率最高的3个品类") → {answer, sql, exec_result, reflection_log}
"""
import os
import re
from typing import Literal, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

from schema_linking import SchemaLinker
from sql_safe import format_error_for_llm, safe_run

load_dotenv()

MAX_RETRIES = 2  # Self-Reflection 最大纠错次数

SYSTEM_PROMPT = (
    "你是电商数据分析师，负责把用户的自然语言问题转成一条 SQLite 查询语句。"
    "只能使用给定表结构中的表和列；指标口径必须严格遵守给定的指标定义；"
    "金额保留 2 位小数，百分比保留 1 位小数；"
    "字符串精确查询时，若候选值可能带后缀（如券名带'券'、活动名带时间/类型后缀），"
    "优先用 LIKE '<关键词>%' 前缀模糊匹配代替 = 精确匹配；"
    "只输出一条 SELECT 查询，用 ```sql 代码块包裹，不要输出任何解释文字。"
)


class AgentState(TypedDict):
    question: str
    plan: dict                 # Schema Linking 检索结果 {tables, metrics, few_shots}
    sql: str
    exec_result: dict          # safe_run 执行结果
    attempts: int              # 已纠错次数
    reflection_log: list[str]  # 纠错过程记录（前端可视化）
    answer: str


_LLM = None
_LINKER = None


def get_llm() -> ChatOpenAI:
    global _LLM
    if _LLM is None:
        key = os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY：请复制 .env.example 为 .env 并填入通义千问 API Key")
        _LLM = ChatOpenAI(
            model=os.getenv("QWEN_MODEL", "qwen-plus"),
            api_key=key,
            base_url=os.getenv(
                "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0,
        )
    return _LLM


def get_linker() -> SchemaLinker:
    global _LINKER
    if _LINKER is None:
        _LINKER = SchemaLinker()
    return _LINKER


def extract_sql(text: str) -> str:
    """从 LLM 输出中提取 SQL（兼容 ```sql 包裹与裸文本）"""
    m = re.search(r"```sql\s*(.*?)\s*```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*?)\s*```", text, re.S)
    if m:
        return m.group(1).strip()
    return text.strip()


# ===== 节点 1：Planner —— Schema Linking 检索 =====
def planner_node(state: AgentState) -> dict:
    plan = get_linker().retrieve(state["question"])
    return {"plan": plan, "attempts": 0, "reflection_log": []}


# ===== 节点 2：SQLGenerator —— 生成 / 纠错 =====
def build_sql_prompt(question: str, plan: dict, retry_info: dict | None = None) -> str:
    ddl = "\n\n".join(plan["tables"])
    metrics = "\n".join(f"- {m}" for m in plan["metrics"]) or "- （无匹配指标，按通用口径）"
    shots = "\n\n".join(
        f"问题：{f['question']}\n```sql\n{f['sql']}\n```" for f in plan["few_shots"])
    prompt = (
        f"【候选表结构（只允许使用以下表和列）】\n{ddl}\n\n"
        f"【指标口径（必须遵守）】\n{metrics}\n\n"
        f"【参考示例（同类型问题的标准写法）】\n{shots}\n\n"
        f"【用户问题】\n{question}\n\n"
        "请生成查询 SQL。"
    )
    if retry_info:
        prompt += (
            f"\n\n【上次执行失败，请修正后重新输出】\n"
            f"上次 SQL：\n```sql\n{retry_info['sql']}\n```\n"
            f"失败原因：{retry_info['error']}\n"
            "请仔细检查列名、表名、日期格式与指标口径，输出修正后的 SQL。"
        )
    return prompt


def sql_generator_node(state: AgentState) -> dict:
    er = state.get("exec_result")
    retry_info = None
    if er is not None:
        if not er.get("ok"):
            retry_info = {"sql": er.get("sql", state["sql"]),
                          "error": format_error_for_llm(er)}
        elif er.get("row_count", 0) == 0:
            retry_info = {
                "sql": er.get("sql", state["sql"]),
                "error": ("查询成功但返回 0 行。请检查：日期比较格式"
                          "（create_time 为 'YYYY-MM-DD HH:MM' 文本）、过滤条件是否过严、"
                          "口径是否符合指标定义")}
    prompt = build_sql_prompt(state["question"], state["plan"], retry_info)
    resp = get_llm().invoke([SystemMessage(content=SYSTEM_PROMPT),
                             HumanMessage(content=prompt)])
    sql = extract_sql(resp.content)
    log = state.get("reflection_log", [])
    if retry_info:
        log = log + [f"第 {state['attempts']} 次纠错：{retry_info['error'][:60]}…"]
    return {"sql": sql, "reflection_log": log}


# ===== 节点 3：SQLExecutor —— 安全校验 + 只读执行 =====
def sql_executor_node(state: AgentState) -> dict:
    res = safe_run(state["sql"])
    bad = (not res.get("ok")) or res.get("row_count", 0) == 0
    if bad:
        return {"exec_result": res, "attempts": state.get("attempts", 0) + 1}
    return {"exec_result": res}


def route_after_exec(state: AgentState) -> Literal["retry", "report"]:
    er = state["exec_result"]
    bad = (not er.get("ok")) or er.get("row_count", 0) == 0
    if bad and state.get("attempts", 0) < MAX_RETRIES:
        return "retry"
    return "report"


# ===== 节点 4：ReportGenerator —— 归因解读 =====
def report_node(state: AgentState) -> dict:
    er = state["exec_result"]
    if not er.get("ok"):
        return {"answer": (
            f"查询未能成功执行：{er['error']}。"
            f"已自动尝试 {MAX_RETRIES} 次修正仍未解决，建议换一种问法再试。")}
    if er.get("row_count", 0) == 0:
        return {"answer": "查询成功但没有符合条件的数据，请尝试调整时间范围或筛选条件。"}

    result_json = {
        "columns": er["columns"],
        "rows": er["rows"],
        "truncated": er.get("truncated", False),
    }
    prompt = (
        f"【用户问题】\n{state['question']}\n\n"
        f"【执行的 SQL】\n```sql\n{er['sql']}\n```\n\n"
        f"【查询结果】\n{result_json}\n\n"
        "请基于以上查询结果生成回答，要求：\n"
        "1. 直接给出结论，必须引用结果中的具体数字（金额 2 位小数、百分比 1 位小数）\n"
        "2. 给出简短的归因或业务解读（如品类差异、活动影响、渠道特点）\n"
        "3. 注明统计口径（如「按实付金额 GMV 口径，未扣除退款」）\n"
        "4. 简洁的中文，适合业务人员直接阅读，不要逐行复述表格"
    )
    resp = get_llm().invoke([
        SystemMessage(content=(
            "你是电商数据分析师，负责向业务人员解读查询结果。"
            "只依据给出的查询结果作答，不编造数据；结果为空或异常时如实说明。")),
        HumanMessage(content=prompt)])
    return {"answer": resp.content}


# ===== 图编排 =====
def build_app():
    graph = StateGraph(AgentState)
    graph.add_node("planner", planner_node)
    graph.add_node("sql_generator", sql_generator_node)
    graph.add_node("sql_executor", sql_executor_node)
    graph.add_node("report", report_node)
    graph.set_entry_point("planner")
    graph.add_edge("planner", "sql_generator")
    graph.add_edge("sql_generator", "sql_executor")
    graph.add_conditional_edges("sql_executor", route_after_exec,
                                {"retry": "sql_generator", "report": "report"})
    graph.add_edge("report", END)
    return graph.compile()


_APP = None


def get_app():
    global _APP
    if _APP is None:
        _APP = build_app()
    return _APP


def run_agent(question: str) -> dict:
    """对外入口：自然语言问题 → {answer, sql, exec_result, reflection_log}"""
    final = get_app().invoke({"question": question})
    return {
        "question": question,
        "sql": final.get("sql", ""),
        "exec_result": final.get("exec_result", {}),
        "reflection_log": final.get("reflection_log", []),
        "answer": final.get("answer", ""),
    }


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        print("请先配置 .env（复制 .env.example 并填入 DASHSCOPE_API_KEY）")
    else:
        for q in ["近7天退款率最高的3个品类", "各营销活动的GMV和ROI是多少"]:
            print("=" * 70)
            print("Q:", q)
            r = run_agent(q)
            print("SQL:", r["sql"])
            print("纠错记录:", r["reflection_log"])
            print("回答:", r["answer"])
