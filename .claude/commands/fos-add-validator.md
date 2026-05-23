# Add a new Quorum validator layer

Arguments: `$ARGUMENTS` — validator name and purpose, e.g. `"payroll Recurring scheduled ADA distributions to active members"`

Parse `$ARGUMENTS` to extract:
- **name**: snake_case validator name (e.g. `payroll`)
- **purpose**: one-sentence description

This skill scaffolds a complete new Aiken validator that integrates with the existing Quorum three-layer stack.

---

## Step 1 — Add shared types to `identity_registry/lib/fos_types.ak`

If the validator needs new on-chain types (datums, redeemers, action variants), add them here. ALL cross-validator types live in this file.

If the new validator should be triggerable by governance, add a new `ProposalAction` variant:
```aiken
// inside the ProposalAction type union
<Name> { <fields> }
```

Ask the user what fields the new action should carry before writing.

## Step 2 — Create `identity_registry/validators/<name>.ak`

Scaffold the validator file. Every Quorum validator must follow these invariants:

**Reference input pattern** (read a lower layer without spending it):
```aiken
fn load_<lower_layer>(reference_inputs: List<Input>, expected_hash: ByteArray) -> <Datum> {
  let input = list.find(reference_inputs, fn(i) { ... }) |> option.expect("...")
  // check ScriptCredential(hash) matches expected_hash
  // parse and return datum
}
```

**Continuing output invariant** — every spend must produce exactly one output back to the same script address with a valid datum.

**Validator skeleton**:
```aiken
use aiken/transaction.{ScriptContext, Transaction}
use fos_types.{...}

validator {
  fn <name>(datum: <Datum>, redeemer: <Redeemer>, ctx: ScriptContext) -> Bool {
    let Transaction { inputs, reference_inputs, outputs, validity_range, .. } = ctx.transaction
    when redeemer is {
      // each branch
    }
  }
}
```

Include at least 3 test blocks at the bottom covering: happy path, rejection of invalid redeemer, continuing output check.

## Step 3 — Add the script hash config to `fos_agent/config.py`

```python
<NAME>_SCRIPT_HASH = os.environ.get("FOS_<NAME>_SCRIPT_HASH", "")
```

And add it to `is_configured()` if the agent requires it.

## Step 4 — Add Python type mirrors to `fos_agent/types.py`

Add a `<Name>Datum` dataclass with a `from_cbor_hex()` classmethod, mirroring the Aiken datum type. Follow the existing CBOR decoding pattern (`_alt`, `_f`, `_decode`).

Add serialization to `fos_agent/datums.py` — a `<name>_datum_cbor_hex()` function.

## Step 5 — Add an inline example to `rag_ingest.py`

At the bottom of `INLINE_EXAMPLES` in `rag_ingest.py`, add the new validator as a curated example so the builder agent can generate similar contracts:

```python
{
    "source": "example",
    "title": "Quorum <Name> Validator",
    "content": """...<full .ak source>...""",
    "is_aiken": True,
},
```

## Step 6 — Update `CLAUDE.md`

Add the new file to the three-layer stack diagram and document any new invariants.

---

## After completing all steps

1. Run `PATH="/Users/tuanluong/Downloads/files:$PATH" aiken check` from `identity_registry/` — must pass.
2. Run `python3 test_fos_agent.py` — must pass.
3. Report the new validator's address derivation command and what env var to set.
