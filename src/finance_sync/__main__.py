"""CLI entry point — ``python -m finance_sync``.

Usage::

    python -m finance_sync reconcile [options]
"""

from __future__ import annotations

from finance_sync.cli import main

if __name__ == "__main__":
    main()
