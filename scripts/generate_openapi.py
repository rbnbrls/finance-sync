#!/usr/bin/env python3
# pyright: basic
# ruff: noqa: T201
"""Generate the finance-sync OpenAPI document.

Usage
-----
    uv run python scripts/generate_openapi.py --output openapi.json

Writes the OpenAPI 3.x document produced by the FastAPI application factory
to a file (stdout when ``--output`` is omitted).  The document is dumped with
sorted keys so diffs between revisions are stable.

Importing the app and calling ``create_app().openapi()`` does **not** start
the application lifespan, and requires no secrets, database or external
services: :class:`finance_sync.config.settings.Settings` applies its built-in
defaults, and the schema is derived purely from the route definitions.  This
is what lets the CI OpenAPI diff gate run without any repository secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from finance_sync.app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write the OpenAPI document to (default: stdout).",
    )
    args = parser.parse_args()

    spec = create_app().openapi()
    payload = json.dumps(spec, indent=2, sort_keys=True) + "\n"

    if args.output is None:
        sys.stdout.write(payload)
    else:
        args.output.write_text(payload, encoding="utf-8")
        print(f"Wrote {args.output} ({args.output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
