"""CLI entrypoint for the worker process.

Usage::

    python -m finance_sync.worker
"""

from __future__ import annotations

from finance_sync.worker import main

if __name__ == "__main__":
    main()
