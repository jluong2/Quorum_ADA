# Quorum — Cardano On-Chain Governance
### What Was Built · How It Works · How ADA Tokens Are Used

---

## 1. Overview

**Quorum** is a fully on-chain governance and treasury management system built on the Cardano blockchain. It gives an organisation — a DAO, a protocol team, a multi-sig fund — a structured way to:

- Maintain a verified list of members and their roles
- Propose and vote on decisions using weighted governance
- Automatically release ADA from a treasury only when a proposal passes
- Watch all of this happen in a live web dashboard without ever exposing a private key to the server

The project is composed of four layers that work together:

| Layer | Technology | Purpose |
|---|---|---|
| Smart Contracts | Aiken (Cardano) | Enforce all rules on-chain |
| Operator Agent | Python + Claude AI | Autonomous monitor and voter |
| Transaction Builder | Python (PyCardano) | Assembles and signs transactions |
| Web Dashboard | Flask + vanilla JS | Human operator interface |

---

## 2. What Was Built

### 2.1 Smart Contracts — `identity_registry/`

Three Aiken validators are deployed as separate script addresses on Cardano. Each validator holds a single UTxO (an unspent transaction output) containing its current state as an inline datum.

#### Layer 1 — Identity Registry (`identity_registry.ak`)

The source of truth for who belongs to the organisation. Every member is recorded with:

- Their **verification key hash** (their on-chain identity)
- A **role**: Admin (vote weight 3), Treasurer (weight 2), Member (weight 1), or Observer (weight 0)
- A **status**: Active, Suspended, or Removed
- A `joined_at` timestamp

Every member now also stores an optional **delegate** key hash — the liquid democracy field. When set, the member's voting weight flows to their delegate at execution time (unless the member votes directly, which overrides delegation). Delegation is single-hop: the target's `delegate` must be unset. Members update their own delegate via the self-service `SetDelegate` redeemer — no admin key or governance vote required.

The registry stores a monotonically increasing **version number**. Every time a member is added, suspended, removed, or changes their delegate, the version increments. This version is the key to keeping governance safe — see Section 4.

The registry also stores the **governance script hash** — the compiled hash of the `governance.ak` validator. This field is immutable after deployment and is used by the `GovernanceApproval` redeemer to authenticate governance reference inputs without requiring the admin key.

#### Layer 2 — Governance (`governance.ak`)

Handles proposals and voting. Each proposal lives in its own UTxO and records:

- A human-readable description
- A **typed action** — exactly what will happen if the proposal passes
- A list of votes cast so far (voter key hash + approve/reject)
- A vote deadline (POSIX milliseconds)
- An execute-after timestamp (the timelock)
- A quorum threshold (weighted yes-score required to pass)
- The registry version at creation time

**Actions that can be proposed:**

| Action | Execution path |
|---|---|
| `TreasuryTransfer` | Release a specific ADA amount to a specific recipient (treasury validator) |
| `RotateAdmin` | Replace the registry admin key — applied via `GovernanceApproval` on the registry validator, no admin key required |
| `UpdateRegistryMember` | Change a member's role or status — applied via `GovernanceApproval` on the registry validator, no admin key required |
| `OffChainDecision` | Record a governance decision that has no on-chain execution (e.g. elect an officer) |

#### Layer 3 — Treasury (`treasury.ak`)

Holds the organisation's ADA. The treasury UTxO can only be spent when:

1. A governance proposal with a `TreasuryTransfer` action has status `Executed`
2. The governance UTxO is included as a **reference input** (read-only, not consumed)
3. The recipient and amount match exactly what the proposal specifies
4. The transfer amount does not exceed the per-proposal cap in `TreasuryDatum`
5. The governance script hash in `TreasuryDatum` matches the actual governance script (preventing a fake "Executed" UTxO from draining funds)
6. The continuing treasury output holds at least `treasury_in - approved_transfer` lovelace (preventing the submitter from draining remaining ADA to their change address)

### 2.2 Operator Agent — `fos_agent/`

A Claude AI-powered Python agent that watches the chain and acts on pending governance items. It runs in one of two modes:

- **One-shot**: called once, reads state, acts, exits
- **Continuous monitor**: polls every N seconds in a loop

The agent has eight enforced safety rules baked into its system prompt:

1. Only vote Yes on `TreasuryTransfer` if the amount is within the configured cap and the recipient is an Active member
2. Never vote or execute if `registry.version != proposal.registry_version`
3. Only call Execute if the timelock has cleared and quorum is met
4. For `RotateAdmin` or `TreasuryTransfer > 10 ADA`, additionally verify a 2/3 on-chain supermajority (`yes_score * 3 >= max_possible_score * 2`) before calling Execute
5. Only trigger a treasury transfer after the Execute transaction is confirmed on-chain
6. Only call `execute_registry_action` after the Execute transaction is confirmed; action must be `RotateAdmin` or `UpdateRegistryMember`
7. Write every decision (approved, rejected, skipped, anomaly) to an audit log
8. In non-autonomous mode, describe intent and wait for human confirmation

**AlertManager** (`fos_agent/alerts.py`) runs at the start of every monitor cycle and fires Discord/Slack webhook notifications for:
- New proposals detected
- Quorum threshold reached on a proposal
- Proposal deadline within 24 hours
- High-value transfer proposals (above the configured threshold)
- Treasury balance below the alert threshold
- **At-risk proposals**: fewer than 48 hours remaining, quorum not met, and current yes-score below 50% of the maximum achievable — fires once per proposal per process lifetime

Configure webhooks via `DISCORD_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, and `FOS_TREASURY_ALERT_ADA`.

**Proposal creation agent** (`fos_agent/proposal_agent.py`) drafts and submits governance proposals from natural language. It reads current on-chain state, validates feasibility (recipient exists, amount within cap, registry state), recommends quorum thresholds (enforcing supermajority for high-stakes actions), shows a preview for human review, and submits on confirmation:

```bash
python3 -c "
from fos_agent.proposal_agent import run_proposal_agent
run_proposal_agent('Propose a 5 ADA grant to member abc123 for Q2 dev work')
"
```

**Delegation-aware vote counting**: when tallying `yes_score`, the agent (and the on-chain `count_weighted_yes` function) applies liquid democracy. For each yes-voter, the tally adds their own vote weight plus the weight of any Active members who have delegated to them and did not vote directly. Delegators who cast a vote directly always override their delegation — their weight is counted for whichever side they voted on, not added to their delegate.

Every action the agent considers produces an `UnsignedTransaction` descriptor — a structured specification of inputs, outputs, redeemers, and validity range — that is passed to the signing layer separately. The agent never holds a private key.

### 2.3 Transaction Builder — `fos_agent/transactions.py`, `signing.py`, `datums.py`

Three modules handle the full transaction lifecycle:

- **`transactions.py`** — one builder function per Quorum action. Each validates preconditions and returns an `UnsignedTransaction`:

  | Builder | Action |
  |---|---|
  | `build_create_proposal_tx` | Create a new governance proposal UTxO |
  | `build_cast_vote_tx` | Append a yes/no vote to a proposal |
  | `build_execute_proposal_tx` | Flip status `Voting → Executed` once quorum + timelock clear |
  | `build_expire_proposal_tx` | Flip status `Voting → Expired` after deadline without quorum |
  | `build_execute_transfer_tx` | Spend treasury UTxO and pay the approved recipient |
  | `build_execute_registry_action_tx` | Apply a `RotateAdmin` or `UpdateRegistryMember` mutation via `GovernanceApproval` redeemer (constructor 4) — no admin key needed |
  | `build_set_delegate_tx` | Set or clear a member's vote delegate — self-service, signed by member, uses `SetDelegate` redeemer (constructor 5) |
- **`datums.py`** — serialises Python datum objects back to on-chain CBOR hex (the inverse of parsing). Used to construct the inline datum on the continuing output for every validator spend.
- **`signing.py`** — integrates PyCardano to derive addresses from script hashes and verification key hashes, estimate fees, and sign a completed transaction body with an Ed25519 private key.

### 2.4 Web Dashboard — `fos_ui/`

A Flask web application that renders the live on-chain state and lets a human operator build and submit transactions directly from the browser. It does not hold any private keys — signing happens locally in the user's Cardano wallet browser extension via the **CIP-30** standard.

**Pages and API routes:**

| Route | Description |
|---|---|
| `GET /` | Main dashboard — Active/History tab bar; member rows show delegate badges and ⇒ button |
| `GET /api/state` | Full chain state as JSON (proposals, registry with `delegate` fields, treasury) |
| `POST /api/propose` | Build a create-proposal transaction |
| `POST /api/vote` | Build a cast-vote transaction |
| `POST /api/execute` | Build an execute-proposal transaction |
| `POST /api/expire` | Build an expire-proposal transaction |
| `POST /api/transfer` | Build a treasury transfer transaction |
| `POST /api/delegate` | Build a set-delegate transaction `{member_key_hash, new_delegate}` |
| `POST /api/executor/run` | Run the executor agent for an Executed proposal |
| `GET /api/audit` | Last 50 audit log entries |
| `POST /api/tx/submit` | Submit a signed CBOR hex to the chain via Blockfrost |
| `GET /api/wallet/balance` | ADA balance for an address (used by the wallet connect flow) |

When no `BLOCKFROST_PROJECT_ID` is configured, the dashboard runs in **mock mode** — it generates a realistic demo state (5 members, 3 proposals, a treasury balance) so the interface can be explored without a deployment.

**Creating a proposal**

A **New Proposal** button in the dashboard header opens a modal with four action-type cards (💸 Treasury Transfer, 📋 Off-Chain Decision, 👤 Update Member, 🔑 Rotate Admin). Selecting a type reveals the relevant fields:

- *Treasury Transfer* — recipient (member dropdown or raw key hash), ADA amount, memo; cap enforced client-side and server-side
- *Off-Chain Decision* — decision memo
- *Update Member* — target member (populated from live registry), new role, new status
- *Rotate Admin* — new admin key hash

Timeline fields (vote deadline hours, timelock hours) and quorum threshold are always shown; the quorum hint displays the maximum achievable score for the current registry. On submit the form calls `POST /api/propose`, which validates all inputs, constructs the `GovernanceDatum`, and returns an `UnsignedTransaction` routed through the same sign-and-submit flow as voting.

**Proposal card structure**

Each governance proposal is rendered as a structured card with four labeled sections:

| Section | Content |
|---|---|
| **Action** | Type tag (e.g. `💸 Treasury Transfer`) + labeled detail rows — recipient, amount, memo for transfers; target key + new role/status for member updates |
| **Timeline** | Vote deadline and execute-after (timelock) as formatted UTC dates, with "Xd Xh remaining" or "Deadline passed" and timelock status |
| **Voting Progress** | Animated progress bar, quorum percentage badge, "X of Y pts required" legend |
| **Votes Cast** | Voter chips showing truncated key hash, role (Admin/Treasurer/Member), and vote weight |

The `/api/state` response enriches each proposal with `action_details` (structured per action type), `vote_deadline_fmt`, `execute_after_fmt`, `time_remaining`, and voter role/weight resolved from the registry.

### 2.5 Builder Agent — `agent.py`

A separate Claude AI agent that generates Aiken smart contracts from natural language descriptions. It has 10 tools for file I/O, Aiken CLI invocation, and optional RAG-based retrieval of Aiken documentation. The Quorum contracts were scaffolded with this agent.

---

## 3. How It Is Used

### 3.1 Deployment

```bash
# 1. Compile the contracts
cd identity_registry && aiken build   # produces plutus.json

# 2. Deploy to preprod testnet
export BLOCKFROST_PROJECT_ID="preprod..."
export DEPLOY_SIGNING_KEY="<32-byte Ed25519 hex>"
export DEPLOY_KEY_HASH="<vkey hash>"
export DEPLOY_COLLATERAL_REF="<txhash#index>"
python3 scripts/deploy.py
# Prints REGISTRY/GOVERNANCE/TREASURY script hash env vars to export
```

The deploy script reads the compiled Plutus scripts from `plutus.json`, derives their on-chain script addresses, and submits two transactions:
1. A UTxO at the registry address containing the initial `RegistryDatum` (founding admin + empty member list)
2. A UTxO at the treasury address containing the initial `TreasuryDatum` (governance hash + ADA funding)

### 3.2 Starting the Dashboard

```bash
# Mock mode (no blockchain connection required)
python3 fos_ui/app.py          # http://localhost:5000

# Live mode
export BLOCKFROST_PROJECT_ID="preprod..."
export FOS_REGISTRY_SCRIPT_HASH="..."
export FOS_GOVERNANCE_SCRIPT_HASH="..."
export FOS_TREASURY_SCRIPT_HASH="..."
PORT=5050 python3 fos_ui/app.py
```

### 3.3 Governance Workflow (End-to-End)

```
1. Proposal creation
   └─ Operator clicks "New Proposal" in the dashboard
      Selects action type and fills in the fields
      POST /api/propose  →  build_create_proposal_tx()
      Signs with wallet (CIP-30) and submits
      A new UTxO with GovernanceDatum appears at the governance script address

2. Voting period
   └─ Each voting member calls /api/vote  →  builds a CastVote tx
      The tx appends a VoteRecord to the proposal's datum
      The governance UTxO is consumed and re-created at the same address
      with the updated vote list (continuing output invariant)

3. Quorum check
   └─ The dashboard shows weighted yes-score vs threshold in real-time
      Suspended members' votes are discounted when tallied

4. Execution (if quorum met + timelock cleared)
   └─ Operator calls /api/execute  →  flips status Voting → Executed

5. Treasury transfer (if action = TreasuryTransfer)
   └─ Operator calls /api/transfer  →  treasury UTxO is spent
      ADA is sent to the recipient address
      Remaining ADA returns to the treasury (continuing output)
      The executed governance UTxO is included as a reference input

5a. Registry mutation (if action = RotateAdmin or UpdateRegistryMember)
   └─ Agent calls execute_registry_action  →  registry UTxO is spent
      GovernanceApproval redeemer (no admin key needed)
      Registry validator verifies governance reference input by script hash,
      checks status == Executed and registry_version == current version
      New registry datum written with mutation applied and version incremented

6. Expiry (if deadline passed without quorum)
   └─ Operator calls /api/expire  →  flips status Voting → Expired
```

### 3.4 Running the Operator Agent

```bash
# One-shot check
python3 -c "from fos_agent import run_fos_agent; run_fos_agent('Check state and act')"

# Continuous monitor (every 5 minutes)
python3 -c "from fos_agent import run_monitor; run_monitor(300)"
```

Before each agent turn, `AlertManager.check()` runs and fires any pending Discord/Slack notifications. The agent then reads the full chain state via Blockfrost, identifies any proposals that are ready to vote on, execute, or expire, builds the appropriate unsigned transactions, and (in autonomous mode) submits them. Every decision is appended to `.fos_audit.jsonl`.

---

## 4. How ADA Tokens Are Used

ADA is the native token of the Cardano blockchain. In Quorum it plays several roles:

### 4.1 Treasury Holdings

The treasury validator holds the organisation's ADA balance. This is real, on-chain ADA locked at a Plutus script address. It cannot be moved by any single key — it can only be spent by a transaction that satisfies all conditions in `treasury.ak`.

### 4.2 Min-ADA on Every UTxO

Every UTxO on Cardano must carry a minimum amount of ADA (the "min-ADA" requirement, enforced by the ledger). Each governance proposal UTxO, each registry UTxO, and the treasury UTxO all hold at least the minimum required lovelace. This ADA is not "spent" in the economic sense — it is locked to keep the UTxO alive and is returned to the script or operator when the UTxO is consumed.

### 4.3 Transaction Fees

Every on-chain action (cast vote, execute proposal, treasury transfer) costs a small ADA transaction fee paid by the submitting wallet. Fees are estimated by the signing layer using Cardano's linear fee formula (`155,381 + 44 × tx_size_bytes`) plus script execution budget costs.

### 4.4 Treasury Transfers

The primary economic use case: a `TreasuryTransfer` proposal authorises a specific lovelace amount to be sent to a specific recipient key hash. The `TreasuryDatum` enforces a per-proposal cap (`max_transfer_lovelace`) as a safety limit — no single passed proposal can drain more than this cap regardless of what the governance datum says.

A complete transfer requires:
- Governance approval (weighted vote ≥ quorum)
- Timelock delay (execute-after timestamp must have passed)
- Registry consistency (proposal's pinned version = current registry version)
- Amount ≤ per-proposal cap
- Recipient is a current Active member

### 4.5 The Registry Version as ADA Safety Lock

The most subtle ADA protection is the **registry version invariant**. When a proposal is created, it records the current registry version. If any member is added, suspended, or removed after the proposal is created, the registry version increments. The governance validator then rejects any vote or execute transaction for the old proposal — the proposer must re-submit with the current membership state.

This prevents an attack where an attacker votes to transfer ADA, then waits for a legitimate member to be removed (reducing the vote weight needed to meet quorum), then executes the now-technically-passing proposal against a smaller membership base.

### 4.6 Script Hash Authentication

The treasury datum stores the `governance_script_hash` — the hash of the compiled governance validator. When a treasury transfer is executed, the treasury validator checks that the governance UTxO's address matches `ScriptCredential(governance_script_hash)`. This ensures a fake UTxO at an arbitrary address claiming status `Executed` cannot be used to drain the treasury.

---

## 5. Security Properties

| Property | Where Enforced |
|---|---|
| Funds locked to script — no single-key withdrawal | `treasury.ak` validator |
| Vote weight reflects current membership | `count_weighted_yes` checks `status == Active` at execution time |
| Stale proposals rejected after membership changes | Registry version pinning in `governance.ak` |
| Treasury only accepts verified governance UTxO | Script hash check in `treasury.ak` |
| Registry mutations via governance require verified governance UTxO | `governance_script_hash` check in `identity_registry.ak` (`GovernanceApproval` path) |
| GovernanceApproval cannot be replayed | `registry_version == datum.version` check in `identity_registry.ak` |
| `governance_script_hash` cannot be changed after deployment | Immutability assertion in all `identity_registry.ak` redeemer paths |
| Per-proposal ADA cap | `max_transfer_lovelace` in `TreasuryDatum` |
| State cannot disappear from chain | Continuing output requirement in all three validators |
| Delegation is single-hop and self-service | `SetDelegate` redeemer checks `target.delegate == None`; signed by delegator only |
| Delegated weight cannot be double-counted | `effective_vote_weight` excludes delegators who voted directly |
| Agent decisions are auditable | Every action logged to `.fos_audit.jsonl` |
| Human confirmation before on-chain submission | `FOS_AUTONOMOUS_MODE=false` (default) |

---

## 6. Technology Stack

| Component | Technology |
|---|---|
| Smart contracts | **Aiken** — a functional language for Cardano Plutus validators |
| On-chain data encoding | **PlutusData CBOR** — records as `Constr(0, fields)`, enums as `Constr(N, [])` |
| Chain indexer | **Blockfrost REST API** — reads UTxOs and inline datums |
| CBOR parsing | **cbor2** Python library |
| Address derivation & signing | **PyCardano** — `ScriptHash`, `VerificationKeyHash`, Ed25519 signing |
| AI agent | **Claude claude-sonnet-4-6** (Anthropic) via the Python SDK |
| Web server | **Flask** |
| Browser wallet integration | **CIP-30** standard — Eternl, Nami, Lace, VESPR, Yoroi, Flint |
| Frontend | Vanilla JS with Inter + JetBrains Mono fonts |
| MCP server | **`@indigoprotocol/cardano-mcp`** — Claude Code tool server for Cardano chain queries, available to the AI agent at development time |

---

## 7. File Map

```
identity_registry/
  lib/fos_types.ak          ← All shared on-chain types
  validators/
    identity_registry.ak    ← Layer 1: membership management
    governance.ak           ← Layer 2: proposals + weighted voting
    treasury.ak             ← Layer 3: ADA release logic

fos_agent/
  config.py                 ← Env var configuration
  types.py                  ← Python mirrors of fos_types.ak (with CBOR parsing)
  chain.py                  ← Blockfrost client + FOSState snapshot
  transactions.py           ← One tx builder per Quorum action
  datums.py                 ← Python → on-chain CBOR serialisation
  signing.py                ← PyCardano address derivation + Ed25519 signing
  agent.py                  ← Claude AI operator agent
  alerts.py                 ← AlertManager (Discord/Slack webhook notifications)
  executor.py               ← Executor agent (runs post-Execute mandate on-chain actions)
  proposal_agent.py         ← Proposal creation agent (natural language → GovernanceDatum)

fos_ui/
  app.py                    ← Flask routes + state → dict serialisation
  templates/index.html      ← Single-page dashboard HTML
  static/style.css          ← Design system
  static/app.js             ← CIP-30 wallet + proposal card rendering

scripts/
  deploy.py                 ← Preprod deployment (registry + treasury UTxOs)
  verify.py                 ← CIP-171 on-chain bytecode verification (metadata label 1984)

agent.py                    ← Builder agent (contract generation)
rag.py                      ← ChromaDB RAG layer
rag_ingest.py               ← Aiken docs scraper
test_fos_agent.py           ← Quorum operator agent test suite
test_agent.py               ← Builder agent test suite
```

---

*Quorum · Built with Claude Code · Cardano preprod testnet · Aiken v1.x*
