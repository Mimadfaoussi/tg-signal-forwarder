from __future__ import annotations

from pydantic import field_validator
from signal_shared.settings import BaseAppSettings


class PublisherSettings(BaseAppSettings):
    tg_api_id: int
    tg_api_hash: str
    session_path: str = "/data/session/publisher.session"

    target_chat: str
    target_topic_id: int | None = None
    output_mode: str = "copy"
    dry_run: bool = False

    min_send_interval: float = 5.0
    send_jitter: float = 2.0
    max_sends_per_hour: int = 30
    max_flood_wait: int = 3600

    database_url: str

    @field_validator("target_topic_id", mode="before")
    @classmethod
    def _blank_topic_id_is_none(cls, value: object) -> object:
        # .env ships TARGET_TOPIC_ID= (blank) when no forum topic is used;
        # pydantic would otherwise try (and fail) to parse "" as an int.
        if value == "":
            return None
        return value
