"""Tests for the ASGI entrypoint module."""

from __future__ import annotations


def test_main_import() -> None:
    """The ASGI entrypoint module can be imported without error."""
    from finance_sync import main

    assert main is not None


def test_main_app_is_callable() -> None:
    """The `app` object in main is a FastAPI application."""
    from finance_sync.main import app

    assert app.title == "finance-sync"
    assert app.version == "0.1.0"
