#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SQL 漂移检测：保证"应用使用的 DDL/DML"与"部署用 SQL"始终一致。

为什么需要这个脚本：
    schema/data 在仓库里有两处副本 ——
      * backend/src/main/resources/db/{schema,data}.sql   （应用启动时执行，H2 MODE=MySQL）
      * deploy/sql/{01-schema,02-seed}.sql                （MySQL 容器初始化时执行）
    两份内容一旦漂移，就会出现"本地演示正常、Docker 起来数据不对"
    或者"本地 24 本书、容器里只有 14 本"这类极难排查的问题（**真实发生过**）。
    人工同步迟早会漏，所以用脚本 + CI 强制校验。

比对规则（只忽略确实应当不同的部分）：
    * 注释行（-- 开头）与空行
    * deploy 侧独有的 MySQL 会话/库级语句（SET NAMES / ALTER DATABASE / USE）
    其余所有语句必须逐条一致（大小写与空白归一化后比较）。

用法：
    python scripts/tools/check_sql_sync.py            # 校验，不一致则退出码 1
    python scripts/tools/check_sql_sync.py --verbose   # 打印差异明细
"""
from __future__ import annotations

import argparse
import os
import re
import sys

PAIRS = [
    ("backend/src/main/resources/db/schema.sql", "deploy/sql/01-schema.sql"),
    ("backend/src/main/resources/db/data.sql", "deploy/sql/02-seed.sql"),
]

# deploy 侧允许额外存在的、纯 MySQL 环境语句
ALLOWED_EXTRA = re.compile(r"^(set\s+names|alter\s+database|use\s+|set\s+time_zone)", re.I)


def normalize(path: str) -> list[str]:
    """把 SQL 规范化为可比较的语句列表：去注释、去空行、压缩空白、统一小写。"""
    statements: list[str] = []
    with open(path, encoding="utf-8") as fp:
        for raw in fp:
            line = raw.strip()
            if not line or line.startswith("--"):
                continue
            line = re.sub(r"\s+", " ", line).strip().lower()
            statements.append(line)
    return statements


def compare(app_path: str, deploy_path: str, verbose: bool) -> bool:
    if not os.path.exists(app_path) or not os.path.exists(deploy_path):
        print(f"[缺失] {app_path} 或 {deploy_path} 不存在")
        return False

    app_lines = normalize(app_path)
    deploy_lines = [ln for ln in normalize(deploy_path) if not ALLOWED_EXTRA.match(ln)]

    if app_lines == deploy_lines:
        print(f"[一致] {os.path.basename(app_path)} ({len(app_lines)} 条语句)")
        return True

    print(f"[漂移] {app_path}")
    print(f"       与 {deploy_path} 不一致（应用侧 {len(app_lines)} 条 / 部署侧 {len(deploy_lines)} 条）")
    if verbose:
        only_app = [ln for ln in app_lines if ln not in deploy_lines]
        only_deploy = [ln for ln in deploy_lines if ln not in app_lines]
        for ln in only_app[:10]:
            print(f"       仅应用侧: {ln[:110]}")
        for ln in only_deploy[:10]:
            print(f"       仅部署侧: {ln[:110]}")
    else:
        print("       加 --verbose 查看差异明细")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="校验应用侧与部署侧 SQL 是否一致")
    parser.add_argument("--root", default=None, help="仓库根目录，默认自动推断")
    parser.add_argument("--verbose", action="store_true", help="打印差异明细")
    args = parser.parse_args()

    root = args.root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(f"仓库根目录: {root}")
    all_ok = True
    for app_rel, deploy_rel in PAIRS:
        ok = compare(os.path.join(root, app_rel), os.path.join(root, deploy_rel), args.verbose)
        all_ok = all_ok and ok

    if all_ok:
        print("\n结论：SQL 无漂移。")
        return 0
    print("\n结论：检测到 SQL 漂移！请同步 backend 与 deploy 两侧的 SQL 后重试。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
