# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Three things in one repo:

1. **Builder agent** (`agent.py`, `rag.py`, `rag_ingest.py`) — A Claude-powered coding agent that generates Aiken smart contracts from natural language, with an optional RAG layer for doc retrieval.

2. **Quorum** (`identity_registry/`) — A three-layer on-chain governance protocol built in Aiken: identity registry → governance → treasury.

3. **Quorum operator agent** (`fos_agent/`) — A Claude-powered autonomous operator that monitors the deployed Quorum contracts on-chain, votes on proposals, executes passed proposals, and releases treasury funds.

---

## Commands

### Python agent

```bash
# Run the agent
python3 agent.py "build a vesting contract"
CARDANO_PROJECT_PATH=./myproject python3 agent.py "add a multi-sig check"

# Run tests (custom runner — no pytest; requires real aiken CLI or mock on PATH)
python3 test_agent.py

# Build the RAG knowledge base
python3 rag_ingest.py                  # full scrape of aiken-lang.org
python3 rag_ingest.py --offline        # inline examples only (no network)
python3 rag_ingest.py --clear          # wipe DB then re-ingest
python3 rag_ingest.py --stats
```

### Quorum operator agent

```bash
# One-shot: check state and act on any pending proposals
python3 -c "from fos_agent import run_fos_agent; run_fos_agent('Check FOS state and take any pending actions')"

# Continuous monitor (polls every 5 minutes)
python3 -c "from fos_agent import run_monitor; run_monitor(300)"

# Run tests (no API key required — all chain calls are mocked)
python3 test_fos_agent.py
```

Required env vars for live chain:
```bash
export BLOCKFROST_PROJECT_ID="preprod..."
export FOS_REGISTRY_SCRIPT_HASH="..."    # from identity_registry/plutus.json
export FOS_GOVERNANCE_SCRIPT_HASH="..."
export FOS_TREASURY_SCRIPT_HASH="..."
export FOS_AGENT_KEY_HASH="..."          # agent's VerificationKeyHash
export FOS_AGENT_SIGNING_KEY="..."       # 32-byte Ed25519 hex private key
export FOS_COLLATERAL_REF="..."          # "txhash#index" — UTxO with 5+ ADA for Plutus collateral
export FOS_MAX_AUTO_TRANSFER_LOVELACE="5000000"   # per-proposal safety cap
export FOS_AUTONOMOUS_MODE="false"       # set true to sign + submit without confirmation
```

CIP-171 on-chain contract verification:
```bash
# Publish cryptographic link between deployed script hashes and source commit
python3 scripts/verify.py --dry-run   # preview metadata without submitting
python3 scripts/verify.py             # publish to chain (requires DEPLOY_* env vars)
```

### Aiken Quorum contracts

```bash
cd identity_registry

# With real Aiken CLI installed:
aiken check          # fast type-check
aiken build          # compile → plutus.json
aiken test           # run all test blocks

# With the mock CLI (for development without real Aiken):
PATH="/path/to/files:$PATH" aiken check
PATH="/path/to/files:$PATH" aiken test
```

The `aiken` file at the repo root is a mock Python binary used by `test_agent.py`. It simulates `new/check/build/test` and is placed on `PATH` during test runs. Tests whose names contain "fail" are made to fail by the mock; all others pass.

---

## Dependencies

```bash
pip install anthropic                                                      # required
pip install chromadb sentence-transformers --break-system-packages        # RAG
pip install requests beautifulsoup4 --break-system-packages               # web ingest
pip install cbor2                                                          # Quorum datum parsing
pip install PyCardano                                                      # signing layer + address derivation
pip install flask                                                          # Quorum web dashboard
```

---

## Python Agent Architecture

- **`agent.py`** — Agent loop + all tool implementations. Calls `claude-sonnet-4-5` with 10 tools (file I/O, aiken CLI wrappers, project memory, RAG search). `run_agent()` is the main loop; `dispatch_tool()` routes tool calls; `_build_rag_system_prompt()` auto-retrieves relevant docs and prepends them to the base system prompt before the first Claude call.

- **`rag.py`** — `CardanoRAG` wrapping ChromaDB + sentence-transformers. Two chunking strategies: `chunk_markdown()` splits on headings + code fences; `chunk_aiken_file()` splits on top-level definitions. Ingestion is idempotent — chunks are keyed by SHA-256 of content.

- **`rag_ingest.py`** — Populates the ChromaDB vector store by scraping aiken-lang.org and loading curated inline validators (including the Quorum identity registry example).

- **`test_agent.py`** — Custom test runner, sections 1–9: file skills, aiken CLI skills, project memory, tool dispatch, tool schemas, simulated agent loop, RAG chunking, RAG ingest/query, agent–RAG integration. Tests override `agent.RAG_DB_PATH` and `agent.RAG_EMBEDDING_FN` directly on the module using `_HashEmbeddingFn` (deterministic bag-of-words, no model downloads).

**RAG is optional.** `agent.py` wraps the `rag` import in a try/except and degrades gracefully if ChromaDB is absent or the DB is empty. RAG DB defaults to `.rag_db/` next to `agent.py`, overridable via `CARDANO_RAG_DB`.

**Adding a new tool:** (1) add function to `agent.py`, (2) add JSON schema to `TOOLS`, (3) add dispatch branch in `dispatch_tool()`, (4) update `dispatchable` set in `test_tool_names_match_dispatch()`, (5) update expected count in `test_tool_count()`.

---

## Quorum Contract Architecture

### Three-layer stack

```
identity_registry/
  lib/
    fos_types.ak          ← ALL shared types live here
  validators/
    identity_registry.ak  ← Layer 1: membership + roles
    governance.ak         ← Layer 2: proposals + weighted voting
    treasury.ak           ← Layer 3: governed fund releases
```

### Reference input chain

Each layer reads the layer below it as a **reference input** — consumed for reading only, never spent — so state at lower layers is never disturbed by upper-layer transactions:

```
treasury  ──ref──▶  governance  ──ref──▶  identity registry
```

A `ExecuteTransfer` transaction includes both the governance UTxO and the identity registry as reference inputs simultaneously.

### `lib/fos_types.ak` — the integration contract

All cross-validator types live here. The most important:

- **`RegistryDatum`** — `members: List<RegistryMember>` + `admin` + `version`. The `version` field is monotonically incremented on every mutation. Governance and treasury pin this at proposal creation; a registry update invalidates open proposals.
- **`GovernanceDatum`** — includes `action: ProposalAction`, `status: ProposalStatus`, `registry_ref: OutputReference`, `registry_version: Int`.
- **`ProposalAction`** — the typed union that connects governance to treasury: `TreasuryTransfer { recipient, lovelace, memo }`, `RotateAdmin`, `UpdateRegistryMember`, `OffChainDecision`. The treasury validator pattern-matches on `TreasuryTransfer` to authorize a payment; all other action variants are rejected.

### Key invariants

**Every registry mutation increments `version`.** Governance proposals pin `registry_version` at creation. `load_registry()` in governance.ak asserts `registry.version == expected_version` — a registry update made after a proposal was created will cause that proposal's vote/execute transactions to fail.

**Treasury authenticates governance by script hash.** `TreasuryDatum.governance_script_hash` is checked against the `payment_credential` of the governance reference input via `ScriptCredential(hash)`. Without this, a fake "Executed" UTxO at a random address could drain the treasury.

**Continuing output required on every spend.** All three validators require exactly one output returning to the same script address with an unchanged (or correctly mutated) datum. State can never disappear from the chain.

**Observers cannot vote.** `vote_weight(Observer) == 0`. The `CastVote` branch asserts `vote_weight(reg_member.role) > 0` before appending a vote.

**Suspended members lose voting power retroactively.** `count_weighted_yes` checks `reg_member.status == Active` at execution time, not at vote-cast time. A member suspended after voting has their yes-vote discounted when quorum is tallied.

---

## Quorum Operator Agent Architecture

### Role vs. the builder agent

| | Builder (`agent.py`) | Operator (`fos_agent/`) |
|---|---|---|
| **Does** | Generates Aiken code | Submits Cardano transactions |
| **Reads** | Aiken docs via RAG | Chain state via Blockfrost |
| **Tools** | file I/O, aiken CLI | read_fos_state, cast_vote, execute_proposal, execute_treasury_transfer |
| **Output** | `.ak` source files | Signed + submitted transactions (autonomous) or `UnsignedTransaction` descriptors (confirmation mode) |

### Package layout

- **`fos_agent/config.py`** — All configuration from env vars. `is_configured()` checks whether all required script hashes and the Blockfrost key are set. Without them, `BlockfrostClient` runs in mock mode.
- **`fos_agent/types.py`** — Python mirrors of every type in `lib/fos_types.ak`, with `from_cbor_hex()` classmethods for deserializing Blockfrost inline datums. CBOR encoding: records → `Constr(0, fields)`, enum variant N → `Constr(N, [])`, `Bool True` → `Constr(1, [])`. Requires `cbor2`.
- **`fos_agent/chain.py`** — `BlockfrostClient` wraps the Blockfrost REST API and returns typed Python objects. `read_fos_state()` snapshots all three validators in one call and returns a `FOSState`. `FOSState` has derived properties: `executable_proposals`, `expirable_proposals`, `executed_proposals`, `unreachable_quorum_proposals` — computed from quorum, timelock, registry score, and current time. `unreachable_quorum_proposals` flags active proposals where the max possible yes score is already below the quorum threshold so the agent can expire them immediately.
- **`fos_agent/transactions.py`** — One builder per FOS action. Each returns an `UnsignedTransaction` (inputs, reference\_inputs, outputs, redeemers, validity range, required signers). The builders assert the three-layer security conditions before constructing — e.g. `build_execute_transfer_tx` raises if `status != Executed`, action is not `TreasuryTransfer`, or `lovelace > max_transfer_lovelace`.
- **`fos_agent/agent.py`** — `FOSAgent` dispatches tool calls to `chain.py` / `transactions.py`. `run_fos_agent(instruction)` is the one-shot entry point; `run_monitor(interval)` wraps it in a polling loop. Every decision is written to `.fos_audit.jsonl` via the `write_audit_log` tool. When `AUTONOMOUS_MODE=true`, `_sign_and_submit()` is called after each transaction builder — it fetches the agent's wallet UTxOs, loads compiled scripts from `plutus.json`, calls `signing.build_signed_transaction()`, and submits via Blockfrost.

### The agent loop flow for a treasury payment

```
read_fos_state
  → find active TreasuryTransfer proposals
  → check unreachable_quorum_proposals → expire immediately if found
  → check registry.version == proposal.registry_version  (staleness guard)
  → check recipient is Active member
  → cast_vote (yes/no) + write_audit_log
      ↳ AUTONOMOUS_MODE: sign → Blockfrost evaluate → sign final → submit

[later, after quorum + timelock]

execute_proposal       → flips status Voting → Executed
    ↳ AUTONOMOUS_MODE: sign → evaluate → sign final → submit
execute_treasury_transfer → builds UnsignedTransaction with:
    inputs:           [treasury UTxO]
    reference_inputs: [governance UTxO, registry UTxO]
    outputs:          [recipient payment, treasury continuing output]
    ↳ AUTONOMOUS_MODE: sign → evaluate → sign final → submit
```

### Six agent-level safety rules (system prompt enforced)

1. Vote yes on `TreasuryTransfer` only if `lovelace ≤ max_transfer_lovelace` and recipient is Active in registry.
2. Never vote or execute if `registry.version != proposal.registry_version` — flag it and require a new proposal.
3. Only call `execute_proposal` if `current_time >= execute_after` (timelock) and `yes_score >= quorum`.
4. Only call `execute_treasury_transfer` after `execute_proposal` is confirmed on-chain.
5. `write_audit_log` on every decision — approved, rejected, skipped, or anomaly.
6. With `FOS_AUTONOMOUS_MODE=false` (default), describe intent and wait for human confirmation before building any transaction.

### Signing layer (`fos_agent/signing.py` + `fos_agent/datums.py`)

**`fos_agent/datums.py`** — CBOR serialization (the inverse of `types.py`).  Converts Python datum objects back to on-chain CBOR hex using `cbor2`.  Used by `transactions.py` to build correct inline datums for continuing outputs:
- CastVote → appends `VoteRecord` to `GovernanceDatum`
- ExecuteProposal → flips status `Voting → Executed`
- ExpireProposal → flips status `Voting → Expired`
- Deploy → serializes initial `RegistryDatum` / `TreasuryDatum`

**`fos_agent/signing.py`** — PyCardano integration. Three capabilities:
1. **Address derivation**: `script_hash_to_address(hash, network)` and `pkh_to_enterprise_address(pkh, network)`. Falls back to mock strings if PyCardano is absent.
2. **Slot conversion**: `posix_ms_to_slot(posix_ms, blockfrost_url, project_id)` — converts POSIX milliseconds to Cardano slot numbers by anchoring to `Blockfrost /blocks/latest` (post-Shelley: 1 slot = 1 second). Falls back to a preprod constant if Blockfrost is unavailable.
3. **`build_signed_transaction(...)`** — two-phase pipeline:
   - Phase 1: build draft transaction with placeholder execution units, convert ms → slots
   - Phase 2: call `Blockfrost /utils/txs/evaluate` to get real Plutus memory/CPU units
   - Phase 3: rebuild with real units for accurate fee, sign with Ed25519, return CBOR hex

Both modules degrade gracefully — `datums.py` raises `AssertionError` if `cbor2` is missing; `signing.py` returns mock strings if `PyCardano` is absent.

### CIP-171 contract verification (`scripts/verify.py`)

Publishes an on-chain verification record (Cardano metadata label 1984) linking deployed script hashes to the exact git commit and Aiken version used to compile them. Anyone can independently verify by cloning the repo, checking out the pinned commit, running `aiken build`, and comparing the resulting hashes.

```bash
python3 scripts/verify.py --dry-run   # preview metadata without submitting
python3 scripts/verify.py             # publish (requires DEPLOY_* env vars + real plutus.json)
```

Guards against publishing mock hashes — exits with an error if `plutus.json` contains placeholder values from the test fixture.

### Preprod deployment (`scripts/deploy.py`)

Reads `identity_registry/plutus.json`, derives script addresses, and submits two deployment transactions:

1. Registry UTxO — `RegistryDatum` with deployer as founding Admin
2. Treasury UTxO — `TreasuryDatum` with `governance_script_hash` + ADA funding

```bash
export BLOCKFROST_PROJECT_ID="preprod..."
export DEPLOY_SIGNING_KEY="<32-byte Ed25519 hex>"
export DEPLOY_KEY_HASH="<vkey hash hex>"
export DEPLOY_COLLATERAL_REF="<txhash#index>"

cd identity_registry && aiken build   # produces plutus.json
python3 scripts/deploy.py             # deploys + prints env vars
```

### FOS Web UI (`fos_ui/`)

Flask dashboard that reads live chain state and lets a human operator build unsigned transactions.

```bash
python3 fos_ui/app.py     # → http://localhost:5000
PORT=8080 python3 fos_ui/app.py
```

Routes:
- `GET  /`             — dashboard HTML
- `GET  /api/state`    — full Quorum state JSON
- `POST /api/vote`     — `{proposal_ref, approve}` → `UnsignedTransaction.summary()`
- `POST /api/execute`  — `{proposal_ref}` → execute-proposal tx
- `POST /api/expire`   — `{proposal_ref}` → expire-proposal tx
- `POST /api/transfer` — `{governance_ref}` → execute-transfer tx
- `GET  /api/audit`    — last 50 audit log entries

The UI builds the transaction descriptor and shows the summary.  The operator copies it to their wallet (Eternl, Nami, or `cardano-cli`) for final signing and submission — the web server never holds a private key.

### Adding a new Quorum validator

1. Add any new shared types to `lib/fos_types.ak`.
2. Create `validators/new_layer.ak`, importing from `fos_types`.
3. If it reads another layer's state, use a reference input pattern (see `load_governance()` in `treasury.ak` or `load_registry()` in `governance.ak`).
4. Add the new `ProposalAction` variant to `fos_types.ak` if governance needs to trigger it.
5. Add the inline example to `INLINE_EXAMPLES` in `rag_ingest.py` so the agent can generate similar contracts.
