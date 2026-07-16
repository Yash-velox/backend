from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    app_secret: str = "change-me"
    app_secret_key: str = "change-me"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    host: str = "0.0.0.0"
    port: int = 8080

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
