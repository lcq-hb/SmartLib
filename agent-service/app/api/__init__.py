"""API 层：chat(SSE) / chat/sync / health / tools / recommend / admin。

刻意**不做** eager import（不在这里 `from app.api import chat`）：
executor 需要复用 api/schemas.py 里的 ToolCallTrace，
如果包初始化时就去导入 chat/tools 这些 router 模块，会形成
`api.__init__ → api.chat → agent.runtime → agent.executor → api.schemas`
的循环导入。main.py 显式 `from app.api.chat import router` 即可。
"""
