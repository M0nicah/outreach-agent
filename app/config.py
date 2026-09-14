"""Application configuration, loaded from environment variables.

Why a dataclass instead of reading os.getenv() all over the codebase:

1. Every setting is declared in ONE place, so you can see the whole
   surface of the system at a glance.
2. Values get converted to the right type (int, bool, Path) exactly once.
3. Missing or malformed settings fail loudly at startup with a clear
   message, instead of failing deep inside a loop three stages later.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Project root = the folder containing this app/ package's parent.
# Used so relative paths in .env work no matter which directory you
# happen to run `python main.py` from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _get_bool(name: str, default: bool) -> bool:
    """Read an environment variable as a boolean.

    Accepts true/1/yes/on (any capitalisation) as True.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    """Read an environment variable as an int, with a clear error if it is not one."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a whole number, got {raw!r}. Fix it in your .env file."
        ) from exc


# Default model per provider, used when AI_MODEL is not set.
#
# We deliberately use Google's "-latest" alias rather than pinning a
# version number. Google retires specific versions (gemini-2.0-flash was
# retired and returned a 404), and the alias keeps working when they do.
#
# The "lite" model is the default because it has a more generous free-tier
# quota. Qualification is a structured classification task, not creative
# writing, so the smaller model is well suited to it. For a harder job,
# set AI_MODEL=gemini-flash-latest in .env.
DEFAULT_MODELS = {
    "gemini": "gemini-flash-lite-latest",
    "openai": "gpt-4o-mini",
}


@dataclass(frozen=True)
class Config:
    """All application settings. Frozen so nothing can mutate it at runtime."""

    # AI -- provider-agnostic, so switching is a one-line .env change.
    ai_provider: str
    ai_api_key: str | None
    ai_model: str
    seconds_between_ai_calls: int

    # Paths
    excel_path: Path

    # Logging
    log_level: str

    # Email safety limits (enforced from Stage 8; declared now so the
    # shape of the config does not churn later)
    daily_send_limit: int
    batch_size: int
    seconds_between_sends: int
    dry_run: bool

    # Gmail OAuth (Stage 8)
    gmail_client_id: str | None
    gmail_client_secret: str | None
    gmail_redirect_uri: str

    @property
    def has_ai_key(self) -> bool:
        """True when an AI key is configured. Checked before any AI work."""
        return bool(self.ai_api_key)

    def require_ai_key(self) -> str:
        """Return the AI key, or raise a helpful error if it is missing.

        Called at the top of any function about to hit the API, so the
        failure happens before we waste time on other work.
        """
        if not self.ai_api_key:
            where = {
                "gemini": "https://aistudio.google.com/apikey  (free tier, no card needed)",
                "openai": "https://platform.openai.com/api-keys  (paid)",
            }.get(self.ai_provider, "your provider's dashboard")
            raise ConfigError(
                f"No API key set for provider '{self.ai_provider}'. "
                f"Add AI_API_KEY to your .env file.\nGet one at: {where}"
            )
        return self.ai_api_key


def load_config() -> Config:
    """Load settings from .env plus the real environment.

    Real environment variables win over .env, which is the normal
    convention and makes one-off overrides easy:
        LOG_LEVEL=DEBUG python main.py
    """
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.debug("Loaded settings from %s", env_path)
    else:
        logger.warning(
            "No .env file found at %s -- using defaults. "
            "Copy .env.example to .env to configure the system.",
            env_path,
        )

    # Resolve the Excel path relative to the project root if it is not absolute.
    raw_excel = os.getenv("EXCEL_PATH", "data/internship_outreach.xlsx").strip()
    excel_path = Path(raw_excel)
    if not excel_path.is_absolute():
        excel_path = PROJECT_ROOT / excel_path

    # Provider selection, with backward compatibility: if someone still has
    # OPENAI_API_KEY set from an earlier version, honour it.
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()

    api_key = os.getenv("AI_API_KEY") or None
    if not api_key:
        # Fall back to a provider-specific variable name.
        api_key = (
            os.getenv("GEMINI_API_KEY")
            if provider == "gemini"
            else os.getenv("OPENAI_API_KEY")
        ) or None

    model = os.getenv("AI_MODEL", "").strip()
    if not model:
        model = DEFAULT_MODELS.get(provider, "gemini-flash-lite-latest")

    return Config(
        ai_provider=provider,
        ai_api_key=api_key,
        ai_model=model,
        seconds_between_ai_calls=_get_int("SECONDS_BETWEEN_AI_CALLS", 4),
        excel_path=excel_path,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        daily_send_limit=_get_int("DAILY_SEND_LIMIT", 10),
        batch_size=_get_int("BATCH_SIZE", 5),
        seconds_between_sends=_get_int("SECONDS_BETWEEN_SENDS", 30),
        dry_run=_get_bool("DRY_RUN", True),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID") or None,
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET") or None,
        gmail_redirect_uri=os.getenv(
            "GMAIL_REDIRECT_URI", "http://localhost:8080/"
        ).strip(),
    )
