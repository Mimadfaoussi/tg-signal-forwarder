from __future__ import annotations

from signal_shared.settings import BaseAppSettings, split_csv


class ListenerSettings(BaseAppSettings):
    tg_api_id: int
    tg_api_hash: str
    source_chat: str
    catchup_limit: int = 50
    session_path: str = "/data/session/listener.session"

    @property
    def source_chats_list(self) -> list[str]:
        return split_csv(self.source_chat)
