#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SmartLib 代码生成器（实体类 + 枚举 + MyBatis 读模型）。

为什么要写生成器：
    8 张表共 70+ 个字段，每个字段要写 字段声明 + getter + setter，
    手写约 900 行样板代码，容易出现字段拼写与数据库列不一致的低级错误。
    用生成器把"字段清单"作为单一事实来源，改字段只改 SPEC 再重跑。

设计约束（与 docs/05-编码规范与工程实践.md 一致）：
    * 不使用 Lombok，所以 getter/setter 必须真实生成
    * 生成的类只承载数据，不写业务方法
    * 覆盖式生成，避免残留旧字段；--check 模式用于 CI 校验是否漂移

用法：
    python scripts/tools/gen_entities.py            # 生成
    python scripts/tools/gen_entities.py --check    # 只校验，不写文件（CI 用）
"""
from __future__ import annotations

import argparse
import os
import sys

# ---------------------------------------------------------------------------
# 实体字段清单：(Java 类型, 字段名) —— 顺序与 schema.sql 的列顺序一致，便于人工核对
# ---------------------------------------------------------------------------
ENTITY_SPEC: dict[str, list[tuple[str, str]]] = {
    "Book": [
        ("Long", "id"),
        ("String", "isbn"),
        ("String", "title"),
        ("String", "author"),
        ("String", "publisher"),
        ("Long", "categoryId"),
        ("Integer", "publishYear"),
        ("BigDecimal", "price"),
        ("String", "tags"),
        ("String", "summary"),
        ("String", "shelfLocation"),
        ("Integer", "totalCopies"),
        ("LocalDateTime", "createdAt"),
        ("LocalDateTime", "updatedAt"),
    ],
    "BookStock": [
        ("Long", "bookId"),
        ("Integer", "totalCopies"),
        ("Integer", "availableCopies"),
        ("Integer", "lockedCopies"),
        ("Integer", "version"),
        ("LocalDateTime", "updatedAt"),
    ],
    "Reader": [
        ("Long", "id"),
        ("String", "cardNo"),
        ("String", "name"),
        ("String", "type"),
        ("String", "dept"),
        ("Integer", "creditScore"),
        ("String", "status"),
        ("LocalDateTime", "createdAt"),
        ("LocalDateTime", "updatedAt"),
    ],
    "Category": [
        ("Long", "id"),
        ("String", "code"),
        ("String", "name"),
        ("Long", "parentId"),
        ("Integer", "sortNo"),
    ],
    "BorrowRecord": [
        ("Long", "id"),
        ("Long", "readerId"),
        ("Long", "bookId"),
        ("LocalDateTime", "borrowAt"),
        ("LocalDateTime", "dueAt"),
        ("LocalDateTime", "returnAt"),
        ("Integer", "renewCount"),
        ("String", "status"),
        ("BigDecimal", "fine"),
        ("LocalDateTime", "createdAt"),
        ("LocalDateTime", "updatedAt"),
    ],
    "Reservation": [
        ("Long", "id"),
        ("Long", "readerId"),
        ("Long", "bookId"),
        ("Integer", "queueNo"),
        ("String", "status"),
        ("LocalDateTime", "readyAt"),
        ("LocalDateTime", "expireAt"),
        ("LocalDateTime", "createdAt"),
        ("LocalDateTime", "updatedAt"),
    ],
    "StockLedger": [
        ("Long", "id"),
        ("Long", "bookId"),
        ("String", "opType"),
        ("Integer", "delta"),
        ("Integer", "beforeQty"),
        ("Integer", "afterQty"),
        ("String", "bizNo"),
        ("String", "strategy"),
        ("String", "traceId"),
        ("LocalDateTime", "createdAt"),
    ],
    "RuleConfig": [
        ("Long", "id"),
        ("String", "ruleKey"),
        ("String", "readerType"),
        ("String", "ruleValue"),
        ("String", "remark"),
        ("Integer", "version"),
        ("LocalDateTime", "updatedAt"),
    ],
    "IdempotentRecord": [
        ("Long", "id"),
        ("String", "idemKey"),
        ("String", "bizType"),
        ("String", "resultJson"),
        ("LocalDateTime", "createdAt"),
    ],
}

# ---------------------------------------------------------------------------
# MyBatis 读模型（查询结果行，不是表实体）
# ---------------------------------------------------------------------------
READ_MODEL_SPEC: dict[str, list[tuple[str, str]]] = {
    # 书目 + 库存 + 分类名的联合查询结果，供书目列表/检索结果/推荐结果复用
    "BookRow": [
        ("Long", "id"),
        ("String", "isbn"),
        ("String", "title"),
        ("String", "author"),
        ("String", "publisher"),
        ("String", "categoryName"),
        ("String", "categoryCode"),
        ("Integer", "publishYear"),
        ("BigDecimal", "price"),
        ("String", "tags"),
        ("String", "summary"),
        ("String", "shelfLocation"),
        ("Integer", "totalCopies"),
        ("Integer", "availableCopies"),
        ("Double", "hotScore"),
    ],
    # 分组统计通用行，如"读者偏好分类 Top5"
    "NamedCount": [
        ("String", "name"),
        ("Integer", "cnt"),
    ],
    # "我的借阅"列表：borrow_record 与 book 的联合查询结果
    "LoanRow": [
        ("Long", "borrowId"),
        ("Long", "bookId"),
        ("String", "title"),
        ("String", "author"),
        ("String", "shelfLocation"),
        ("LocalDateTime", "borrowAt"),
        ("LocalDateTime", "dueAt"),
        ("Integer", "renewCount"),
        ("String", "status"),
        ("BigDecimal", "fine"),
    ],
}

ENUM_SPEC: dict[str, list[str]] = {
    "ReaderType": ["UNDERGRAD", "POSTGRAD", "TEACHER"],
    "BorrowStatus": ["BORROWED", "RETURNED", "OVERDUE", "LOST"],
    "ReservationStatus": ["WAITING", "READY", "FULFILLED", "EXPIRED", "CANCELLED"],
    "StockOpType": ["DEDUCT", "RELEASE", "ROLLBACK", "INIT"],
}

ENTITY_CN = {
    "Book": "书目实体，一种书一条记录，复本数量在 BookStock",
    "BookStock": "库存实体，全系统库存的唯一事实源，availableCopies 永不允许为负",
    "Reader": "读者实体",
    "Category": "图书分类实体（两级，parentId 为空表示一级分类）",
    "BorrowRecord": "借阅流水实体",
    "Reservation": "预约队列实体",
    "StockLedger": "库存流水实体，审计与对账的唯一依据",
    "RuleConfig": "借阅规则配置实体，改规则不发版",
    "IdempotentRecord": "幂等记录实体，保证写操作重复提交只生效一次",
}

READ_MODEL_CN = {
    "BookRow": "书目联合查询读模型（book + book_stock + category 的一次性投影）",
    "NamedCount": "分组统计读模型",
    "LoanRow": "借阅列表读模型（borrow_record + book 的投影），用于\"我的借阅\"",
}

ENUM_CN = {
    "ReaderType": "读者类型，决定可借册数、借期与续借次数（具体数值在 rule_config 表中配置）",
    "BorrowStatus": "借阅状态：BORROWED 在借 / RETURNED 已归还 / OVERDUE 逾期 / LOST 遗失",
    "ReservationStatus": "预约状态：WAITING 排队中 / READY 到馆保留中 / FULFILLED 已借出 / EXPIRED 过期 / CANCELLED 已取消",
    "StockOpType": "库存操作类型：DEDUCT 扣减 / RELEASE 归还释放 / ROLLBACK 回滚补偿 / INIT 初始化",
}

GENERATED_BY = "scripts/tools/gen_entities.py"


def cap(name: str) -> str:
    return name[0].upper() + name[1:]


def gen_pojo(pkg: str, name: str, cn_name: str, fields: list[tuple[str, str]]) -> str:
    lines = [
        f"package com.smartlib.domain.{pkg};",
        "",
        "import java.math.BigDecimal;",
        "import java.time.LocalDateTime;",
        "",
        "/**",
        f" * {cn_name}。",
        " *",
        f" * <p>本类由 {GENERATED_BY} 生成，请勿手工修改字段：",
        " * 需要调整字段请修改生成器中的字段清单后重新生成（与",
        " * backend/src/main/resources/db/schema.sql 一一对应）。",
        " */",
        f"public class {name} {{",
        "",
    ]
    for typ, fname in fields:
        lines.append(f"    private {typ} {fname};")
    lines.append("")
    for typ, fname in fields:
        lines.append(f"    public {typ} get{cap(fname)}() {{")
        lines.append(f"        return {fname};")
        lines.append("    }")
        lines.append("")
        lines.append(f"    public void set{cap(fname)}({typ} {fname}) {{")
        lines.append(f"        this.{fname} = {fname};")
        lines.append("    }")
        lines.append("")
    lines.append("    @Override")
    lines.append("    public String toString() {")
    lines.append(f'        return "{name}{{"')
    for _, fname in fields:
        lines.append(f'                + "{fname}=" + {fname} + ", "')
    lines.append("                + '}';")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def gen_enum(name: str, values: list[str]) -> str:
    body = ",\n".join(f"    {v}" for v in values) + ";"
    return (
        "package com.smartlib.domain.enums;\n"
        "\n"
        "/**\n"
        f" * {ENUM_CN[name]}。\n"
        " *\n"
        f" * <p>本枚举由 {GENERATED_BY} 生成。\n"
        " */\n"
        f"public enum {name} {{\n"
        f"{body}\n"
        "}\n"
    )


def resolve_root(explicit: str | None) -> str:
    if explicit:
        return explicit
    # 本文件位于 <root>/scripts/tools/，上溯三级即仓库根目录
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 SmartLib 实体类、读模型与枚举")
    parser.add_argument("--root", default=None, help="仓库根目录，默认自动推断")
    parser.add_argument("--check", action="store_true", help="只检查是否漂移，不写文件")
    args = parser.parse_args()

    root = resolve_root(args.root)
    java_root = os.path.join(root, "backend", "src", "main", "java", "com", "smartlib", "domain")

    targets: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    for name, fields in ENTITY_SPEC.items():
        targets.append(("entity", name, ENTITY_CN[name], fields))
    for name, fields in READ_MODEL_SPEC.items():
        targets.append(("dto", name, READ_MODEL_CN[name], fields))

    written = 0
    drifted: list[str] = []

    for pkg, name, cn_name, fields in targets:
        content = gen_pojo(pkg, name, cn_name, fields)
        directory = os.path.join(java_root, pkg)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{name}.java")
        if args.check:
            old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if old != content:
                drifted.append(path)
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(content)
        written += 1
        print(f"生成 {os.path.relpath(path, root)}  ({len(fields)} 字段)")

    enum_dir = os.path.join(java_root, "enums")
    os.makedirs(enum_dir, exist_ok=True)
    for name, values in ENUM_SPEC.items():
        content = gen_enum(name, values)
        path = os.path.join(enum_dir, f"{name}.java")
        if args.check:
            old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if old != content:
                drifted.append(path)
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(content)
        written += 1
        print(f"生成 {os.path.relpath(path, root)}  ({len(values)} 枚举值)")

    if args.check:
        if drifted:
            print("以下文件与生成器不一致，请重新运行 gen_entities.py：")
            for p in drifted:
                print(f"  [drift] {p}")
            return 1
        print("实体类/读模型/枚举与生成器一致，无漂移。")
        return 0

    print(f"完成，共写入 {written} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
