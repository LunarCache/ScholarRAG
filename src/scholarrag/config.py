from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SCHOLARRAG_",
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Path("data")
    evals_dir: Path = Path("evals")
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    api_bearer_token: SecretStr | None = None
    auth_mode: Literal["local", "session"] = "local"
    api_cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    authorization_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    query_worker_poll_seconds: float = Field(default=0.5, ge=0.02, le=10)
    query_worker_lease_seconds: float = Field(default=30, ge=1, le=300)
    query_worker_concurrency: int = Field(default=2, ge=1, le=32)
    evaluation_worker_poll_seconds: float = Field(default=0.5, ge=0.02, le=10)
    evaluation_worker_lease_seconds: float = Field(default=30, ge=1, le=300)
    evaluation_worker_concurrency: int = Field(default=1, ge=1, le=16)
    workspace_evaluation_max_active: int = Field(default=16, ge=1, le=1000)
    workspace_evaluation_max_per_user: int = Field(default=4, ge=1, le=100)
    workspace_query_max_active: int = Field(default=16, ge=1, le=1000)
    workspace_query_max_per_user: int = Field(default=4, ge=1, le=100)
    ingestion_worker_poll_seconds: float = Field(default=0.5, ge=0.02, le=10)
    ingestion_worker_lease_seconds: float = Field(default=30, ge=1, le=300)
    ingestion_worker_concurrency: int = Field(default=1, ge=1, le=16)
    workspace_ingestion_max_active: int = Field(default=64, ge=1, le=10000)
    workspace_ingestion_max_per_user: int = Field(default=8, ge=1, le=1000)
    api_requests_per_minute: int = Field(default=240, ge=10, le=10_000)
    upload_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    upload_max_pages: int = Field(default=500, ge=1)

    # Optional telemetry. Credentials alone never enable export.
    langfuse_enabled: bool = False
    langfuse_capture_content: bool = False
    langfuse_public_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGFUSE_PUBLIC_KEY", "SCHOLARRAG_LANGFUSE_PUBLIC_KEY"),
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LANGFUSE_SECRET_KEY", "SCHOLARRAG_LANGFUSE_SECRET_KEY"),
    )
    langfuse_base_url: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_BASE_URL", "SCHOLARRAG_LANGFUSE_BASE_URL"),
    )
    langfuse_environment: str = "development"

    mineru_api_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("MINERU_API_TOKEN", "SCHOLARRAG_MINERU_API_TOKEN"),
    )
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_model: Literal["vlm", "pipeline"] = "vlm"
    mineru_language: str = Field(default="en", min_length=1, max_length=32)
    mineru_is_ocr: bool = False

    paddleocr_access_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "PADDLEOCR_ACCESS_TOKEN", "SCHOLARRAG_PADDLEOCR_ACCESS_TOKEN"
        ),
    )
    paddle_model: str = "PaddleOCR-VL-1.6"
    parse_quality_mode: Literal["audit", "enforce"] = "audit"
    parse_quality_accept_score: float = Field(default=0.85, ge=0, le=1)
    parse_quality_review_score: float = Field(default=0.65, ge=0, le=1)
    parse_fallback_mode: Literal["off", "on_fallback", "on_review"] = "on_review"
    parse_route_min_score_improvement: float = Field(default=0.03, ge=0, le=1)
    # When a figure/table block has a bounding box but no extracted image (parser
    # failed to export the image), render the region straight from the source PDF.
    ingest_visual_backfill: bool = True

    siliconflow_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("SILICONFLOW_API_KEY", "SCHOLARRAG_SILICONFLOW_API_KEY"),
    )
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    # Retrieval uses one VL model/space for text chunks, text queries and images.
    # Keep both setting names for compatibility with existing deployments.
    embedding_model: str = "Qwen/Qwen3-VL-Embedding-8B"
    multimodal_embedding_model: str = "Qwen/Qwen3-VL-Embedding-8B"
    embedding_dimensions: int = Field(default=1024, ge=64, le=4096)
    rerank_model: str = "Qwen/Qwen3-VL-Reranker-8B"
    chat_model: str = "Qwen/Qwen3.6-27B"
    chat_enable_thinking: bool = True
    # Qwen3 official recommendations for thinking mode: temperature=0.6,
    # top_p=0.95, top_k=20, min_p=0. Greedy decoding (temperature=0) must be
    # avoided — it degrades performance and causes repetitive output.
    chat_temperature: float = Field(default=0.6, ge=0.01, le=2.0)
    chat_top_p: float = Field(default=0.95, ge=0.01, le=1.0)
    chat_top_k: int = Field(default=20, ge=1, le=100)
    chat_min_p: float = Field(default=0.0, ge=0.0, le=1.0)
    # Optional cap on per-turn thinking tokens (Qwen docs recommend >1024).
    chat_thinking_budget: int | None = Field(default=None, ge=1024, le=65536)
    chat_input_cny_per_million_tokens: float = Field(default=0, ge=0)
    chat_output_cny_per_million_tokens: float = Field(default=0, ge=0)
    provider_max_concurrency: int = Field(default=8, ge=1, le=64)
    provider_queue_timeout_seconds: float = Field(default=30, gt=0, le=600)
    provider_max_retries: int = Field(default=3, ge=0, le=10)
    provider_circuit_failure_threshold: int = Field(default=5, ge=1, le=50)
    provider_circuit_cooldown_seconds: float = Field(default=30, ge=1, le=600)
    agent_max_steps: int = Field(default=4, ge=1, le=8)
    agent_timeout_seconds: float = Field(default=180, ge=10, le=900)
    agent_max_context_chars: int = Field(default=48_000, ge=4_000, le=200_000)
    # Tool-calling agentic RAG: the planner turns retrieval primitives into
    # OpenAI-compatible tools and lets the chat model decide the search order.
    # Falls back to the hard-coded multi-step planner when disabled.
    agent_tool_calling: bool = True
    agent_tool_max_calls: int = Field(default=12, ge=1, le=40)
    # Visual evidence: attach block images (figures/tables) to generation messages
    # so the vision LLM reads them directly instead of relying on lossy OCR text.
    # No per-request image-count budget: every distinct figure/table image among
    # the retrieved chunks is attached, for answer quality over cost.
    chat_include_visual_evidence: bool = True
    chat_visual_max_edge: int = Field(default=1024, ge=128, le=4096)
    ragas_judge_model: str = "Qwen/Qwen3.6-27B"
    ragas_judge_context_window_tokens: int = 262_144
    ragas_judge_enable_thinking: bool = True
    ragas_judge_temperature: float = Field(default=0.6, ge=0.01, le=2.0)

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def vector_dir(self) -> Path:
        return self.data_dir / "qdrant"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "scholarrag.db"

    @property
    def unified_embedding_model(self) -> str:
        """Canonical model for every retrieval and evaluation embedding call."""
        return self.multimodal_embedding_model

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.vector_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
