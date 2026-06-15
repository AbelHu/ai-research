"""Configuration loading (design spec section 13).

Loads environment variables from `.env` and the role->provider mapping from
`config/models.yaml`. Secrets (tokens) live only in the environment and are
never persisted.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.security import Secret

# Repo root = backend/app/config/settings.py -> parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODELS_CONFIG = REPO_ROOT / "config" / "models.yaml"


class Settings(BaseSettings):
    """Environment-backed settings. Reads from process env and `.env`."""

    github_models_token: Secret | None = None
    github_org: str | None = None
    bing_search_key: Secret | None = None
    openai_api_key: Secret | None = None

    # Telegram bot (channel adapter, §10). The bot token authenticates Bot API
    # calls; the optional webhook secret verifies inbound webhook requests.
    telegram_bot_token: Secret | None = None
    telegram_webhook_secret: Secret | None = None

    # The owner's GitHub login (§10.1). Optional pin: when set, a chat pairing
    # must approve the device flow as exactly this account to be admitted as
    # owner. When unset, the owner login is established by the host-side pairing.
    owner_github_login: str | None = None

    data_dir: Path = REPO_ROOT / "data"
    models_config_path: Path = DEFAULT_MODELS_CONFIG

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class ProviderConfig(BaseModel):
    kind: str
    model: str
    api_mode: str = "chat_completions"
    base_url: str | None = None
    api_key_env: str | None = None
    org_env: str | None = None


class ModelsConfig(BaseModel):
    roles: dict[str, str]
    providers: dict[str, ProviderConfig]

    def provider_for_role(self, role: str) -> ProviderConfig:
        if role not in self.roles:
            raise KeyError(f"role {role!r} is not defined in models.yaml")
        provider_name = self.roles[role]
        if provider_name not in self.providers:
            raise KeyError(f"role {role!r} maps to unknown provider {provider_name!r}")
        return self.providers[provider_name]


def load_models_config(path: Path | None = None) -> ModelsConfig:
    cfg_path = path or get_settings().models_config_path
    with open(cfg_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return ModelsConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
