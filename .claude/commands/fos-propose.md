# Build a governance proposal transaction

Arguments: `$ARGUMENTS` — proposal details in natural language, e.g.:
- `"transfer 3 ADA to addr1q9x... for Q3 infrastructure costs"`
- `"rotate admin to abc123def456..."`
- `"update member deadbeef... to Treasurer Active"`
- `"off-chain Adopt the new security policy document"`

Parse `$ARGUMENTS` to determine:
- **action type**: TreasuryTransfer | RotateAdmin | UpdateRegistryMember | OffChainDecision
- **action fields**: recipient + lovelace + memo, new_admin, target_key + role + status, or memo
- **description**: a human-readable proposal description (derive from the argument if not explicit)

Then build the proposal transaction descriptor.

---

## Step 1 — Read current FOS state

Read `fos_agent/config.py` to get script hashes. If `BLOCKFROST_PROJECT_ID` is set, call the live Blockfrost API. Otherwise use `mock_fos_state()` from `fos_agent/chain.py`.

Show a brief state summary:
- Registry version (the proposal will pin this)
- Current quorum threshold needed
- Treasury balance and cap (for TreasuryTransfer proposals)

## Step 2 — Validate the action

**TreasuryTransfer**:
- Confirm `lovelace ≤ treasury.max_transfer_lovelace`
- Confirm recipient is an Active member in the registry (or warn if they're not)

**RotateAdmin**:
- Confirm new_admin is a valid hex key hash

**UpdateRegistryMember**:
- Confirm target member exists in the registry

**OffChainDecision**:
- No validation needed; confirm memo is not empty

Warn the user about any issues before proceeding.

## Step 3 — Build the UnsignedTransaction descriptor

Construct the transaction spec using the types from `fos_agent/transactions.py` and `fos_agent/types.py`. Show the full `UnsignedTransaction.summary()` output.

Also show the inline datum hex that will be stored on-chain (from `fos_agent/datums.py`).

## Step 4 — Show the on-chain redeemer

Print the redeemer JSON so the user understands what data is being submitted.

## Step 5 — Next steps

Tell the user what to do next:
1. Sign this transaction with their wallet or `fos_agent/signing.py`
2. Submit via Blockfrost or `cardano-cli`
3. After confirmation, the proposal will appear in the UI at http://localhost:5050

If the action is TreasuryTransfer, remind about the two-step flow:
> After this proposal reaches quorum + timelock, call `execute_proposal` first, then `execute_treasury_transfer`.
