"""工具注册表：工具白名单的唯一来源。

护栏的"工具白名单"就是查这张表——**未注册的工具名一律拒绝**。
把白名单和实现放在同一个模块，可以避免"加了个工具但忘了加进白名单"
（那会导致线上工具不可用）或"白名单里有工具但没实现"（会导致 500）这两类事故。

注册表是进程级单例：工具是无状态的（状态都在 Java 后端与 memory 里），
每次请求重建一遍纯属浪费。
"""

from __future__ import annotations

from typing import Iterator

from app.tools.base import Tool

_TOOLS: dict[str, Tool] = {}

#: 蓝图 §4.3 规定的 8 个工具名，用于启动自检（少一个就拒绝启动，早失败早发现）
REQUIRED_TOOLS: tuple[str, ...] = (
    "search_books",
    "get_stock",
    "borrow",
    "renew",
    "my_loans",
    "get_reader_profile",
    "policy_qa",
    "recommend",
)


def register(tool: Tool, *, replace: bool = False) -> Tool:
    """注册工具。重名默认报错——静默覆盖会让排查变成猜谜。"""
    name = tool.name
    if not name:
        raise ValueError("工具必须有 name")
    if name in _TOOLS and not replace:
        raise ValueError(f"工具 {name} 已注册，如需覆盖请显式传 replace=True")
    _TOOLS[name] = tool
    return tool


def get(name: str) -> Tool | None:
    return _TOOLS.get(name)


def exists(name: str) -> bool:
    return name in _TOOLS


def all_tools() -> list[Tool]:
    """按蓝图 §4.3 的顺序返回，保证 /tools 输出稳定（便于前端与文档对齐）。"""
    ordered = [t for n in REQUIRED_TOOLS if (t := _TOOLS.get(n)) is not None]
    extra = [t for n, t in _TOOLS.items() if n not in REQUIRED_TOOLS]
    return ordered + extra


def names() -> set[str]:
    return set(_TOOLS)


def llm_specs() -> list[dict]:
    """给 LLM function calling 用的工具描述列表。"""
    return [t.llm_spec() for t in all_tools()]


def itertool_names() -> Iterator[str]:
    return iter(_TOOLS.keys())


def missing_required() -> list[str]:
    return [n for n in REQUIRED_TOOLS if n not in _TOOLS]


def reset() -> None:
    """仅供测试使用：清空注册表后重新装配。"""
    _TOOLS.clear()


def build_default_registry(*, force: bool = False) -> dict[str, Tool]:
    """装配 8 个工具。幂等：已装配过且数量正确时直接返回。"""
    if not force and not missing_required():
        return _TOOLS

    # 延迟导入：避免 registry ← tools/* ← registry 的循环导入
    from app.tools.borrow import TOOL as borrow_tool
    from app.tools.get_reader_profile import TOOL as profile_tool
    from app.tools.get_stock import TOOL as stock_tool
    from app.tools.my_loans import TOOL as loans_tool
    from app.tools.policy_qa import TOOL as policy_tool
    from app.tools.recommend import TOOL as recommend_tool
    from app.tools.renew import TOOL as renew_tool
    from app.tools.search_books import TOOL as search_tool

    for tool in (
        search_tool,
        stock_tool,
        borrow_tool,
        renew_tool,
        loans_tool,
        profile_tool,
        policy_tool,
        recommend_tool,
    ):
        register(tool, replace=True)

    missing = missing_required()
    if missing:
        raise RuntimeError(f"工具装配不完整，缺少: {missing}")
    return _TOOLS


# 导入即装配：FastAPI 启动与评测脚本都不需要额外调用
REGISTRY = build_default_registry()


def get_registry() -> dict[str, Tool]:
    return REGISTRY


__all__ = [
    "REGISTRY",
    "REQUIRED_TOOLS",
    "register",
    "get",
    "exists",
    "all_tools",
    "names",
    "llm_specs",
    "missing_required",
    "reset",
    "build_default_registry",
    "get_registry",
]
