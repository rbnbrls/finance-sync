"""Stable market-data identifiers for canonical securities."""

from __future__ import annotations

from typing import Any


def quote_identifier(security: Any) -> tuple[str | None, str]:
    """Return the best provider identifier for market data.

    Exchange-qualified tickers (for example ``BESI:XAMS``) preserve the
    listing context that an ISIN-only request loses at some providers.  Use
    the canonical ticker first and fall back to globally stable identifiers.
    """
    ticker = str(getattr(security, "ticker", None) or "").strip()
    if ticker:
        return ticker, "ticker"
    isin = str(getattr(security, "isin", None) or "").strip()
    if isin:
        return isin, "isin"
    figi = str(getattr(security, "figi", None) or "").strip()
    if figi:
        return figi, "figi"
    return None, "ticker"
