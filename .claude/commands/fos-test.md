# Run all FOS tests

Run both test suites for this project and report results clearly.

## Step 1 — Python FOS agent tests

Run:
```
python3 test_fos_agent.py
```

Parse the output. Count passed/failed. If any tests fail, show the exact failing test name and error message.

## Step 2 — Python builder agent tests (if test_agent.py exists)

Run:
```
python3 test_agent.py
```

Parse results the same way.

## Step 3 — Aiken type-check (if `aiken` is on PATH or the mock binary exists)

From the `identity_registry/` directory, run:
```
PATH="/Users/tuanluong/Downloads/files:$PATH" aiken check
```

Report any errors.

## Report format

Print a concise summary table:

| Suite | Passed | Failed |
|---|---|---|
| fos_agent (Python) | N | N |
| agent (Python)     | N | N |
| Aiken check        | OK / ERRORS | — |

If everything passes, say so in one line. If anything fails, list each failure with the test name and error, then suggest what to fix based on the CLAUDE.md architecture.

Do not run the tests silently — show the raw output for any suite that has failures.
