from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TeamUser:
    slug: str
    name: str
    admin_token: str | None
    webhook_secret: str | None


def _load_team_users() -> tuple[TeamUser, ...]:
    raw = os.environ.get("KONDO_FOLK_USERS_JSON")
    if not raw:
        return ()
    parsed: Any = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("KONDO_FOLK_USERS_JSON must be a JSON array")
    users: list[TeamUser] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("Each KONDO_FOLK_USERS_JSON entry must be an object")
        slug = str(item.get("slug") or "").strip().lower()
        if not slug:
            raise ValueError("Each KONDO_FOLK_USERS_JSON entry needs a slug")
        users.append(
            TeamUser(
                slug=slug,
                name=str(item.get("name") or slug).strip(),
                admin_token=str(item["admin_token"]) if item.get("admin_token") else None,
                webhook_secret=str(item["webhook_secret"]) if item.get("webhook_secret") else None,
            )
        )
    return tuple(users)


@dataclass(frozen=True)
class Settings:
    folk_api_key: str | None = os.environ.get("FOLK_API_KEY")
    folk_base_url: str = os.environ.get("FOLK_BASE_URL", "https://api.folk.app/v1")
    folk_group_id: str | None = os.environ.get("FOLK_GROUP_ID")
    folk_claims_professionals_group_id: str | None = os.environ.get(
        "FOLK_GROUP_CLAIMS_PROFESSIONALS_ID"
    )
    folk_distribution_partners_group_id: str | None = os.environ.get(
        "FOLK_GROUP_DISTRIBUTION_PARTNERS_ID"
    )
    folk_tpas_subrogation_attorneys_group_id: str | None = os.environ.get(
        "FOLK_GROUP_TPAS_SUBROGATION_ATTORNEYS_ID"
    )
    folk_reminder_visibility: str = os.environ.get("FOLK_REMINDER_VISIBILITY", "private")
    folk_assigned_user_email: str | None = os.environ.get("FOLK_ASSIGNED_USER_EMAIL")

    openai_api_key: str | None = os.environ.get("OPENAI_API_KEY")
    openai_model: str = os.environ.get("KONDO_FOLK_OPENAI_MODEL", "gpt-4o-mini")
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY")
    anthropic_model: str = os.environ.get("KONDO_FOLK_ANTHROPIC_MODEL", "claude-sonnet-4-6")
    ai_provider: str = os.environ.get("KONDO_FOLK_AI_PROVIDER", "auto")

    database_path: Path = Path(os.environ.get("KONDO_FOLK_DB", "kondo_folk_sync.db"))
    prompt_path: Path = Path(
        os.environ.get(
            "KONDO_FOLK_PROMPT_PATH",
            "kondo_folk_sync/prompts/crm_analysis.md",
        )
    )
    dry_run: bool = _bool_env("KONDO_FOLK_DRY_RUN", True)
    webhook_secret: str | None = os.environ.get("KONDO_WEBHOOK_SECRET")
    admin_token: str | None = os.environ.get("KONDO_FOLK_ADMIN_TOKEN")
    team_users: tuple[TeamUser, ...] = _load_team_users()
    reconcile_interval_minutes: int = int(os.environ.get("KONDO_FOLK_RECONCILE_INTERVAL_MINUTES", "0"))
    review_mode: bool = _bool_env("KONDO_FOLK_REVIEW_MODE", False)
    worker_enabled: bool = _bool_env("KONDO_FOLK_WORKER_ENABLED", True)
    worker_interval_seconds: float = float(os.environ.get("KONDO_FOLK_WORKER_INTERVAL_SECONDS", "5"))
    worker_batch_size: int = int(os.environ.get("KONDO_FOLK_WORKER_BATCH_SIZE", "1"))
    processing_timeout_seconds: int = int(os.environ.get("KONDO_FOLK_PROCESSING_TIMEOUT_SECONDS", "120"))
    folk_request_spacing_seconds: float = float(os.environ.get("KONDO_FOLK_REQUEST_SPACING_SECONDS", "0.25"))
    default_timezone: str = os.environ.get("KONDO_FOLK_TIMEZONE", "America/Los_Angeles")
    default_followup_hour: int = int(os.environ.get("KONDO_FOLK_FOLLOWUP_HOUR", "9"))


settings = Settings()
