"""真机联调验证：通过 HTTP 打 Agent 服务（8000），走真实 Java 后端（8080）。

输出两条目标用例的完整响应，以及确认环节的真实借阅结果。
"""
import json
import sys

import httpx

AGENT = "http://127.0.0.1:8000"
BACKEND = "http://127.0.0.1:8080"
READER_ID = 3


def show(label: str, body: dict) -> None:
    print(f"{label}")
    print(f"  trace_id   : {body.get('trace_id')}")
    print(f"  intent     : {body.get('intent')} conf={body.get('intent_confidence')} steps={body.get('steps')}")
    print(f"  evidence   : {body.get('tool_evidence')}  degraded={body.get('degraded')}")
    print(f"  tool_calls : {json.dumps(body.get('tool_calls'), ensure_ascii=False)}")
    print(f"  internal   : {json.dumps(body.get('internal_tool_calls'), ensure_ascii=False)}")
    print(f"  pending    : {json.dumps(body.get('pending_confirm'), ensure_ascii=False)}")
    print(f"  notes      : {body.get('guardrail_notes')}")
    print(f"  reply      : {body.get('reply')}")
    print("-" * 88)


def main() -> int:
    with httpx.Client(timeout=30.0) as client:
        health = client.get(f"{AGENT}/health").json()
        print(f"[agent /health] circuit={health['java_backend']['circuit']['state']} "
              f"chunks={health['knowledge_chunks']} tools={health['tools']}")
        try:
            print(f"[backend] HTTP {client.get(f'{BACKEND}/actuator/health').status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"[backend] 不可达 {exc}")
        print("=" * 88)

        # 1) 库存查询（原联调失败用例 1）
        r1 = client.post(f"{AGENT}/chat/sync", json={"message": "《算法导论》还有几本", "reader_id": READER_ID})
        show("[1] Q: 《算法导论》还有几本", r1.json())

        # 2) 借书（原联调失败用例 2）
        r2 = client.post(f"{AGENT}/chat/sync", json={"message": "帮我借《算法导论》", "reader_id": READER_ID})
        body2 = r2.json()
        show("[2] Q: 帮我借《算法导论》", body2)

        # 3) 确认（写操作真正落地）
        session_id = body2.get("session_id")
        r3 = client.post(
            f"{AGENT}/chat/sync",
            json={"message": "确认借阅", "reader_id": READER_ID, "session_id": session_id},
        )
        show("[3] Q: 确认借阅", r3.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
