# Pyright warning debt

The source warning budget is 138. The current warnings are classified from
`uv run pyright --outputjson src`:

| Diagnostic class | Unknown type | Optional/member | Call/attribute | Argument type | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Source modules | 116 | 5 | 7 | 10 | 138 |
| **Total** | **116** | **5** | **7** | **10** | **138** |

The warning count is intentionally recorded as the current baseline so the
gate reports newly introduced warnings instead of failing on pre-existing
debt. The next ratchet should reduce the unknown-type diagnostics first,
starting with the Bunq connector and Wealthfolio/read service boundaries.
