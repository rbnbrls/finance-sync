# Pyright warning debt

Release 12 ratchets the source warning budget from 69 to 60. The remaining
warnings are classified from `uv run pyright --outputjson src`:

| Module | Private usage | Missing stubs | Argument type | Total |
| --- | ---: | ---: | ---: | ---: |
| `services/subscription_detector/__init__.py` | 16 | 0 | 0 | 16 |
| `cli.py` | 7 | 0 | 0 | 7 |
| `services/subscription_detector/pattern_detector.py` | 7 | 0 | 0 | 7 |
| `services/subscription_detector/service.py` | 7 | 0 | 0 | 7 |
| `exporter/transaction_mapper.py` | 6 | 0 | 0 | 6 |
| `services/subscription_detector/merchant_classifier.py` | 6 | 0 | 0 | 6 |
| `services/pattern_clustering.py` | 4 | 0 | 0 | 4 |
| `worker/scheduler.py` | 0 | 4 | 0 | 4 |
| `connectors/plaid_like.py` | 0 | 0 | 1 | 1 |
| `enrichment/gateway.py` | 0 | 0 | 1 | 1 |
| `worker/schedule_runner.py` | 1 | 0 | 0 | 1 |
| **Total** | **54** | **4** | **2** | **60** |

The nine-warning reduction addressed concrete type contracts rather than
silencing diagnostics: provider payload fields are validated, enrichment
DTOs receive explicit strings, invalid FX responses are rejected, the Intel
typing stub no longer constructs an incomplete model, and the Actual Budget
adapter handles optional credentials and login methods safely.

The remaining private-usage warnings are legacy cross-module helper exports;
the four stub warnings come from APScheduler, and the two remaining argument
warnings are isolated at external/provider boundaries. New read, persistence,
and sync-stage modules remain warning-free.
