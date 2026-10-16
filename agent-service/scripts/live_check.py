"""真机联调自检脚本：打已启动的 Agent 服务（默认 8000），走真实 Java 后端。

用法（Java 后端需先启动，且 INTERNAL_SIGN_SECRET 与之一致）：

    $env:JAVA_BASE_URL='http://127.0.0.1:8080'
    $env:SMARTLIB_INTERNAL_SIGN='dev-internal-secret-please-change'
    python scripts/live_check.py --reader-id 3

它做三件事：
1. 打印 Agent /health（含对 Java 后端的连通性视图与熔断器状态）；
2. 依次问"库存查询"与"帮我借某本书"，打印完整响应（含工具调用链与 book_id）；
3. 紧接着发送"确认借阅"，验证写操作在真实后端上真的落地。

[注意] reader_id 必须是真实后端里存在的读者。本机实测库里是 1/2/3，
   用不存在的 id 会得到后端业务错误「读者不存在」——那属于数据问题，不是 Agent 缺陷。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

AGENT = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000")
BACKEND = os.getenv("JAVA_BASE_URL", "http://127.0.0.1:8080")


def show(label: str, body: dict) -> None:
    print(label)
    print(f"  trace_id   : {body.get('trace_id')}")
    print(
        f"  intent     : {body.get('intent')} conf={body.get('intent_confidence')} "
        f"steps={body.get('steps')}"
    )
    print(
        f"  evidence   : {body.get('tool_evidence')} degraded={body.get('degraded')} "
        f"handoff={body.get('handoff')}"
    )
    print(f"  tool_calls : {json.dumps(body.get('tool_calls'), ensure_ascii=False)}")
    print(f"  pending    : {json.dumps(body.get('pending_confirm'), ensure_ascii=False)}")
    print(f"  notes      : {body.get('guardrail_notes')}")
    print(f"  reply      : {body.get('reply')}")
    print("-" * 88)


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartLib Agent 真机联调自检")
    parser.add_argument("--reader-id", type=int, default=3, help="真实存在的读者 id")
    parser.add_argument("--book", default="算法导论", help="要查询/借阅的书名")
    args = parser.parse_args()

    with httpx.Client(timeout=30.0) as client:
        try:
            backend = client.get(f"{BACKEND}/actuator/health")
            print(f"[backend {BACKEND}] HTTP {backend.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"[backend {BACKEND}] 不可达：{type(exc).__name__}: {exc}")
            print("请先启动 Java 后端，或设置 JAVA_BASE_URL 指向正确地址。")
            return 2

        health = client.get(f"{AGENT}/health").json()
        print(
            f"[agent {AGENT}/health] mode={health['agent_mode']} provider={health['llm_provider']} "
            f"chunks={health['knowledge_chunks']} tools={health['tools']} "
            f"circuit={health['java_backend']['circuit']['state']}"
        )
        print("=" * 88)

        stock = client.post(
            f"{AGENT}/chat/sync",
            json={"message": f"《{args.book}》还有几本", "reader_id": args.reader_id},
        )
        show(f"[1] Q: 《{args.book}》还有几本", stock.json())

        borrow = client.post(
            f"{AGENT}/chat/sync",
            json={"message": f"帮我借《{args.book}》", "reader_id": args.reader_id},
        )
        borrow_body = borrow.json()
        show(f"[2] Q: 帮我借《{args.book}》", borrow_body)

        if borrow_body.get("pending_confirm"):
            confirm = client.post(
                f"{AGENT}/chat/sync",
                json={
                    "message": "确认借阅",
                    "reader_id": args.reader_id,
                    "session_id": borrow_body.get("session_id"),
                },
            )
            show("[3] Q: 确认借阅", confirm.json())
        else:
            print("[3] 本轮没有待确认的写操作，跳过确认环节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
