---
schema_version: 1
open_count: 1
waived_count: 0
fixed_count: 0
total_count: 1
last_updated: 2026-09-12T15:26:25.538Z
---

# Broken Windows Ledger

> Cross-phase defect register. With `workflow.windows_enforce` enabled, `/gsd-ship` blocks while `open_count > 0`.
> Waive with `gsd-tools windows waive <id> "<reason>"` (reason required).
> Mark fixed with `gsd-tools windows fixed <id>`.

| id | phase | kind | file | line | description | status | reason | recorded_at | resolved_at |
|----|-------|------|------|------|-------------|--------|--------|-------------|-------------|
| 1 | 01 | unrun-verify | tests/exporter/test_wealthfolio_client.py |  | pytest blocked because checkout runtime dependencies are not installed | open |  | 2026-09-12T15:26:25.538Z |  |

````json
[
  {
    "id": 1,
    "kind": "unrun-verify",
    "phase": "01",
    "file": "tests/exporter/test_wealthfolio_client.py",
    "line": null,
    "description": "pytest blocked because checkout runtime dependencies are not installed",
    "status": "open",
    "reason": "",
    "recorded_at": "2026-09-12T15:26:25.538Z",
    "resolved_at": null
  }
]
````
