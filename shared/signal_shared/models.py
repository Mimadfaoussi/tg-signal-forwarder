from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class TakeProfit(BaseModel):
    index: int
    price: Decimal
    percent: Decimal | None = None


class Signal(BaseModel):
    signal_id: str
    source_chat_id: int
    source_message_id: int
    pair: str | None = None
    base: str | None = None
    quote: str | None = None
    entries: list[Decimal] = []
    take_profits: list[TakeProfit] = []
    stop: Decimal | None = None
    stop_note: str | None = None
    signal_date: date | None = None
    raw_text: str
    raw_entities: list[dict[str, Any]] = []
    received_at: datetime
    parse_ok: bool = True
