"""全局配置。

设计取舍：
1. 用 `pydantic-settings` 而不是散落的 `os.getenv`，因为配置项要同时在
   FastAPI 依赖注入、评测脚本（`python -m app.eval.run_eval`）和 pytest 里可用，
   集中一处可以避免"测试里改了环境变量但业务代码读的是另一份缓存"的经典坑。
2. 所有有默认值的项都必须让服务在**零 Key、零外部依赖**下启动，
   这样 CI / 面试官本机 / 断网的演示环境都能跑通（蓝图 §3.2 明确要求 MockLLM）。
3. 用 lru_cache 暴露 get_settings()，同一个进程只解析一次；
   测试中需要覆盖配置时调用 `get_settings.cache_clear()`。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 与 backend/src/main/resources/application.yml 中 smartlib.internal.sign-secret 的默认值保持一致。
# 两边不一致会在联调时直接 401，所以这里把默认值写死成同一个字符串。
DEFAULT_INTERNAL_SIGN = "dev-internal-secret-please-change"

AgentMode = Literal["rule", "llm", "hybrid", "plan"]
LLMProvider = Literal["mock", "openai_compat"]


class Settings(BaseSettings):
    """环境变量配置表（完整说明见 README.md 的环境变量表）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 服务本身 ----------------
    app_name: str = "smartlib-agent"
    app_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # ---------------- 跨域（只服务于本地演示控制台） ----------------
    # 演示控制台是单页 HTML，放在后端静态资源里（http://127.0.0.1:8080/demo.html），
    # 它调用本服务的 /chat/sync 属于跨域，因此这里白名单放行本机来源。
    #
    # 为什么用"逗号分隔的字符串"而不是 list[str]：
    #   pydantic-settings 对复杂类型会按 **JSON** 解析环境变量，
    #   写成 CORS_ORIGINS=http://a,http://b 会直接校验失败；
    #   逗号分隔更符合运维直觉，拆解放在 cors_origin_list 里做。
    # 为什么默认只放本机：
    #   生产环境 Agent 只被网关调用，浏览器不该直连；
    #   默认收紧、需要时显式配置，比默认放开安全。
    cors_origins: str = (
        "http://127.0.0.1:8080,http://localhost:8080,"
        "http://127.0.0.1:5500,http://localhost:5500"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的配置拆成列表，容忍多余空格与空项。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ---------------- Agent 运行时 ----------------
    # hybrid = 规则路由 + LLM 兜底（蓝图 §6.4 G5），是默认模式
    agent_mode: AgentMode = "hybrid"
    # ReAct 主循环最大步数：蓝图 §6.4 要求"最大步数 5"，
    # 目的是防止 LLM 在工具反复失败时无限绕圈烧 token
    max_steps: int = 5
    # 单步（单次 LLM 调用或单次工具调用）超时，秒
    step_timeout_seconds: float = 3.0
    # 低于该置信度不再猜，直接转人工（蓝图 §6.4 "低置信度转人工"）
    low_confidence_threshold: float = 0.45
    # 输出长度上限（护栏规则之一），超出走优雅截断而不是报错
    max_output_chars: int = 800
    # 同一会话内相同工具+相同参数重复调用超过该次数即判定为死循环
    loop_repeat_limit: int = 2

    # ---------------- Java 后端 ----------------
    # 本机后端默认端口见 backend application.yml 的 server.port
    java_base_url: str = "http://127.0.0.1:8080"
    java_timeout_seconds: float = 3.0
    java_retries: int = 2
    java_circuit_fail_threshold: int = 5
    java_circuit_open_seconds: float = 10.0
    java_circuit_half_open_probes: int = 3
    internal_sign_secret: str = DEFAULT_INTERNAL_SIGN
    internal_sign_enabled: bool = True

    # ---------------- LLM ----------------
    llm_provider: LLMProvider = "mock"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 30.0
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.2

    # ---------------- RAG ----------------
    knowledge_dir: str = "app/knowledge"
    rag_top_k: int = 4
    rag_bm25_weight: float = 0.7
    rag_vector_weight: float = 0.3
    # 向量召回：无 embedding key 时用确定性哈希嵌入降级（蓝图 §3.2）
    rag_enable_vector: bool = True
    rag_chunk_size: int = 320
    rag_chunk_overlap: int = 60

    # ---------------- Memory ----------------
    session_max_turns: int = 10
    session_ttl_seconds: int = 1800
    profile_ttl_seconds: int = 86400

    @field_validator("max_steps")
    @classmethod
    def _check_max_steps(cls, v: int) -> int:
        # 上限 5 是蓝图写死的 SLO 约束，配置不小心写大就失去"防绕圈"的意义
        if v < 1 or v > 5:
            raise ValueError("max_steps 必须在 1~5 之间（蓝图 §6.4 规定最大步数 5）")
        return v

    @field_validator("rag_bm25_weight", "rag_vector_weight")
    @classmethod
    def _check_weight(cls, v: float) -> float:
        if v < 0 or v > 1:
            raise ValueError("RAG 权重必须在 0~1 之间")
        return v

    @property
    def knowledge_path(self) -> str:
        """知识库目录的绝对路径。

        评测脚本和 pytest 的当前工作目录可能是仓库根目录，
        直接拼 cwd 会找不到文件，这里用包所在目录反推，保证任何 cwd 下都能加载。
        """
        import os

        if os.path.isabs(self.knowledge_dir):
            return self.knowledge_dir
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(pkg_root, self.knowledge_dir)

    @property
    def use_real_llm(self) -> bool:
        return self.llm_provider == "openai_compat"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
