"""Authentication and configuration management for Intervals.icu API."""

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv, set_key
from pydantic import Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DeleteMode = Literal["safe", "full", "none"]
VALID_DELETE_MODES: tuple[DeleteMode, ...] = ("safe", "full", "none")
AuthMode = Literal["api_key", "bearer"]

# In bearer (OAuth) mode the API resolves athlete "0" to the athlete the token belongs to,
# so a tool never needs, or trusts, a process-wide athlete ID.
TOKEN_OWNER_ATHLETE_ID = "0"


def normalize_athlete_id(value: object) -> str:
    """Normalize an Intervals.icu athlete ID to its canonical ``i<digits>`` form.

    The API shows athlete IDs with an ``i`` prefix (``i987654``), but some responses
    (e.g. the OAuth token response) carry the bare number. Both sides of any comparison
    should go through this function. Non-numeric IDs are returned stripped, unchanged.
    """
    text = str(value).strip() if value is not None else ""
    if text[:1] in ("i", "I") and text[1:].isdigit():
        return "i" + text[1:]
    if text.isdigit():
        return "i" + text
    return text


class ICUConfig(BaseSettings):
    """Intervals.icu API configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    intervals_icu_api_key: str = Field(default="", repr=False)
    intervals_icu_athlete_id: str = ""
    intervals_icu_delete_mode: DeleteMode = "safe"

    # Per-request OAuth credentials. Private attributes are never read from the
    # environment or .env, so a bearer token only ever comes from an authenticated
    # request (see with_bearer_token), never from process-wide configuration.
    _bearer_token: str | None = PrivateAttr(default=None)
    _token_athlete_id: str | None = PrivateAttr(default=None)

    @property
    def auth_mode(self) -> AuthMode:
        """``bearer`` for a per-request OAuth token, ``api_key`` for HTTP Basic auth."""
        return "bearer" if self._bearer_token else "api_key"

    @property
    def bearer_token(self) -> str | None:
        """The per-request OAuth access token, or None in API-key mode."""
        return self._bearer_token

    @property
    def display_athlete_id(self) -> str:
        """The athlete ID to show in responses.

        In bearer mode requests go to athlete ``0``; this returns the token owner's
        real ID when it is known, so responses stay meaningful.
        """
        if self._bearer_token and self._token_athlete_id:
            return self._token_athlete_id
        return self.intervals_icu_athlete_id

    def with_bearer_token(self, token: str, athlete_id: str | None = None) -> "ICUConfig":
        """Return a copy of this config that authenticates with an OAuth bearer token.

        The copy drops the API key, so a bearer-mode request can never fall back to
        the operator's API key, and targets athlete ``0`` (the token owner).
        Non-credential settings (delete mode) are kept.
        """
        if not token:
            raise ValueError("A bearer token is required")
        copy = self.model_copy(
            update={
                "intervals_icu_api_key": "",
                "intervals_icu_athlete_id": TOKEN_OWNER_ATHLETE_ID,
            }
        )
        copy._bearer_token = token
        copy._token_athlete_id = normalize_athlete_id(athlete_id) if athlete_id else None
        return copy

    @field_validator("intervals_icu_delete_mode", mode="before")
    @classmethod
    def _normalize_delete_mode(cls, v: object) -> str:
        if v is None or v == "":
            return "safe"
        normalized = str(v).lower().strip()
        if normalized not in VALID_DELETE_MODES:
            raise ValueError(
                "INTERVALS_ICU_DELETE_MODE must be one of: "
                f"{', '.join(VALID_DELETE_MODES)}. Got: '{v}'"
            )
        return normalized


def load_config() -> ICUConfig:
    """Load configuration from .env file.

    Returns:
        ICUConfig instance with configuration from environment variables
    """
    load_dotenv()
    return ICUConfig()


def validate_credentials(config: ICUConfig) -> bool:
    """Check if credentials are properly configured.

    Args:
        config: ICUConfig instance to validate

    Returns:
        True if credentials are valid, False otherwise
    """
    if config.bearer_token:
        return True
    if not config.intervals_icu_api_key or config.intervals_icu_api_key == "your_api_key_here":
        return False
    # Do not reject specific athlete id values: any non-empty id may belong to a
    # real athlete (i123456 from .env.example is a valid id someone could own).
    # The API is the authority on whether credentials actually work.
    if not config.intervals_icu_athlete_id:
        return False
    return True


def update_env_key(api_key: str, athlete_id: str | None = None) -> None:
    """Update the .env file with new credentials.

    Args:
        api_key: New API key to save
        athlete_id: Optional athlete ID to save
    """
    env_path = Path.cwd() / ".env"

    # Create .env if it doesn't exist
    if not env_path.exists():
        env_path.touch()

    # Update API key
    set_key(str(env_path), "INTERVALS_ICU_API_KEY", api_key)
    os.environ["INTERVALS_ICU_API_KEY"] = api_key

    # Update athlete ID if provided
    if athlete_id:
        set_key(str(env_path), "INTERVALS_ICU_ATHLETE_ID", athlete_id)
        os.environ["INTERVALS_ICU_ATHLETE_ID"] = athlete_id
