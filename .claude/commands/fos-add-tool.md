# Add a new tool to the Quorum operator agent

Arguments: `$ARGUMENTS` — tool name and short description, e.g. `"get_proposal_history Returns the last N proposals from the audit log"`

Parse `$ARGUMENTS` to extract:
- **tool_name**: snake_case identifier
- **description**: one-line description of what the tool does

Then execute all five required steps in order. Do not skip any — missing a step breaks the test suite.

---

## Step 1 — Implement the tool in `fos_agent/agent.py`

Add a private method `_<tool_name>(self, ...)` to the `FOSAgent` class (around line 375, after the existing `_write_audit_log` method).

The method must:
- Accept keyword arguments that match the JSON schema you'll define in Step 2
- Return a JSON string via `json.dumps({...})`
- Always include `"success": True/False` in the return value

## Step 2 — Add the JSON schema to `TOOLS` in `fos_agent/agent.py`

Add an entry to the `TOOLS` list (around line 109). Follow the exact same structure as existing tools:

```python
{
    "name": "<tool_name>",
    "description": "...",
    "input_schema": {
        "type": "object",
        "properties": {
            "<param>": {"type": "string", "description": "..."},
        },
        "required": ["<param>"],
    },
},
```

## Step 3 — Add a dispatch branch in `FOSAgent.dispatch()`

In the `dispatch()` method (around line 228), add an `elif` branch:

```python
elif tool_name == "<tool_name>":
    return self._<tool_name>(**inputs)
```

Keep the branches in the same order as the `TOOLS` list.

## Step 4 — Update the test file `test_fos_agent.py`

Two places to update:

**a) `dispatchable` set** — Find the set that lists all dispatchable tool names and add `"<tool_name>"` to it.

**b) Tool count assertion** — Find the line that asserts the total number of tools (e.g. `eq(len(TOOLS), 6)`) and increment it by 1.

## Step 5 — Run tests to verify

```
python3 test_fos_agent.py
```

All 32+ tests must pass. If they don't, fix the issue before reporting done.

---

## After completing all steps

Report:
- The tool name and its input parameters
- Which lines were added in each file
- Confirm `python3 test_fos_agent.py` passes
