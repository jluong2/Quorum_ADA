# Quorum

**On-chain governance for Cardano — with an AI agent that monitors, votes, and executes proposals autonomously.**

Quorum is a three-layer Aiken smart contract stack (identity registry → governance → treasury) paired with a Claude-powered operator agent that watches the chain, evaluates proposals against configurable safety rules, casts votes, and triggers treasury transfers — without ever holding a private key.

---

## What makes this different

Most governance tooling assumes a human is reading dashboards and clicking buttons. Quorum is designed for an **AI agent to be the primary operator**. Humans set the rules on-chain; the agent enforces them.

- The agent reads live chain state via Blockfrost
- It reasons about *whether* it should act — not just *how*
- Every decision (approved, rejected, skipped, anomaly) is written to an append-only audit log
- In default mode it describes its intent and waits for human confirmation before submitting anything
- **Liquid democracy**: members can delegate their voting weight to another member; delegators who vote directly override their own delegation
- **Proposal deposits**: proposers lock ADA (minimum 2 ADA) when creating a proposal — refunded on pass, forfeited on expiry; deters spam without governance overhead
- **Native token treasury**: `TreasuryTransfer` proposals can include a list of `NativeToken` assets alongside ADA; the treasury validator enforces token conservation on the continuing output
- **Time-locked vesting**: `CreateVesting` proposals fund a new vesting UTxO from the treasury; the recipient claims tranches as each `release_time` passes; a new `vesting.ak` validator enforces claim rules on-chain
- **IPFS rationale documents**: proposals can link to a CID or HTTPS URL stored as tx metadata (label 675); the dashboard shows a "📄 Rationale" link on each card
- **CIP-95 DRep registration**: the agent can register as a Cardano Delegated Representative so ADA holders can delegate their on-chain voting power to it; `scripts/register_drep.py` builds the certificate, `fos_agent/drep.py` queries status
- **Webhook alerts**: Discord/Slack notifications for new proposals, quorum reached, approaching deadlines, high-value transfers, and at-risk proposals (< 48 h, < 50% participation)
- **Proposal history**: dashboard Active/History tab shows executed and expired proposals with full vote records

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Claude AI Operator Agent            │
│   reads state · evaluates rules · builds tx · logs  │
└────────────────────────┬────────────────────────────┘
                         │
          ┌──────────────▼──────────────┐
          │        Web Dashboard         │
          │  Flask · CIP-30 wallet       │
          └──────────────┬──────────────┘
                         │
    ┌────────────────────▼────────────────────┐
    │           Aiken Smart Contracts          │
    │                                          │
    │  Identity Registry  (Layer 1)            │
    │    members · roles · vote weights        │
    │           │ ref input                    │
    │  Governance         (Layer 2)            │
    │    proposals · weighted voting           │
    │           │ ref input                    │
    │  Treasury           (Layer 3)            │
    │    ADA locked · guarded release          │
    └──────────────────────────────────────────┘
```

Each layer reads the layer below it as a **reference input** — consumed for reading only, never spent — so lower-layer state is never disturbed by upper-layer transactions.

---

## Try it in two minutes (no wallet or Aiken required)

```bash
git clone https://github.com/jluong2/Quorum_ADA
cd Quorum_ADA

pip install anthropic flask cbor2

# Launch the dashboard in mock mode
python3 fos_ui/app.py
# → http://localhost:5000
```

Mock mode generates a realistic demo state (5 members, 4 proposals including a native token grant, a treasury balance, and a DRep status panel) so the full interface can be explored without a deployment or Blockfrost key.

---

## Smart contracts — `identity_registry/`

Three Aiken validators, each holding one UTxO of on-chain state:

| Layer | Validator | Responsibility |
|---|---|---|
| 1 | `identity_registry.ak` | Membership — key hashes, roles, statuses, version |
| 2 | `governance.ak` | Proposals — typed actions, weighted voting, timelock |
| 3 | `treasury.ak` | ADA — locked funds, guarded release + vesting funding |
| 4 | `vesting.ak` | Time-locked vesting — tranche-by-tranche recipient claims |

**Proposal actions:**

| Action | Execution path |
|---|---|
| `TreasuryTransfer` | Release ADA (and/or native tokens) to an approved recipient (treasury validator) |
| `RotateAdmin` | Replace the registry admin key (registry validator — no admin key needed) |
| `UpdateRegistryMember` | Change a member's role or status (registry validator — no admin key needed) |
| `OffChainDecision` | Record a governance decision with no on-chain execution |
| `CreateVesting` | Fund a time-locked vesting schedule at the vesting validator; recipient claims tranches as each `release_time` passes |

`RotateAdmin` and `UpdateRegistryMember` proposals use the `GovernanceApproval` redeemer on the registry validator. The registry verifies the governance reference input by script hash, confirms status is `Executed`, and checks the proposal's pinned `registry_version` matches the current version (replay protection).

**Key safety properties:**

- Registry mutations increment a version number. Governance proposals pin this version at creation — a membership change after a proposal is created invalidates that proposal, preventing quorum manipulation attacks.
- `RegistryDatum.governance_script_hash` is immutable after deployment. The registry validator rejects any datum that attempts to change it, preventing governance from being rewired post-deploy.
- GovernanceApproval is replay-proof. A governance proposal can only mutate the registry version it was voted on; once the version increments, the same proposal reference fails the version check.
- Treasury authenticates governance by script hash. A fake "Executed" UTxO at an arbitrary address cannot drain funds.
- Treasury conserves ADA. The continuing treasury output must hold at least `treasury_in - approved_transfer` lovelace — the submitter cannot drain remaining ADA to their change address.
- Governance datum fields are fully locked in Execute and Expire paths. All ten immutable fields (`proposer`, `description`, `action`, `votes`, `quorum`, `execute_after`, `vote_deadline`, `registry_ref`, `registry_version`, `deposit`) are checked on the continuing output — no field can be silently mutated during status transitions.
- Suspended members lose voting weight retroactively. `count_weighted_yes` checks `status == Active` at execution time, not at vote-cast time.
- Every validator requires a continuing output — state can never disappear from the chain.
- Vesting schedules are irrevocable. `CancelVesting` always returns `False` — once the treasury funds a vesting UTxO, only the designated recipient can claim it, tranche by tranche as each `release_time` passes.
- Vesting datum integrity on partial claims. After each `ClaimVested` spend the continuing output must carry the unchanged `recipient` and `proposal_ref`, only the matured tranches removed from `tranches`, and lovelace ≥ the sum of remaining tranches.

---

## AI operator agent — `fos_agent/`

A Claude-powered agent that monitors the deployed contracts and acts on pending governance items.

```bash
# One-shot: check state and act on any pending proposals
python3 -c "from fos_agent import run_fos_agent; run_fos_agent('Check state and take pending actions')"

# Continuous monitor (polls every 5 minutes)
python3 -c "from fos_agent import run_monitor; run_monitor(300)"
```

**Eight enforced safety rules (baked into the system prompt):**

1. Only vote Yes on `TreasuryTransfer` if `lovelace ≤ max_transfer_lovelace` and recipient is Active
2. Never vote or execute if `registry.version != proposal.registry_version` — flag it and require a new proposal
3. Only execute if `current_time >= execute_after` (timelock) and `yes_score >= quorum`
4. `RotateAdmin` and `TreasuryTransfer > 10 ADA` additionally require a 2/3 on-chain supermajority
5. Only trigger a treasury transfer after the execute transaction is confirmed on-chain
6. Only apply a registry mutation (`execute_registry_action`) after the execute transaction is confirmed on-chain
7. Write every decision to `.fos_audit.jsonl` — approved, rejected, skipped, or anomaly
8. With `FOS_AUTONOMOUS_MODE=false` (default), describe intent and wait for human confirmation

In default mode the agent builds `UnsignedTransaction` descriptors and waits for human confirmation. In autonomous mode (`FOS_AUTONOMOUS_MODE=true`) it signs and submits transactions directly using a two-phase flow: draft → Blockfrost evaluate (real execution units) → sign → submit.

The monitor loop also runs `AlertManager` on each cycle, sending Discord/Slack notifications when proposals are created, quorum is reached, deadlines are within 24 h, high-value transfers are active, or treasury balance drops below the configured threshold.

**Proposal creation agent** — draft proposals from natural language:

```bash
python3 -c "
from fos_agent.proposal_agent import run_proposal_agent
run_proposal_agent('Propose a 5 ADA grant to member abc123 for Q2 dev work')
"
```

The agent reads current on-chain state, validates feasibility, recommends quorum (including supermajority for high-stakes actions), shows a preview for review, and submits on confirmation.

---

## Web dashboard — `fos_ui/`

Flask app with CIP-30 wallet integration (Eternl, Nami, Lace, VESPR). The server never holds a private key — all signing happens in the browser wallet. The UI is fully responsive: on tablet/mobile the proposals grid renders first and the sidebar collapses into a horizontal-scrolling info strip below it; modals become bottom sheets; forms stack vertically.

```bash
python3 fos_ui/app.py          # mock mode → http://127.0.0.1:5000

# Live mode
export BLOCKFROST_PROJECT_ID="preprod..."
export FOS_REGISTRY_SCRIPT_HASH="..."
export FOS_GOVERNANCE_SCRIPT_HASH="..."
export FOS_TREASURY_SCRIPT_HASH="..."
export FOS_VESTING_SCRIPT_HASH="..."   # optional — enables vesting UTxO reads
python3 fos_ui/app.py
```

Dashboard security defaults: binds to `127.0.0.1` (loopback only), debug mode off, CSRF Origin check on all mutating routes. Override with `HOST=0.0.0.0` and `FLASK_DEBUG=1` if needed.

---

## Full deployment

```bash
# 0. Generate a deployer key pair (first time only)
python3 scripts/keygen.py
# Prints your address — fund it from https://docs.cardano.org/cardano-testnets/tools/faucet/
# Also prints DEPLOY_SIGNING_KEY and DEPLOY_KEY_HASH — copy them

# 1. Compile the contracts
cd identity_registry && aiken build   # produces plutus.json

# 2. Deploy to preprod testnet
export BLOCKFROST_PROJECT_ID="preprod..."
export DEPLOY_SIGNING_KEY="<from keygen.py>"
export DEPLOY_KEY_HASH="<from keygen.py>"
export DEPLOY_COLLATERAL_REF="<txhash#index of any funded UTxO>"
python3 scripts/deploy.py
# Prints REGISTRY/GOVERNANCE/TREASURY script hashes — copy them

# 3. Publish on-chain contract verification (CIP-171)
python3 scripts/verify.py --dry-run   # preview first
python3 scripts/verify.py             # publishes metadata label 1984 on-chain
# Anyone can now verify your contracts match the source at the pinned commit

# 3a. Register the agent as a CIP-95 DRep (optional — ADA holders can then delegate voting power)
export DREP_ANCHOR_URL="https://…/drep_metadata.json"   # upload scripts/drep_metadata.json first
export DREP_ANCHOR_HASH="<blake2b-256 of metadata>"
python3 scripts/register_drep.py --dry-run   # preview
python3 scripts/register_drep.py             # submits DRep registration certificate

# 4. Start the agent (confirmation mode — describes intent, waits for approval)
export FOS_REGISTRY_SCRIPT_HASH="..."
export FOS_GOVERNANCE_SCRIPT_HASH="..."
export FOS_TREASURY_SCRIPT_HASH="..."
export FOS_AGENT_KEY_HASH="..."
export ANTHROPIC_API_KEY="..."
python3 -c "from fos_agent import run_monitor; run_monitor(300)"

# 4a. OR run in autonomous mode (signs and submits without confirmation)
export FOS_AGENT_SIGNING_KEY="<32-byte Ed25519 hex>"   # agent's private key
export FOS_COLLATERAL_REF="<txhash#index>"              # UTxO with 5+ ADA
export FOS_AUTONOMOUS_MODE="true"
export FOS_VESTING_SCRIPT_HASH="..."                    # optional — enables vesting UTxO reads
python3 -c "from fos_agent import run_monitor; run_monitor(300)"
```

Full environment variable reference and architecture details: [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)

---

## Dependencies

```bash
pip install anthropic                                              # agent (required)
pip install flask cbor2                                           # dashboard + CBOR parsing
pip install PyCardano                                             # signing + address derivation
pip install chromadb sentence-transformers                        # RAG (optional)
pip install requests beautifulsoup4                               # doc ingest (optional)
```

Aiken CLI: [aiken-lang.org](https://aiken-lang.org) — required only for contract compilation, not for running the agent or dashboard.

---

## Tests

```bash
python3 test_fos_agent.py   # operator agent (no API key required — chain calls mocked)
python3 test_agent.py       # builder agent
```

---

## License

MIT
