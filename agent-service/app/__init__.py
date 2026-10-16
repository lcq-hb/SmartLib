"""SmartLib Agent 服务（Python / FastAPI）。

分层（蓝图 §4.3）：
    api → agent(runtime/intent/planner/executor/guardrail) → tools → client(Java 后端)
                              ↘ llm（mock / openai_compat）
                              ↘ rag、memory
"""

__version__ = "1.0.0"
