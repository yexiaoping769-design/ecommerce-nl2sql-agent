# -*- coding: utf-8 -*-
"""
FastAPI 后端：SSE 流式推送 Agent 执行过程
POST /chat  -> text/event-stream
  事件序列: plan -> sql -> result -> answer -> [DONE]
GET /  -> 前端页面
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import get_app

load_dotenv()

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="电商经营分析 NL2SQL 智能问数")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatReq(BaseModel):
    question: str


@app.post("/chat")
def chat(req: ChatReq):
    question = req.question.strip()
    if not question:
        return StreamingResponse(iter(["data: {\"type\": \"error\", \"message\": \"问题不能为空\"}\n\n",
                                       "data: [DONE]\n\n"]),
                                 media_type="text/event-stream")

    def gen():
        try:
            for chunk in get_app().stream({"question": question}):
                node = next(iter(chunk))
                state = chunk[node]
                if node == "planner":
                    yield _sse({"type": "plan", "plan": state.get("plan", "")})
                elif node == "sql_generator":
                    yield _sse({"type": "sql", "sql": state.get("sql", ""),
                                "reflections": state.get("reflection_log", [])})
                elif node == "sql_executor":
                    er = state.get("exec_result", {})
                    yield _sse({
                        "type": "result",
                        "ok": er.get("ok", False),
                        "error": er.get("error", ""),
                        "columns": er.get("columns", []),
                        "rows": er.get("rows", [])[:50],
                        "truncated": er.get("truncated", False),
                        "sql": er.get("sql", ""),
                    })
                elif node == "report":
                    yield _sse({"type": "answer", "answer": state.get("answer", "")})
            yield "data: [DONE]\n\n"
        except Exception as e:  # 兜底：不让后端异常压垮前端连接
            yield _sse({"type": "error", "message": f"服务异常: {e}"})
            yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# 前端静态资源（放最后挂载，避免吞掉 /chat）
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100)
