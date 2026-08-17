from __future__ import annotations

import uuid

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    # Legacy placeholders retained for compatibility; prefer canonical names below.
    app_secret: str = "change-me"
    app_secret_key: str = "change-me"
    shopify_api_secret: str = ""
    shopify_api_key: str = ""
    internal_handoff_secret: str = ""
    token_encryption_key: str = ""
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    host: str = "0.0.0.0"
    port: int = 8080
    poc_enabled: bool = True
    poc_max_prompt_steps: int = 5
    poc_max_image_size_mb: int = 10
    poc_storage_dir: str = "storage/poc-jobs"
    poc_job_ttl_hours: int = 24
    openai_api_key: str = ""
    openai_image_model: str = "gpt-image-2"
    # GPT Image quality tier: low | medium | high | auto
    openai_image_quality: str = "medium"
    # Hard cap so a hung OpenAI call cannot block the worker forever.
    openai_image_timeout_seconds: float = 180.0
    # Phase 1: OPEN_AI (current). Phase 2: external_llm → LLM microservice (not implemented yet).
    ai_provider: str = "OPEN_AI"
    llm_service_url: str = ""
    # Primary Queue AI path: OPENAI_BATCH (production default) or SYNC (dev/emergency).
    ai_execution_mode: str = "OPENAI_BATCH"
    openai_batch_enabled: bool = True
    # Only when true may Primary Queue fall back to SYNC if Batch is unavailable.
    openai_allow_sync_fallback: bool = False
    openai_text_model: str = "gpt-4.1"
    openai_batch_completion_window: str = "24h"
    openai_batch_poll_interval_seconds: float = 20.0
    openai_temp_file_retention_hours: int = 48
    openai_batch_max_requests: int = 50000
    poc_dev_skip_auth: bool = False

    # PostgreSQL (production). SQLite URL allowed for local/tests only.
    database_url: str = "sqlite+pysqlite:///./storage/app.db"

    auto_processing_enabled: bool = True
    processing_batch_size: int = 10
    processing_batch_concurrency: int = 2
    processing_poll_interval_seconds: int = 5
    processing_max_attempts: int = 3
    processing_retry_delay_seconds: int = 60
    processing_item_timeout_seconds: int = 300
    # How long a PROCESSING lock may sit before recover requeues it.
    # Kept under the OpenAI timeout so dead workers unlock before the next demo waits too long.
    processing_stale_lock_seconds: int = 240
    processing_worker_id: str = ""
    processing_output_directory: str = "storage/processed"

    # Week 2 Auto Sync (server-side bounds; per-shop interval lives in DB)
    batch_interval_minutes_cap: int = 1440
    default_batch_interval_minutes: int = 15
    # Safety cap when claiming Secondary Queue items into one automatic Primary batch
    auto_batch_claim_limit: int = 500
    # Max BatchProducts per automatic Primary batch (fill existing QUEUED batches up to this)
    auto_batch_product_limit: int = 10
    manual_batch_product_limit: int = 2

    shopify_image_download_timeout_seconds: int = 60
    shopify_image_max_download_mb: int = 30
    shopify_api_version: str = "2026-07"
    # Optional bootstrap seed for first shop create in dev/uat only. API calls never read this.
    shopify_dev_access_token: str = ""
    # Proactive Admin token refresh cadence (client-credentials / expiring offline ~24h).
    shopify_token_proactive_refresh_hours: float = 23.0
    # How often the worker checks whether shops need a proactive token refresh.
    shopify_token_refresh_check_seconds: int = 3600

    # Shopify publishing
    publish_product_concurrency: int = 2
    shopify_file_upload_concurrency: int = 3
    shopify_file_status_poll_seconds: float = 2.0
    shopify_file_ready_timeout_seconds: float = 300.0
    shopify_reorder_timeout_seconds: float = 180.0
    publish_poll_interval_seconds: float = 3.0
    publish_stale_lock_seconds: int = 600
    publish_worker_id: str = ""

    # Shopify generated-image validation / temp retention (CDN versions)
    shopify_image_preferred_max_mb: int = 10
    shopify_image_optimize_warn_mb: int = 10
    shopify_image_optimize_attempt_mb: int = 15
    shopify_image_reject_mb: int = 20
    processing_temp_retry_retention_hours: int = 48
    # Soft warning thresholds for estimated app-managed image versions (metadata only;
    # not Shopify plan storage). total_versions applies to GENERATED Files uploads.
    image_storage_warn_total_versions: int = 5000
    image_storage_warn_avg_generated_mb: float = 8.0
    image_storage_warn_versions_per_product: int = 50

    # Local rembg cut-out after OpenAI (not an extra OpenAI Batch step).
    # u2netp is the small model (~4 MB) so UAT's ~1 GB box can load it.
    rembg_enabled: bool = True
    rembg_model: str = "u2netp"
    rembg_require_alpha: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_worker_id(self) -> str:
        return self.processing_worker_id.strip() or f"worker_{uuid.uuid4().hex[:12]}"

    @property
    def effective_publish_worker_id(self) -> str:
        return self.publish_worker_id.strip() or f"publish_{uuid.uuid4().hex[:12]}"

    @property
    def effective_handoff_secret(self) -> str:
        return self.internal_handoff_secret or self.app_secret

    @property
    def effective_token_encryption_key(self) -> str:
        return self.token_encryption_key or self.app_secret_key


settings = Settings()
