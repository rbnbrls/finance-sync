"""Fixture payloads for the market-intelligence adapters.

Shared by the adapter unit tests (``tests/test_intel_adapters.py``) and
usable by the scheduler/ingestion integration tests.  The RSS payload
mirrors the live SEC press-releases feed; the OpenBB payloads mirror
the shapes the OpenBB platform serves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Path to the SEC press-releases RSS fixture.
SEC_PRESS_RSS_FIXTURE = Path(__file__).parent / "sec_press_releases.rss"

SEC_PRESS_RSS_XML = SEC_PRESS_RSS_FIXTURE.read_text(encoding="utf-8")


def openbb_news_payload() -> dict[str, Any]:
    """OpenBB ``/market/news`` response shape (results array)."""
    return {
        "results": [
            {
                "id": "news-12345",
                "url": "https://example.com/news/12345",
                "title": "AAPL beats estimates in Q3",
                "summary": (
                    "Apple reported quarterly revenue above analyst "
                    "expectations, driven by services growth."
                ),
                "published_utc": "2026-08-18T12:00:00Z",
                "language": "en",
                "symbols": ["AAPL"],
            },
            {
                "id": "news-12346",
                "url": "https://example.com/news/12346",
                "title": "MSFT announces dividend increase",
                "summary": "Microsoft raised its quarterly dividend by 10%.",
                "published_utc": "2026-08-18T13:30:00Z",
                "language": "en",
                "symbols": ["MSFT"],
            },
        ]
    }


def openbb_earnings_payload() -> dict[str, Any]:
    """OpenBB ``/market/earnings`` response shape (results array)."""
    return {
        "results": [
            {
                "id": "earn-1",
                "period": "2026Q3",
                "date": "2026-08-18",
                "eps_estimate": "1.25",
                "revenue_estimate": "94000000000",
            },
            {
                "id": "earn-2",
                "period": "2026Q4",
                "date": "2026-11-15",
                "eps_estimate": "1.40",
                "revenue_estimate": "98000000000",
            },
        ]
    }
