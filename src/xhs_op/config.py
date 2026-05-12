from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class AccountConfig:
    name: str
    cookie_path: Path
    proxy_url: str | None
    persona_default: str
    timezone: str = "Asia/Shanghai"


# LiteLLM model-id conventions: <provider>/<model>.
_DEFAULT_MODEL_ROUTING: dict[str, str] = {
    "villa_hook": "anthropic/claude-sonnet-4-6",
    "villa_body": "dashscope/qwen-max",
    "stock_digest": "openai/gpt-5",
    "stock_hottake": "anthropic/claude-sonnet-4-6",
    "bulk": "lm_studio/local",
}

# Primary first, fallback after.
_DEFAULT_IMAGE_MODELS: list[str] = [
    "gemini/gemini-2.5-flash-image",
    "openai/gpt-image-1",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=(),
    )

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    dashscope_api_key: str = ""
    lmstudio_base_url: str = "http://localhost:1234/v1"

    banna_proxy_url: str = ""
    stock_proxy_url: str = ""

    x_username: str = ""
    x_password: str = ""
    x_email: str = ""

    model_routing: dict[str, str] = Field(default_factory=lambda: dict(_DEFAULT_MODEL_ROUTING))
    image_models: list[str] = Field(default_factory=lambda: list(_DEFAULT_IMAGE_MODELS))

    @property
    def accounts(self) -> dict[str, AccountConfig]:
        return {
            "banna": AccountConfig(
                name="banna",
                cookie_path=Path("data/cookies/banna.json"),
                proxy_url=self.banna_proxy_url or None,
                persona_default="villa",
                timezone="Asia/Shanghai",
            ),
            "stock": AccountConfig(
                name="stock",
                cookie_path=Path("data/cookies/stock.json"),
                proxy_url=self.stock_proxy_url or None,
                persona_default="stock_digest",
                timezone="Asia/Shanghai",
            ),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
