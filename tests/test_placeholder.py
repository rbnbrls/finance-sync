"""Placeholder tests to verify the toolchain is wired correctly."""

from finance_sync import __version__


def test_version() -> None:
    """Package exposes a version string."""
    assert __version__ == "0.1.0"


async def test_async_placeholder() -> None:
    """Async tests work (pytest-asyncio auto mode)."""
    assert await async_identity(42) == 42


async def async_identity(x: int) -> int:
    """Return the input unchanged."""
    return x
