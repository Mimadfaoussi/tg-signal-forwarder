from __future__ import annotations

from signal_shared.settings import BaseAppSettings


class ListenerSettings(BaseAppSettings):
    tg_api_id: int
    tg_api_hash: str
    source_chat: str
    catchup_limit: int = 50
    session_path: str = "/data/session/listener.session"
