# Pyright warning debt

The source warning budget is 60. The current warnings are classified from
`uv run pyright --outputjson src`:

| Diagnostic class | Unknown type | Optional/member | Call/attribute | Argument type | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Source modules | 0 | 5 | 7 | 10 | 21 |
| **Total** | **0** | **5** | **7** | **10** | **21** |

The warning count is intentionally kept below the ratchet so the gate
reports newly introduced warnings instead of failing on pre-existing
debt. The next ratchet should reduce the unknown-type diagnostics first,
starting with the Bunq connector and Wealthfolio/read service boundaries.
