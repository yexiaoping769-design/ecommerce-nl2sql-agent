"""
SQL 安全校验与执行模块（三重防护）：
1. sqlglot 语法树校验：仅允许单条 SELECT 查询（含 WITH CTE），拦截 DML/DDL/多语句
2. EXPLAIN QUERY PLAN 编译检查：提前暴露列名/表名错误（不实际执行）
3. SQLite 只读连接（mode=ro）执行：结果截断保护

设计约定：所有失败返回结构化错误 dict（ok=False + stage + error），不抛异常——
错误信息原样透传，供 Agent 的 Self-Reflection 节点喂回 LLM 修正 SQL。
单测复现：python sql_safe.py
"""
import re
import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "ecommerce.sqlite"

MAX_ROWS = 50  # 返回行数上限，防止超大结果撑爆上下文

# 语句根节点白名单：普通 SELECT / UNION / 子查询（WITH 开头的查询在 sqlglot 中根为 Select）
ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Subquery)
# 语法树内禁止出现的节点类型（key 为 sqlglot 的 snake_case 类型名）
FORBIDDEN_KEYS = {
    "insert", "update", "delete", "drop", "create", "alter", "attach",
    "detach", "pragma", "command", "grant", "merge", "truncate",
}


def validate_sql(sql: str) -> tuple[bool, str]:
    """校验 SQL：必须单条、仅 SELECT、语法树内无危险节点。返回 (ok, 错误说明)"""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return False, "SQL 为空"
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as e:
        return False, f"SQL 语法错误：{e}"
    if len(statements) != 1:
        return False, f"禁止多语句执行（检测到 {len(statements)} 条）"
    tree = statements[0]
    if tree is None:
        return False, "SQL 解析结果为空"
    if not isinstance(tree, ALLOWED_ROOTS):
        return False, f"仅允许 SELECT 查询语句（检测到 {tree.key.upper()}）"
    for node in tree.walk():
        if node.key in FORBIDDEN_KEYS:
            return False, f"禁止使用 {node.key.upper()} 语句，本接口仅支持查询"
    return True, ""


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """SQLite 只读连接（mode=ro）：物理层兜底，连接本身无法写入"""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def explain_check(conn: sqlite3.Connection, sql: str) -> dict:
    """EXPLAIN QUERY PLAN 编译检查：不执行也能暴露列名/表名错误，返回 {ok, error?}"""
    try:
        conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        return {"ok": True}
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}


def execute_readonly(conn: sqlite3.Connection, sql: str, max_rows: int = MAX_ROWS) -> dict:
    """只读执行 + 行数截断。返回结构化结果或错误 dict"""
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return {
            "ok": True,
            "columns": columns,
            "rows": [list(r) for r in rows[:max_rows]],
            "row_count": min(len(rows), max_rows),
            "truncated": truncated,
        }
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}


def safe_run(sql: str, db_path: Path = DB_PATH) -> dict:
    """完整安全执行管线：校验 → EXPLAIN → 只读执行。任何一步失败都返回可喂回 LLM 的结构化错误"""
    sql = sql.strip().rstrip(";").strip()
    ok, err = validate_sql(sql)
    if not ok:
        return {"ok": False, "stage": "validate", "error": err, "sql": sql}
    conn = _connect_ro(db_path)
    try:
        chk = explain_check(conn, sql)
        if not chk["ok"]:
            return {"ok": False, "stage": "explain", "error": chk["error"], "sql": sql}
        result = execute_readonly(conn, sql)
        if not result["ok"]:
            return {"ok": False, "stage": "execute", "error": result["error"], "sql": sql}
        result["sql"] = sql
        return result
    finally:
        conn.close()


def format_error_for_llm(res: dict) -> str:
    """把失败结果整理成 Self-Reflection 修正用的错误说明（喂给 LLM 的文本）"""
    stage_desc = {
        "validate": "SQL 未通过安全校验（仅允许单条 SELECT 查询）",
        "explain": "SQL 编译失败（表名或列名可能有误）",
        "execute": "SQL 执行失败",
    }
    return f"[{stage_desc.get(res['stage'], res['stage'])}] {res['error']}"


if __name__ == "__main__":
    # 正常查询
    r = safe_run("SELECT category, COUNT(*) AS n FROM products GROUP BY category")
    print("[正常查询]", r["ok"], r.get("row_count"), r.get("columns"))

    # WITH CTE + 窗口函数 + REPLACE 函数（不应被误杀）
    r = safe_run("""
        WITH t AS (SELECT order_id, REPLACE(substr(create_time,1,7),'-','/') AS ym FROM orders)
        SELECT ym, COUNT(*) FROM t GROUP BY ym""")
    print("[CTE+REPLACE函数]", r["ok"], r.get("error", ""))

    # 危险语句拦截
    for bad in ["DROP TABLE orders", "DELETE FROM orders", "INSERT INTO products VALUES ('P999','x','数码',1,1)",
                "UPDATE products SET price=0", "ATTACH DATABASE 'x' AS x", "PRAGMA journal_mode=WAL"]:
        r = safe_run(bad)
        print(f"[拦截 {bad.split()[0]}]", r["ok"] is False, "→", format_error_for_llm(r))

    # 多语句
    r = safe_run("SELECT 1; SELECT 2")
    print("[多语句]", r["ok"] is False, "→", format_error_for_llm(r))

    # 列名错误（explain 阶段捕获）
    r = safe_run("SELECT user_nmae FROM users")
    print("[列名错误]", r["stage"] == "explain", "→", format_error_for_llm(r))

    # 空结果（execute 成功但 0 行）
    r = safe_run("SELECT * FROM orders WHERE total_amount > 999999")
    print("[空结果]", r["ok"], r.get("row_count"))
