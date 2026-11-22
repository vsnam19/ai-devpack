| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |
|---|---|---|---|---|---|
| R1 | COR | major | module.cpp:1 | `values[index]` has no bounds check. | Validate the index before reading. |
VERDICT: REQUEST_CHANGES
