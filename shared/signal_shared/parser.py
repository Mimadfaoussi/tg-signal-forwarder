from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from signal_shared.models import Signal, TakeProfit

_DEFAULT_QUOTE_ASSETS = ["USDT"]

_ENTRY_RE = re.compile(r"\bEntry\s*(\d+)?\s*:\s*([\d.]+)", re.IGNORECASE)
# "TP1: 0.0185 (2.38%)" (AL-MAHWASHI style) or "T1: 0.14042 (4.79%)" (Suhaib
# AlMashhadani style). Only the first parenthesised group is captured as the
# percent -- a second one (e.g. a signed P&L like "(-5.34%)") is left alone.
_TP_RE = re.compile(
    r"\bT(?:P)?\s*(\d+)\s*:\s*([\d.]+)\s*(?:\(\s*([\d.]+)\s*%\s*\))?", re.IGNORECASE
)
# "Stop: 0.01043 (5m)" or "SL: 0.12685 (4h) (-5.34%)" -- same reasoning:
# only the first parenthesised group becomes stop_note.
_STOP_RE = re.compile(r"\b(?:Stop|SL)\s*:\s*([\d.]+)\s*(?:\(([^)]*)\))?", re.IGNORECASE)
_DATE_RE = re.compile(r"\bDate\s*:\s*.*?(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def _build_pair_pattern(quote_assets: list[str]) -> re.Pattern[str]:
    quotes = "|".join(re.escape(q) for q in quote_assets)
    # Two known channel styles: "#SAGA/USDT" and "PAIR: ARB/USDT" (no #).
    return re.compile(rf"(?:#|\bPAIR\s*:\s*)([A-Z0-9]{{2,15}})/({quotes})\b", re.IGNORECASE)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_date(text: str) -> date | None:
    match = _DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_signal(
    text: str,
    *,
    signal_id: str,
    source_chat_id: int,
    source_message_id: int,
    received_at: datetime,
    raw_entities: list[dict[str, Any]] | None = None,
    quote_assets: list[str] | None = None,
) -> Signal:
    normalized = _normalize(text)
    quotes = quote_assets or _DEFAULT_QUOTE_ASSETS
    pair_re = _build_pair_pattern(quotes)

    base: str | None = None
    quote: str | None = None
    pair: str | None = None
    pair_match = pair_re.search(normalized)
    if pair_match:
        base = pair_match.group(1).upper()
        quote = pair_match.group(2).upper()
        pair = f"{base}/{quote}"

    entries: list[Decimal] = []
    entry_matches = sorted(
        ((int(m.group(1)) if m.group(1) else 1, m) for m in _ENTRY_RE.finditer(normalized)),
        key=lambda pair_: pair_[0],
    )
    for _, m in entry_matches:
        value = _to_decimal(m.group(2))
        if value is not None:
            entries.append(value)

    take_profits: list[TakeProfit] = []
    for m in _TP_RE.finditer(normalized):
        price = _to_decimal(m.group(2))
        if price is None:
            continue
        percent = _to_decimal(m.group(3)) if m.group(3) else None
        take_profits.append(TakeProfit(index=int(m.group(1)), price=price, percent=percent))
    take_profits.sort(key=lambda tp: tp.index)

    stop: Decimal | None = None
    stop_note: str | None = None
    stop_match = _STOP_RE.search(normalized)
    if stop_match:
        stop = _to_decimal(stop_match.group(1))
        note = stop_match.group(2)
        stop_note = note.strip() if note and note.strip() else None

    signal_date = _parse_date(normalized)

    parse_ok = bool(pair and entries and take_profits and stop is not None)

    return Signal(
        signal_id=signal_id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        pair=pair,
        base=base,
        quote=quote,
        entries=entries,
        take_profits=take_profits,
        stop=stop,
        stop_note=stop_note,
        signal_date=signal_date,
        raw_text=text,
        raw_entities=raw_entities or [],
        received_at=received_at,
        parse_ok=parse_ok,
    )
