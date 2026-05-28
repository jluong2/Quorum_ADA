# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Three things in one repo:

1. **Builder agent** (`agent.py`, `rag.py`, `rag_ingest.py`) — A Claude-powered coding agent that generates Aiken smart contracts from natural language, with an optional RAG layer for doc retrieval.

2. **Quorum** (`identity_registry/`) — A four-layer on-chain governance protocol built in Aiken: identity registry → governance → treasury → vesting.

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
export FOS_VESTING_SCRIPT_HASH="..."     # optional — enables vesting UTxO reads
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."  # optional alerts
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."    # optional alerts
export FOS_TREASURY_ALERT_ADA="10"      # alert when treasury drops below this (ADA)
```

CIP-95 DRep registration:
```bash
# Optional DRep env vars (for register_drep.py)
export DREP_ANCHOR_URL="https://…/drep_metadata.json"   # after uploading scripts/drep_metadata.json
export DREP_ANCHOR_HASH="<blake2b-256 of metadata JSON>"

python3 scripts/register_drep.py --dry-run   # preview
python3 scripts/register_drep.py             # register
python3 scripts/register_drep.py --retire    # retire
python3 scripts/register_drep.py --print-metadata  # emit CIP-119 JSON
```

CIP-171 on-chain contract verification:
```bash
# Publish cryptographic link between deployed script hashes and source commit
python3 scripts/verify.py --dry-run   # preview metadata without submitting
python3 scripts/verify.py             # publish to chain (requires DEPLOY_* env vars)
```

### Aiken Quorum contracts

Contracts target **Plutus V3** using Aiken 1.1.22 and stdlib v3.1.0. The compiled output is `identity_registry/plutus.json`.

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

**Stdlib v3 import paths** (differs from older Aiken tutorials):

| V2 (stdlib v2) | V3 (stdlib v3, current) |
|---|---|
| `use aiken/list` | `use aiken/collection/list` |
| `use aiken/transaction.{...}` | `use cardano/transaction.{...}` |
| `use aiken/transaction/credential.{ScriptCredential, VerificationKeyCredential}` | `use cardano/address.{Script, VerificationKey}` |
| `use aiken/transaction/value` | `use cardano/assets` |
| `value.zero()` | `assets.zero` (constant, no parens) |
| `value.lovelace_of(v)` | `assets.lovelace_of(v)` |

**Plutus V3 validator syntax** (all four validators follow this pattern):

```aiken
validator my_validator_name {
  spend(
    datum: Option<MyDatum>,
    redeemer: MyRedeemer,
    own_ref: OutputReference,
    self: Transaction,
  ) -> Bool {
    expect Some(datum) = datum
    let Transaction { inputs, outputs, extra_signatories, .. } = self
    // own_ref replaces ctx.purpose / find_input(inputs, own_ref)
    // self replaces ctx.transaction
    ...
  }

  else(_) { fail }
}
```

Key differences from V2: `ScriptContext` is gone; `own_ref: OutputReference` and `self: Transaction` are direct parameters; datum is wrapped in `Option<>`; every named validator needs an `else(_) { fail }` catch-all.

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

### Four-layer stack

```
identity_registry/
  lib/
    fos_types.ak          ← ALL shared types live here
  validators/
    identity_registry.ak  ← Layer 1: membership + roles
    governance.ak         ← Layer 2: proposals + weighted voting
    treasury.ak           ← Layer 3: governed fund releases + vesting funding
    vesting.ak            ← Layer 4: time-locked vesting (ClaimVested / CancelVesting)
```

### Reference input chain

Each layer reads the layer below it as a **reference input** — consumed for reading only, never spent — so state at lower layers is never disturbed by upper-layer transactions:

```
treasury  ──ref──▶  governance  ──ref──▶  identity registry
```

A `ExecuteTransfer` transaction includes both the governance UTxO and the identity registry as reference inputs simultaneously.

### `lib/fos_types.ak` — the integration contract

All cross-validator types live here. The most important:

- **`RegistryMember`** — `key_hash`, `role`, `joined_at`, `status`, and now `delegate: Option<VerificationKeyHash>`. The `delegate` field implements liquid democracy: if set, this member's voting weight flows to the target when the target votes (and this member has not voted directly). Single-hop only — the target's `delegate` must be `None`.
- **`RegistryDatum`** — `members: List<RegistryMember>` + `admin` + `version` + `governance_script_hash`. The `version` field is monotonically incremented on every mutation (including `SetDelegate`). Governance and treasury pin this at proposal creation; a registry update invalidates open proposals. `governance_script_hash` is the hash of the governance validator — immutable after deployment, used by the `GovernanceApproval` redeemer to authenticate governance reference inputs.
- **`GovernanceDatum`** — includes `action: ProposalAction`, `status: ProposalStatus`, `registry_ref: OutputReference`, `registry_version: Int`, and `deposit: Int`. The `deposit` field records the lovelace locked by the proposer at creation time. On Execute (quorum met) the deposit is refunded to the proposer in the same transaction. On Expire (deadline missed without quorum) the deposit stays locked in the governance UTxO — forfeited as a spam deterrent. Minimum deposit is `2_000_000` lovelace (2 ADA), enforced both on-chain (`min_deposit` constant in `governance.ak`) and off-chain (`MIN_PROPOSAL_DEPOSIT` in `transactions.py`).
- **`NativeToken`** — `{ policy_id: ByteArray, asset_name: ByteArray, quantity: Int }`. Used in `TreasuryTransfer.tokens` to transfer Cardano native assets alongside ADA. The treasury validator enforces `tokens_to_recipient` (recipient gets all approved tokens) and `treasury_conserves_tokens` (continuing output holds at least the remainder). Python-side: `NativeToken.asset_name_str()` tries UTF-8 decode, falls back to hex.
- **`ProposalAction`** — the typed union that connects governance to other validators: `TreasuryTransfer { recipient, lovelace, memo, tokens: List<NativeToken> }` (treasury pays out ADA and/or native tokens), `RotateAdmin` (registry rotates admin key), `UpdateRegistryMember` (registry updates a member), `OffChainDecision` (no on-chain execution), `CreateVesting { recipient, tranches: List<VestingTranche>, memo }` (treasury funds a new vesting UTxO at the vesting script). Treasury and registry validators each pattern-match on only their relevant action variants. The `tokens` field was added at the end of `TreasuryTransfer` for backward compatibility (old datums with 3 fields still parse — `tokens` defaults to `[]`).
- **`VestingTranche`** — `{ release_time: Int, lovelace: Int }`. One time-locked slice of a vesting schedule. `release_time` is POSIX milliseconds — the earliest moment the tranche can be claimed.
- **`VestingDatum`** — `{ recipient: VerificationKeyHash, tranches: List<VestingTranche>, proposal_ref: OutputReference }`. Inline datum on every vesting UTxO. `proposal_ref` is an immutable audit link back to the governance proposal that created the schedule. Matured tranches are removed on each `ClaimVested` spend; the list shrinks until the UTxO is fully drained.

### Key invariants

**Every registry mutation increments `version`.** Governance proposals pin `registry_version` at creation. `load_registry()` in governance.ak asserts `registry.version == expected_version` — a registry update made after a proposal was created will cause that proposal's vote/execute transactions to fail.

**Treasury authenticates governance by script hash.** `TreasuryDatum.governance_script_hash` is checked against the `payment_credential` of the governance reference input via `Script(hash)` (Plutus V3 credential constructor). Without this, a fake "Executed" UTxO at a random address could drain the treasury.

**Registry authenticates governance by script hash.** `RegistryDatum.governance_script_hash` is checked identically in the `GovernanceApproval` redeemer path. A fake "Executed" UTxO cannot mutate the membership list or rotate the admin key.

**GovernanceApproval is replay-proof.** The registry validator asserts `governance_proposal.registry_version == current_datum.version`. After a mutation increments the version, the same governance UTxO will fail this check on any re-use attempt.

**`governance_script_hash` is immutable in `RegistryDatum`.** All registry mutation paths assert `new_datum.governance_script_hash == datum.governance_script_hash`. Once set at deployment, the governance link cannot be rewired without redeploying.

**High-stakes actions require a 2/3 supermajority.** `RotateAdmin` and `TreasuryTransfer > 10 ADA` must satisfy `yes_score * 3 >= max_possible_score * 2` in addition to the proposal's `quorum` field. Enforced by `action_requires_supermajority()` / `supermajority_met()` in `governance.ak` and mirrored by the same functions in `fos_agent/types.py`.

**Treasury conserves ADA.** The continuing treasury output must hold at least `treasury_in_lovelace - lovelace` after a transfer. Without this check, the tx submitter could drain the remaining balance to their change address.

**Treasury conserves native tokens.** `treasury_conserves_tokens(own_value, continuing_value, tokens)` in `treasury.ak` asserts for each approved token that `held_after >= held_before - quantity`. Without this check, the submitter could drain remaining tokens to their change address alongside the ADA.

**Governance datum fields are fully locked in Execute and Expire.** Both status-transition paths assert all ten immutable fields (`proposer`, `description`, `action`, `votes`, `quorum`, `execute_after`, `vote_deadline`, `registry_ref`, `registry_version`, `deposit`) are unchanged on the continuing output. Only `status` may change.

**Proposal deposit is preserved on CastVote, refunded on Execute, locked on Expire.** `CastVote` asserts `cont_lovelace >= in_lovelace` (no ADA drain during voting). `Execute` asserts `lovelace_to_pkh(outputs, datum.proposer) >= datum.deposit` and `cont_lovelace >= in_lovelace - datum.deposit` (deposit flows to proposer, continuing output holds the remainder). `Expire` asserts `cont_lovelace >= in_lovelace` (full lovelace including deposit stays locked forever).

**Continuing output required on every spend.** All three validators require exactly one output returning to the same script address with an unchanged (or correctly mutated) datum. State can never disappear from the chain.

**Observers cannot vote.** `vote_weight(Observer) == 0`. The `CastVote` branch asserts `vote_weight(reg_member.role) > 0` before appending a vote.

**Suspended members lose voting power retroactively.** `count_weighted_yes` checks `reg_member.status == Active` at execution time, not at vote-cast time. A member suspended after voting has their yes-vote discounted when quorum is tallied.

**Delegation is single-hop and self-service.** `SetDelegate` (constructor 5 in `RegistryAction`) lets a member update their own `delegate` field without admin or governance approval — they sign the tx themselves. The on-chain validator enforces: no self-delegation, target must be active, target's `delegate` must be `None` (no chains). Increments version like all mutations, invalidating open proposals.

**Vesting schedules are irrevocable.** `CancelVesting` always returns `False` in `vesting.ak`. Once the treasury funds a vesting UTxO, only the designated recipient can spend it.

**Vesting claims are time-gated by validity interval.** `ClaimVested` reads `validity_range.lower_bound` (must be `Finite`) and only allows tranches where `release_time <= lower_bound`. The submitter commits to a point in time — they cannot back-date the claim.

**Vesting datum integrity on partial claims.** The continuing vesting output must carry the unchanged `recipient` and `proposal_ref`, exactly the unmatured tranches, and lovelace ≥ sum of those tranches. Matured tranches are permanently removed.

**Vesting is treasury-funded, governance-authenticated.** The treasury `CreateVestingSchedule` redeemer verifies the governance reference input by script hash and confirms `status == Executed`, preventing a fake governance UTxO from misdirecting treasury funds to an arbitrary vesting address.

**Delegated weight is carried by the delegate, not the delegator.** `effective_vote_weight(members, direct_voters, voter_key)` in `governance.ak` adds the weights of all active members who (a) delegated to `voter_key`, and (b) have not cast a direct vote of their own. A delegator who votes directly overrides the delegation — their weight stays with their own ballot.

---

## Quorum Operator Agent Architecture

### Role vs. the builder agent

| | Builder (`agent.py`) | Operator (`fos_agent/`) |
|---|---|---|
| **Does** | Generates Aiken code | Submits Cardano transactions |
| **Reads** | Aiken docs via RAG | Chain state via Blockfrost |
| **Tools** | file I/O, aiken CLI | read_fos_state, cast_vote, execute_proposal, expire_proposal, execute_treasury_transfer, execute_registry_action, write_audit_log |
| **Output** | `.ak` source files | Signed + submitted transactions (autonomous) or `UnsignedTransaction` descriptors (confirmation mode) |

### Package layout

- **`fos_agent/config.py`** — All configuration from env vars. `is_configured()` checks whether all required script hashes and the Blockfrost key are set. Without them, `BlockfrostClient` runs in mock mode.
- **`fos_agent/types.py`** — Python mirrors of every type in `lib/fos_types.ak`, with `from_cbor_hex()` classmethods for deserializing Blockfrost inline datums. CBOR encoding: records → `Constr(0, fields)`, enum variant N → `Constr(N, [])`, `Bool True` → `Constr(1, [])`. Requires `cbor2`.
- **`fos_agent/chain.py`** — `BlockfrostClient` wraps the Blockfrost REST API and returns typed Python objects. `read_fos_state()` snapshots all validators in one call and returns a `FOSState`. Accepts optional `vesting_script_hash` — when set, reads and parses all vesting UTxOs at that address. `FOSState` has derived properties: `executable_proposals`, `expirable_proposals`, `executed_proposals`, `unreachable_quorum_proposals`, `executed_awaiting_registry`, `claimable_vesting` — computed from quorum, timelock, registry score, action type, and current time. `claimable_vesting` filters `vesting_utxos` to those with at least one matured tranche (`release_time <= current_time_ms`). `unreachable_quorum_proposals` flags active proposals where the max possible yes score is already below the quorum threshold so the agent can expire them immediately. `executed_awaiting_registry` flags Executed proposals with `RotateAdmin` or `UpdateRegistryMember` actions that still need `execute_registry_action` called.
- **`fos_agent/transactions.py`** — One builder per FOS action. Each returns an `UnsignedTransaction` (inputs, reference\_inputs, outputs, redeemers, validity range, required signers). The builders assert security conditions before constructing — e.g. `build_execute_transfer_tx` raises if `status != Executed`, action is not `TreasuryTransfer`, or `lovelace > max_transfer_lovelace`. `build_execute_transfer_tx` now passes `action.tokens` to the recipient `TxOutput` and notes that the signing layer is responsible for returning the token remainder to the treasury. `build_execute_registry_action_tx` uses redeemer constructor 4 (`GovernanceApproval`) and applies the mutation in Python before serialising the new datum. `build_set_delegate_tx` uses redeemer constructor 5 (`SetDelegate`) and enforces no-self, no-chain, and active-target checks before building. `build_create_proposal_tx` accepts a `deposit` parameter (default `2_000_000`, minimum enforced by `MIN_PROPOSAL_DEPOSIT`) and an optional `rationale_url: str = ""` — when set, the URL is stored as tx metadata label 675 (`{"rationale": url}`); the proposal output holds `min_lovelace + deposit`. `build_execute_proposal_tx` adds a second output refunding `deposit` lovelace to the proposer's address when `deposit > 0`; the continuing governance output holds `in_lovelace - deposit`. `build_create_vesting_tx` spends the treasury UTxO for an Executed `CreateVesting` proposal, creates a vesting UTxO at `vesting_script_hash` with a `VestingDatum`, and returns the remainder to the treasury (`CreateVestingSchedule` redeemer, constructor 1). `build_claim_vesting_tx` spends a vesting UTxO, pays matured tranches (those with `release_time <= current_time_ms`) to the recipient, and if unmatured tranches remain creates a continuing vesting output with the updated `VestingDatum` (`ClaimVested` redeemer, constructor 0).
- **`fos_agent/agent.py`** — `FOSAgent` dispatches tool calls to `chain.py` / `transactions.py`. `run_fos_agent(instruction)` is the one-shot entry point; `run_monitor(interval)` wraps it in a polling loop, calling `AlertManager.check()` before each agent turn. Every decision is written to `.fos_audit.jsonl` via the `write_audit_log` tool. When `AUTONOMOUS_MODE=true`, `_sign_and_submit()` is called after each transaction builder — it fetches the agent's wallet UTxOs, loads compiled scripts from `plutus.json`, calls `signing.build_signed_transaction()`, and submits via Blockfrost.
- **`fos_agent/alerts.py`** — `AlertManager` fires Discord/Slack webhook notifications for: new proposals, quorum reached, deadline within 24 h, high-value transfer proposals, treasury below threshold, and **at-risk proposals** (< 48 h left, quorum unmet, yes votes below 50% of maximum possible). Deduplicates via an in-memory `_fired` set so each alert fires at most once per process lifetime. Configure: `DISCORD_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `FOS_TREASURY_ALERT_ADA`.
- **`fos_agent/proposal_agent.py`** — `run_proposal_agent(instruction)` drafts and submits governance proposals from natural language. Tools: `read_fos_state`, `draft_proposal` (validates feasibility, enforces supermajority quorum for high-stakes actions, returns preview), `submit_proposal` (only called after user confirms). Signs and submits autonomously when `AUTONOMOUS_MODE=true`.

### The agent loop — treasury payment

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

### The agent loop — registry mutation (RotateAdmin / UpdateRegistryMember)

```
read_fos_state
  → governance.executed_awaiting_registry lists proposal refs
  → cast_vote + write_audit_log (same rules as treasury proposals)

[later, after quorum + timelock]

execute_proposal       → flips status Voting → Executed
    ↳ AUTONOMOUS_MODE: sign → evaluate → sign final → submit
execute_registry_action → builds UnsignedTransaction with:
    inputs:           [registry UTxO]
    reference_inputs: [governance UTxO]           — proves Executed status
    outputs:          [registry UTxO with mutation applied, version + 1]
    redeemer:         GovernanceApproval (constructor 4) — no admin key needed
    ↳ AUTONOMOUS_MODE: sign → evaluate → sign final → submit
```

### Eight agent-level safety rules (system prompt enforced)

1. Vote yes on `TreasuryTransfer` only if `lovelace ≤ max_transfer_lovelace` and recipient is Active in registry.
2. Never vote or execute if `registry.version != proposal.registry_version` — flag it and require a new proposal.
3. Only call `execute_proposal` if `current_time >= execute_after` (timelock) and `yes_score >= quorum`.
4. For `RotateAdmin` or `TreasuryTransfer > 10 ADA`, additionally verify `yes_score * 3 >= max_possible_score * 2` before calling `execute_proposal` (on-chain supermajority check).
5. Only call `execute_treasury_transfer` after `execute_proposal` is confirmed on-chain.
6. Only call `execute_registry_action` after `execute_proposal` is confirmed on-chain; action must be `RotateAdmin` or `UpdateRegistryMember`.
7. `write_audit_log` on every decision — approved, rejected, skipped, or anomaly.
8. With `FOS_AUTONOMOUS_MODE=false` (default), describe intent and wait for human confirmation before building any transaction.

### Signing layer (`fos_agent/signing.py` + `fos_agent/datums.py`)

**`fos_agent/datums.py`** — CBOR serialization (the inverse of `types.py`).  Converts Python datum objects back to on-chain CBOR hex using `cbor2`.  Used by `transactions.py` to build correct inline datums for continuing outputs:
- CastVote → appends `VoteRecord` to `GovernanceDatum`
- ExecuteProposal → flips status `Voting → Executed`
- ExpireProposal → flips status `Voting → Expired`
- GovernanceApproval → applies `RotateAdmin` / `UpdateRegistryMember` mutation to `RegistryDatum` + increments version
- CreateVestingSchedule → serializes `VestingDatum` for the new vesting UTxO (`vesting_datum_cbor_hex`)
- ClaimVested (partial) → serializes updated `VestingDatum` with matured tranches removed
- Deploy → serializes initial `RegistryDatum` (including `governance_script_hash`) / `TreasuryDatum`

**`fos_agent/signing.py`** — PyCardano integration. Three capabilities:
1. **Address derivation**: `script_hash_to_address(hash, network)` and `pkh_to_enterprise_address(pkh, network)`. Falls back to mock strings if PyCardano is absent.
2. **Slot conversion**: `posix_ms_to_slot(posix_ms, blockfrost_url, project_id)` — converts POSIX milliseconds to Cardano slot numbers by anchoring to `Blockfrost /blocks/latest` (post-Shelley: 1 slot = 1 second). Falls back to a preprod constant if Blockfrost is unavailable.
3. **`build_signed_transaction(...)`** — two-phase pipeline:
   - Phase 1: build draft transaction with placeholder execution units, convert ms → slots
   - Phase 2: call `Blockfrost /utils/txs/evaluate` to get real Plutus memory/CPU units
   - Phase 3: rebuild with real units for accurate fee, sign with Ed25519, return CBOR hex

Both modules degrade gracefully — `datums.py` raises `AssertionError` if `cbor2` is missing; `signing.py` returns mock strings if `PyCardano` is absent.

### CIP-95 DRep integration (`fos_agent/drep.py` + `scripts/register_drep.py`)

**`fos_agent/drep.py`** — Cardano Delegated Representative (CIP-95) support:
- `drep_id_from_key_hash(key_hash)` — derives bech32 `drep1…` ID from the agent's vkey hash (uses PyCardano bech32; falls back to readable hex stub)
- `query_drep_status(key_hash)` — returns `DRepInfo` (registered, voting power, delegator count, anchor URL) via Blockfrost `/governance/dreps/{hash}`; mock mode returns plausible data when `BLOCKFROST_PROJECT_ID` is absent
- `build_drep_registration(key_hash, anchor_url, anchor_hash)` — returns a `DRepRegistrationTx` descriptor; deposit is `2_000_000` on preprod, `500_000_000` on mainnet
- `build_drep_retirement(key_hash)` — returns a retirement descriptor
- `generate_drep_metadata(...)` — builds a CIP-119 compliant metadata dict; upload the JSON to IPFS, then pass URL + blake2b-256 hash to `build_drep_registration`

**`scripts/register_drep.py`** — CLI for DRep lifecycle:
```bash
python3 scripts/register_drep.py --dry-run        # preview without submitting
python3 scripts/register_drep.py                   # register on preprod
python3 scripts/register_drep.py --retire          # retire DRep
python3 scripts/register_drep.py --print-metadata  # print CIP-119 JSON
```
Requires `DREP_ANCHOR_URL` and `DREP_ANCHOR_HASH` env vars (anchor must be uploaded first). The `GET /api/drep` endpoint in `fos_ui/app.py` exposes live DRep status to the dashboard.

**`scripts/drep_metadata.json`** — CIP-119 metadata template with the agent's motivation, objectives, and qualifications. Upload to IPFS and pass the resulting CID as `DREP_ANCHOR_URL`.

### CIP-171 contract verification (`scripts/verify.py`)

Publishes an on-chain verification record (Cardano metadata label 1984) linking deployed script hashes to the exact git commit and Aiken version used to compile them. Anyone can independently verify by cloning the repo, checking out the pinned commit, running `aiken build`, and comparing the resulting hashes.

```bash
python3 scripts/verify.py --dry-run   # preview metadata without submitting
python3 scripts/verify.py             # publish (requires DEPLOY_* env vars + real plutus.json)
```

Guards against publishing mock hashes — exits with an error if `plutus.json` contains placeholder values from the test fixture.

### Preprod deployment (`scripts/deploy.py`)

Reads `identity_registry/plutus.json`, derives script addresses, and submits two deployment transactions:

1. Registry UTxO — `RegistryDatum` with deployer as founding Admin and `governance_script_hash` pre-loaded (both hashes are deterministic from compiled code and can be computed before either contract is deployed)
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
python3 fos_ui/app.py     # → http://127.0.0.1:5000
PORT=8080 python3 fos_ui/app.py
HOST=0.0.0.0 python3 fos_ui/app.py   # expose to LAN
```

Security defaults: binds to `127.0.0.1` (loopback), debug off, CSRF Origin check on all mutating routes. Set `HOST=0.0.0.0` to expose to LAN; set `FLASK_DEBUG=1` for development.

Routes:
- `GET  /`               — dashboard HTML (Active / History tab bar; member delegate buttons; vesting panel)
- `GET  /api/state`      — full Quorum state JSON (includes `delegate` / `delegate_short` per member; proposals include `rationale_url`, `transfer_tokens`, `deposit_ada`; `vesting` key with schedules + claimable count)
- `POST /api/propose`    — `{action_type, description, rationale_url?, tokens?[], tranches?[], deposit_ada, …}` → create-proposal tx; `action_type=CreateVesting` accepts `tranches: [{release_time_ms, ada}]`
- `POST /api/vote`       — `{proposal_ref, approve}` → `UnsignedTransaction.summary()`
- `POST /api/execute`    — `{proposal_ref}` → execute-proposal tx
- `POST /api/expire`     — `{proposal_ref}` → expire-proposal tx
- `POST /api/transfer`   — `{governance_ref}` → execute-transfer tx (ADA + native tokens)
- `POST /api/delegate`   — `{member_key_hash, new_delegate}` → set-delegate tx (liquid democracy)
- `POST /api/vesting/fund` — `{governance_ref, vesting_script_hash}` → create-vesting-schedule tx (funds from treasury)
- `POST /api/vesting/claim` — `{vesting_ref, recipient_address}` → claim-vested tx (matured tranches)
- `GET  /api/drep`       — agent DRep registration status (registered, voting power, delegator count)
- `GET  /api/audit`      — last 50 audit log entries
- `POST /api/executor/run` — `{proposal_ref}` → run executor agent for an Executed proposal

The UI builds the transaction descriptor and shows the summary.  The operator copies it to their wallet (Eternl, Nami, or `cardano-cli`) for final signing and submission — the web server never holds a private key.

### Executor agent (`fos_agent/executor.py`)

When a proposal reaches `Executed` status, `run_executor()` launches a second Claude agent that interprets the mandate and carries it out (post a Discord message, compile a contract, create a GitHub issue, etc.).

**Security hardening:**
- **Prompt injection guard**: `description` and `memo` fields from on-chain data are wrapped in `<user-submitted-content>` XML tags in the instruction and flagged as untrusted. The system prompt explicitly instructs Claude to refuse override attempts and log them as anomalies.
- **Path traversal**: `_write_file`, `_read_file`, and `_list_directory` call `_resolve_safe()` which checks `target.relative_to(repo_root)` before acting. Any `../` escape returns `{"success": false, "error": "Path traversal blocked"}`.
- **SSRF**: `_http_request` validates the URL via `_is_safe_url()` — requires `https` scheme and blocks all private/loopback IP ranges (`127.*`, `10.*`, `192.168.*`, `169.254.*`, `localhost`, etc.).

### Adding a new Quorum validator

1. Add any new shared types to `lib/fos_types.ak`.
2. Create `validators/new_layer.ak` using Plutus V3 syntax (`validator name { spend(datum: Option<T>, redeemer, own_ref: OutputReference, self: Transaction) -> Bool { ... } else(_) { fail } }`). Import from `cardano/transaction`, `cardano/address`, `cardano/assets`, `aiken/collection/list`.
3. If it reads another layer's state, use a reference input pattern — pass `self.reference_inputs` directly to a helper function (see `load_governance()` in `treasury.ak` or `load_registry()` in `governance.ak`).
4. Add the new `ProposalAction` variant to `fos_types.ak` if governance needs to trigger it. Update `parse_proposal_action()` in `fos_agent/types.py` and `_encode_action()` in `fos_agent/datums.py`.
5. If the treasury funds the new validator, add a new `TreasuryRedeemer` variant to `treasury.ak` (see `CreateVestingSchedule` as the pattern).
6. Add Python mirrors to `fos_agent/types.py`, CBOR serializers to `fos_agent/datums.py`, and transaction builders to `fos_agent/transactions.py`.
7. Expose state via `FOSState` in `fos_agent/chain.py` and add Flask routes in `fos_ui/app.py`.
8. Add the inline example to `INLINE_EXAMPLES` in `rag_ingest.py` so the agent can generate similar contracts.
