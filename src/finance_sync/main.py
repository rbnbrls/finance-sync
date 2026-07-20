"""ASGI entrypoint for finance-sync.

Usage::

    uv run uvicorn finance_sync.main:app --reload
"""

from __future__ import annotations

from finance_sync.app import create_app

app = create_app()
