# -*- coding: utf-8 -*-
"""
评测脚本：30 条评测集 execution accuracy 评测
用法:
    python run_eval.py gold   # 仅自检 golden SQL 可执行（不调 LLM、零成本）
    python run_eval.py 3      # 跑前 3 条（调试）
    python run_eval.py        # 全量 30 条
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db" / "ecommerce.sqlite"
EVAL_SET = ROOT / "eval" / "eval_set.json"
RESULT_PATH = ROOT / "eval" / "eval_results.json"


def exec_sql(sql: str):
    """只读连接执行 SQL，返回 (rows, error)"""
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall(), None
    except Exception as e:
        return None, str(e)
    finally:
        con.close()


def norm_cell(v):
    """单元格归一：数值统一 round(2)，其余转字符串"""
    if isinstance(v, (int, float)):
        return round(float(v) + 0.0, 2)
    return str(v).strip()


def norm_rows(rows):
    """结果集归一为排序后的多重集（顺序无关对比）"""
    return sorted(tuple(norm_cell(c) for c in row) for row in (rows or []))


def gold_check(cases):
    """golden SQL 自检：全部可执行且非空"""
    ok = 0
    for c in cases:
        rows, err = exec_sql(c["golden_sql"])
        if err or not rows:
            print(f"  [FAIL] #{c['id']} {c['question']} -> {err or '空结果'}")
        else:
            ok += 1
            print(f"  [OK]  #{c['id']:2d} {c['question']}  ({len(rows)} 行)")
    print(f"\n== golden 自检: {ok}/{len(cases)} 可执行 ==")
    return ok == len(cases)


def run_eval(cases):
    from agent import run_agent

    results = []
    for c in cases:
        t0 = time.time()
        try:
            r = run_agent(c["question"])
            agent_sql = r["sql"]
            reflections = len(r.get("reflection_log", []))
        except Exception as e:
            results.append({"id": c["id"], "question": c["question"],
                            "error": f"AGENT_CRASH: {e}", "correct": False,
                            "executable": False, "reflections": 0,
                            "seconds": round(time.time() - t0, 1)})
            print(f"  [CRASH] #{c['id']} {e}")
            continue
        seconds = round(time.time() - t0, 1)

        gold_rows, _ = exec_sql(c["golden_sql"])
        executable, correct, got_rows, err = False, False, None, None
        if agent_sql:
            got_rows, err = exec_sql(agent_sql)
            executable = err is None
            if executable:
                correct = norm_rows(got_rows) == norm_rows(gold_rows)

        tag = "PASS" if correct else ("EXEC_FAIL" if not executable else "WRONG")
        results.append({
            "id": c["id"], "category": c["category"], "difficulty": c["difficulty"],
            "question": c["question"], "golden_sql": c["golden_sql"],
            "agent_sql": agent_sql, "agent_error": err,
            "executable": executable, "correct": correct,
            "reflections": reflections, "seconds": seconds, "tag": tag,
        })
        print(f"  [{tag:9s}] #{c['id']:2d} {seconds:5.1f}s 纠错{reflections}次  {c['question']}")

    # ===== 汇总 =====
    total = len(results)
    n_exec = sum(1 for x in results if x["executable"])
    n_correct = sum(1 for x in results if x["correct"])
    n_reflect = sum(1 for x in results if x.get("reflections", 0) > 0)
    by_diff = {}
    for x in results:
        by_diff.setdefault(x["difficulty"], [0, 0])
        by_diff[x["difficulty"]][0] += 1
        by_diff[x["difficulty"]][1] += 1 if x["correct"] else 0

    summary = {
        "total": total,
        "executable": n_exec,
        "exec_rate": round(n_exec / total * 100, 1),
        "correct": n_correct,
        "accuracy": round(n_correct / total * 100, 1),
        "reflection_triggered": n_reflect,
        "avg_seconds": round(sum(x["seconds"] for x in results) / total, 1),
        "by_difficulty": {k: f"{v[1]}/{v[0]}" for k, v in by_diff.items()},
    }
    RESULT_PATH.write_text(
        json.dumps({"summary": summary, "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"SQL 可执行率 : {n_exec}/{total} ({summary['exec_rate']}%)")
    print(f"结果正确率   : {n_correct}/{total} ({summary['accuracy']}%)  [execution accuracy]")
    print(f"纠错触发     : {n_reflect} 条题触发了 Self-Reflection")
    print(f"难度分布     : {summary['by_difficulty']}")
    print(f"平均耗时     : {summary['avg_seconds']}s/题")
    print(f"明细已写入   : {RESULT_PATH}")
    return summary


def main():
    data = json.loads(EVAL_SET.read_text(encoding="utf-8"))
    cases = data["cases"]
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "gold":
        gold_check(cases)
        return
    n = int(arg) if arg.isdigit() else len(cases)
    print(f"== 开始评测：{n}/{len(cases)} 条 ==")
    run_eval(cases[:n])


if __name__ == "__main__":
    main()
