"""真机联调：用真实读者 3 + 一本可借书，走完整 Agent 借阅链路。

同时打印后端实际返回的字段名，用于核对 Agent 的字段归一化是否覆盖真实契约。
"""
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.agent.guardrail import Guardrail
from app.agent.intent import IntentClassifier
from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.client.java_client import JavaClient
from app.config import Settings
from app.llm.mock import MockLLM
from app.memory.session_store import get_session_store

BACKEND = "http://127.0.0.1:8080"
SECRET = os.getenv("SMARTLIB_INTERNAL_SIGN", "dev-internal-secret-please-change")
READER_ID = 3


async def main() -> int:
    settings = Settings(
        _env_file=None,
        llm_provider="mock",
        agent_mode="hybrid",
        java_base_url=BACKEND,
        internal_sign_secret=SECRET,
        log_level="WARNING",
    )
    client = JavaClient(settings)
    raw = httpx.AsyncClient(base_url=BACKEND, timeout=10.0)

    # 找一个"读者 3 还没借且有余量"的书
    content = json.dumps({"query": "", "page": 1, "size": 20, "sortBy": "relevance"},
                         ensure_ascii=False, separators=(",", ":")).encode()
    headers = client._sign_headers("POST", "/internal/agent/tools/search-books", content, "tr-probe")
    headers["Content-Type"] = "application/json"
    resp = await raw.post("/internal/agent/tools/search-books", content=content, headers=headers)
    items = (resp.json().get("data") or {}).get("items") or []

    loans_resp = await raw.get(
        f"/internal/agent/tools/readers/{READER_ID}/loans",
        headers=client._sign_headers("GET", f"/internal/agent/tools/readers/{READER_ID}/loans", b"", "tr-probe"),
    )
    owned = {loan["bookId"] for loan in (loans_resp.json().get("data") or [])}
    print(f"读者 {READER_ID} 已借 bookId: {sorted(owned)}")

    target = next(
        (b for b in items if (b.get("availableCopies") or 0) > 0 and (b.get("id") or b.get("bookId")) not in owned),
        None,
    )
    if target is None:
        print("没有找到可借的新书，跳过借阅验证")
        await raw.aclose(); await client.aclose()
        return 1

    title = target["title"]
    print(f"目标书: id={target.get('id')} 《{title}》 可借={target.get('availableCopies')}")
    print(f"后端 search 字段名: {sorted(target)}")
    print(f"后端 loans 字段名: {sorted((loans_resp.json().get('data') or [{}])[0])}" if loans_resp.json().get("data") else "")
    print("=" * 88)

    llm = MockLLM(settings)
    runtime = AgentRuntime(
        settings=settings,
        llm=llm,
        java_client=client,
        guardrail=Guardrail(settings),
        classifier=IntentClassifier(llm, settings),
        planner=Planner(llm, settings),
    )

    session_id = "s-live-borrow"
    for question in (f"{title}还有几本", f"帮我借《{title}》", "确认借阅"):
        get_session_store().clear() if question.startswith(f"{title}还有") else None
        result = await runtime.run(question, session_id=session_id, reader_id=READER_ID)
        print(f"Q: {question}")
        print(f"   intent={result.intent} evidence={result.tool_evidence} steps={result.steps}")
        print(f"   tool_calls: {json.dumps(result.tool_calls, ensure_ascii=False)}")
        print(f"   internal  : {json.dumps([t['tool'] for t in result.internal_tool_calls], ensure_ascii=False)}")
        print(f"   pending   : {json.dumps(result.pending_confirm, ensure_ascii=False)}")
        print(f"   reply     : {result.reply}")
        print("-" * 88)
        if question.startswith("确认借阅"):
            break

    await raw.aclose()
    await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
