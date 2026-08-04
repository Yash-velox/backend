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
    openai_image_model: str = "gpt-image-1"
    # Hard cap so a hung OpenAI call cannot block the worker forever.
    openai_image_timeout_seconds: float = 180.0
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
    manual_batch_product_limit: int = 50

    shopify_image_download_timeout_seconds: int = 60
    shopify_image_max_download_mb: int = 30
    shopify_api_version: str = "2026-07"
    # Dev-only fallback when shop encrypted token is empty. Never used when APP_ENV != dev.
    shopify_dev_access_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_worker_id(self) -> str:
        return self.processing_worker_id.strip() or f"worker_{uuid.uuid4().hex[:12]}"

    @property
    def effective_handoff_secret(self) -> str:
        return self.internal_handoff_secret or self.app_secret

    @property
    def effective_token_encryption_key(self) -> str:
        return self.token_encryption_key or self.app_secret_key


settings = Settings()
