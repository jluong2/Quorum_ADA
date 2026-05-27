// Shared type definitions for the Cardano Financial OS.
// All three validators (identity, governance, treasury) import from here
// so the type system is consistent across the entire stack.

use aiken/crypto.{VerificationKeyHash}
use aiken/transaction.{OutputReference}

// ─── Identity Layer ───────────────────────────────────────

pub type Role {
  Admin
  Treasurer
  Member
  Observer
}

pub type MemberStatus {
  Active
  Suspended
  Removed
}

// Named RegistryMember (not Member) to avoid a name clash with
// the Member role constructor when both are imported in the same scope.
pub type RegistryMember {
  key_hash: VerificationKeyHash,
  role: Role,
  joined_at: Int,
  status: MemberStatus,
  // Liquid democracy: delegate voting power to another active member.
  // None means vote directly; Some(key) means this member's weight flows
  // to key when key votes (and this member has not voted directly).
  // Chains are not allowed — the target's delegate must be None.
  delegate: Option<VerificationKeyHash>,
}

pub type RegistryDatum {
  members: List<RegistryMember>,
  admin: VerificationKeyHash,
  // Monotonically increasing.  Governance and treasury pin this at
  // proposal creation time — a registry update invalidates open proposals.
  version: Int,
  // Script hash of the governance validator.  Immutable after deployment.
  // Lets the registry verify that a GovernanceApproval redeemer references
  // a genuine governance UTxO and not a counterfeit one.
  governance_script_hash: ByteArray,
}

// ─── Governance Layer ─────────────────────────────────────

// A single Cardano native token component of a treasury transfer.
// policy_id: 28-byte minting policy hash (hex)
// asset_name: 0–32 bytes UTF-8 or hex label
// quantity: number of tokens to transfer
pub type NativeToken {
  policy_id: ByteArray,
  asset_name: ByteArray,
  quantity: Int,
}

// Structured on-chain effect that executes when a proposal passes.
// The treasury validator pattern-matches on TreasuryTransfer to release funds.
// Future actions (AddMemberAction, etc.) let governance replace the
// identity registry's admin-only mutation model.
pub type ProposalAction {
  TreasuryTransfer {
    recipient: VerificationKeyHash,
    lovelace: Int,
    memo: ByteArray,
    // Native tokens to transfer alongside the ADA.  Empty list = ADA-only.
    tokens: List<NativeToken>,
  }
  RotateAdmin { new_admin: VerificationKeyHash }
  UpdateRegistryMember {
    target_key: VerificationKeyHash,
    new_role: Role,
    new_status: MemberStatus,
  }
  // Governance decisions that have no mandatory on-chain execution
  // (e.g. electing an officer, ratifying a policy).
  OffChainDecision { memo: ByteArray }
}

pub type VoteRecord {
  voter: VerificationKeyHash,
  approve: Bool,
}

pub type ProposalStatus {
  Voting
  Executed
  Expired
}

pub type GovernanceDatum {
  proposer: VerificationKeyHash,
  description: ByteArray,
  // The structured action to execute when the proposal passes.
  action: ProposalAction,
  votes: List<VoteRecord>,
  status: ProposalStatus,
  vote_deadline: Int,
  execute_after: Int,
  quorum: Int,
  registry_ref: OutputReference,
  registry_version: Int,
  // ADA (lovelace) locked by the proposer at creation time.
  // Refunded to the proposer when Execute succeeds; forfeited (stays locked)
  // if the proposal expires without quorum.  Must be >= min_deposit in governance.ak.
  deposit: Int,
}
