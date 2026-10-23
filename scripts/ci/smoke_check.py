#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 冒烟断言：把「服务真的能用」这件事验证成可重复的脚本。

为什么要有这个文件而不是在 workflow 里内联 python：
    1. YAML 的 `run: |` 块里，heredoc 的结束符必须顶格，但块内每一行又必须缩进，
       两者冲突 —— 内联 `python3 - <<'PY'` 会在 runner 上直接语法失败。
    2. 断言逻辑写在文件里可版本管理、可在本地复现，出问题不用对着 YAML 猜。

用法（服务需已启动）：
    python3 scripts/ci/smoke_check.py search
    python3 scripts/ci/smoke_check.py borrow --key ci-123
    python3 scripts/ci/smoke_check.py reconcile
    python3 scripts/ci/smoke_check.py chat
    python3 scripts/ci/smoke_check.py all --key ci-123

环境变量：
    SMOKE_BACKEND  后端地址，默认 http://127.0.0.1:8080
    SMOKE_AGENT    Agent 地址，默认 http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BACKEND = os.environ.get('SMOKE_BACKEND', 'http://127.0.0.1:8080').rstrip('/')
AGENT = os.environ.get('SMOKE_AGENT', 'http://127.0.0.1:8000').rstrip('/')

_failures = []


def ok(label: str, detail: str = '') -> None:
    print(f'  PASS  {label}{("  " + detail) if detail else ""}')


def fail(label: str, detail: str) -> None:
    print(f'  FAIL  {label}  {detail}')
    _failures.append(label)


def http(url: str, method: str = 'GET', body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {'raw': raw[:400]}


# ---------------------------------------------------------------- 各项检查
def check_search() -> None:
    print('[1/4] 检索：中文关键词 + 拼音首字母')
    q = urllib.parse.quote('计算机网络')
    st, a = http(f'{BACKEND}/api/books?query={q}&size=3')
    if st != 200 or a.get('code') != 0:
        fail('中文检索', f'HTTP {st} body={a}')
    elif not a['data']['list']:
        fail('中文检索', '结果为空')
    else:
        ok('中文检索', f'命中 {a["data"]["total"]} 条，首条《{a["data"]["list"][0]["title"]}》')

    st, b = http(f'{BACKEND}/api/books?query=jsjwl&size=3')
    if st != 200 or b.get('code') != 0:
        fail('拼音首字母检索', f'HTTP {st} body={b}')
    elif not b['data']['list']:
        fail('拼音首字母检索', '结果为空（拼音索引与查询侧可能不一致）')
    elif '计算机网络' not in b['data']['list'][0]['title']:
        fail('拼音首字母检索', f'首条是《{b["data"]["list"][0]["title"]}》，未命中《计算机网络》')
    else:
        ok('拼音首字母检索', f'首条《{b["data"]["list"][0]["title"]}》')


def _pick_book() -> int | None:
    """挑一本「读者 1 没借过、且当前可借」的书，避免撞业务规则。"""
    st, loans = http(f'{BACKEND}/api/readers/1/loans?limit=100')
    if st != 200:
        fail('查询读者在借', f'HTTP {st} body={loans}')
        return None
    held = {x['bookId'] for x in loans.get('data') or []
            if x.get('status') in ('BORROWED', 'OVERDUE')}
    st, books = http(f'{BACKEND}/api/books?availableOnly=true&size=50')
    if st != 200:
        fail('查询可借书目', f'HTTP {st} body={books}')
        return None
    for b in books['data']['list']:
        if b['id'] not in held:
            return b['id']
    fail('挑选可借书目', '没有找到该读者还能借的书，可能需要重启后端恢复种子数据')
    return None


def check_borrow(key: str) -> None:
    print('[2/4] 借阅幂等：同一个 Idempotency-Key 连发两次')
    book_id = _pick_book()
    if book_id is None:
        return
    st1, b1 = http(f'{BACKEND}/api/borrows', 'POST',
                   {'readerId': 1, 'bookId': book_id}, {'Idempotency-Key': key})
    st2, b2 = http(f'{BACKEND}/api/borrows', 'POST',
                   {'readerId': 1, 'bookId': book_id}, {'Idempotency-Key': key})
    if st1 != 200 or st2 != 200:
        fail('借阅请求', f'HTTP {st1}/{st2} body={b1 if st1 != 200 else b2}')
        return
    d1, d2 = b1.get('data') or {}, b2.get('data') or {}
    if d1.get('ok') is not True:
        fail('第 1 次借阅', f'未成功 body={d1}')
        return
    if d1.get('borrowId') != d2.get('borrowId'):
        fail('幂等', f'两次 borrowId 不同：{d1.get("borrowId")} vs {d2.get("borrowId")}')
        return
    if d2.get('idempotentReplay') is not True:
        fail('幂等', f'第二次没有标记为重放 body={d2}')
        return
    ok('借阅幂等', f'bookId={book_id} 两次 borrowId 均为 {d1["borrowId"]}，第二次 idempotentReplay=true')


def check_reconcile() -> None:
    print('[3/4] 库存对账：库存与流水守恒')
    st, r = http(f'{BACKEND}/api/admin/reconcile')
    if st != 200:
        fail('对账接口', f'HTTP {st} body={r}（可能是 admin 接口未开启）')
        return
    d = r.get('data') or {}
    if d.get('consistent') is not True or d.get('violationCount', 1) != 0:
        fail('对账一致性', f'body={d}')
        return
    ok('对账一致', f'违规书目 {d.get("violationCount")} 条')


def check_chat() -> None:
    print('[4/4] Agent 对话：回答必须来自工具返回的真实数据')
    msg = '《计算机网络》现在还有库存吗？在哪个架位？'
    st, body = http(f'{AGENT}/chat/sync', 'POST',
                    {'reader_id': 1, 'message': msg, 'session_id': 'ci-smoke'})
    if st != 200:
        fail('Agent 对话', f'HTTP {st} body={body}')
        return
    d = body.get('data', body)
    reply = d.get('reply') or d.get('answer') or ''
    tools = [c.get('tool') for c in (d.get('tool_calls') or [])]
    if not reply:
        fail('Agent 回答', f'为空 body={d}')
        return
    # 断言要查"证据"，不要查具体用词：早先这里写死了「架位」两个字，
    # 而实际文案是「索书位置：三楼东区 TP393/771」，导致明明答对了却判失败。
    if '馆藏' not in reply or '册' not in reply:
        fail('Agent 回答内容', f'未包含真实库存信息（应含"馆藏"与"N 册"）：{reply[:120]}')
        return
    if 'get_stock' not in tools:
        fail('Agent 工具调用', f'未调用 get_stock，实际调用了 {tools}')
        return
    ok('Agent 对话', f'intent={d.get("intent")} tools={tools}')
    print(f'        回答：{reply.replace(chr(10), " ")[:110]}')


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description='SmartLib CI 冒烟断言')
    ap.add_argument('check', choices=['search', 'borrow', 'reconcile', 'chat', 'all'])
    ap.add_argument('--key', default='ci-smoke-key', help='借阅幂等键')
    args = ap.parse_args()

    print(f'backend={BACKEND}  agent={AGENT}')
    if args.check in ('search', 'all'):
        check_search()
    if args.check in ('borrow', 'all'):
        check_borrow(args.key)
    if args.check in ('reconcile', 'all'):
        check_reconcile()
    if args.check in ('chat', 'all'):
        check_chat()

    print()
    if _failures:
        print(f'冒烟失败 {len(_failures)} 项：' + '、'.join(_failures))
        return 1
    print('冒烟全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
