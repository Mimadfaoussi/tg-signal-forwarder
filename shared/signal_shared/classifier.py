from __future__ import annotations

import re


def _build_pair_pattern(quote_assets: list[str]) -> re.Pattern[str]:
    quotes = "|".join(re.escape(q) for q in quote_assets)
    return re.compile(rf"#[A-Z0-9]{{2,15}}/(?:{quotes})\b", re.IGNORECASE)


_ENTRY_RE = re.compile(r"\bEntry\s*1?\s*:\s*[\d.]+", re.IGNORECASE)
_TP_RE = re.compile(r"\bTP\s*\d+\s*:\s*[\d.]+", re.IGNORECASE)
_STOP_RE = re.compile(r"\bStop\s*:\s*[\d.]+", re.IGNORECASE)

_DEFAULT_QUOTE_ASSETS = ["USDT"]


def is_signal(text: str | None, quote_assets: list[str] | None = None) -> bool:
    if not text:
        return False

    quotes = quote_assets or _DEFAULT_QUOTE_ASSETS
    pair_re = _build_pair_pattern(quotes)

    return bool(
        pair_re.search(text)
        and _ENTRY_RE.search(text)
        and _TP_RE.search(text)
        and _STOP_RE.search(text)
    )
