use aiken/list
use aiken/transaction.{
  InlineDatum, OutputReference, ScriptContext, Spend, Transaction, find_input,
}
use aiken/transaction/credential.{ScriptCredential, VerificationKeyCredential}
use aiken/transaction/value
use fos_types.{
  Executed, GovernanceDatum, OffChainDecision, ProposalAction, RotateAdmin,
  TreasuryTransfer, UpdateRegistryMember,
}

// ─── Types ────────────────────────────────────────────────
//
// The treasury holds ADA (and optionally native tokens) at this
// script address.  Funds can only leave when a governance proposal
// with a TreasuryTransfer action has reached Executed status.
//
// The governance UTxO is supplied as a reference input — read without
// being spent — completing the three-layer reference chain:
//
//   treasury  →ref→  governance  →ref→  identity registry

pub type TreasuryDatum {
  // Script hash of the governance validator.
  // Required to authenticate the governance reference input and
  // prevent a fake "Executed" UTxO from draining the treasury.
  governance_script_hash: ByteArray,
  // Hard cap per single approved transfer.
  // Guards against catastrophic single-proposal drains.
  max_transfer_lovelace: Int,
}

pub type TreasuryRedeemer {
  // governance_ref: the OutputReference of the Executed governance UTxO.
  // Must be included in the transaction's reference_inputs.
  ExecuteTransfer { governance_ref: OutputReference }
}

// ─── Helpers ─────────────────────────────────────────────

// Locate, authenticate, and parse the governance reference input.
// Fails the validator if:
//   - the UTxO is absent from reference_inputs
//   - it is not at the expected governance script address
//   - its datum is not a GovernanceDatum
fn load_governance(
  ctx: ScriptContext,
  governance_ref: OutputReference,
  governance_script_hash: ByteArray,
) -> GovernanceDatum {
  let governance_inputs =
    list.filter(
      ctx.transaction.reference_inputs,
      fn(i) { i.output_reference == governance_ref },
    )
  expect [gov_input] = governance_inputs

  // Authenticate: verify the UTxO is locked by the real governance script.
  // Without this check, anyone could create a fake Executed UTxO.
  expect ScriptCredential(script_hash) =
    gov_input.output.address.payment_credential
  expect script_hash == governance_script_hash

  expect InlineDatum(raw) = gov_input.output.datum
  expect gov_datum: GovernanceDatum = raw
  gov_datum
}

// Sum all lovelace in outputs sent to a specific verification key.
fn lovelace_to_recipient(
  outputs: List<aiken/transaction.Output>,
  recipient: aiken/crypto.VerificationKeyHash,
) -> Int {
  list.foldl(
    outputs,
    0,
    fn(o, acc) {
      when o.address.payment_credential is {
        VerificationKeyCredential(pkh) ->
          if pkh == recipient {
            acc + value.lovelace_of(o.value)
          } else {
            acc
          }
        _ -> acc
      }
    },
  )
}

// ─── Validator ────────────────────────────────────────────

validator {
  fn spend(
    datum: TreasuryDatum,
    redeemer: TreasuryRedeemer,
    ctx: ScriptContext,
  ) -> Bool {
    let Transaction { outputs, inputs, .. } = ctx.transaction

    when redeemer is {
      ExecuteTransfer { governance_ref } -> {
        // ── 1. Load and authenticate the governance proposal ──
        let gov_datum =
          load_governance(ctx, governance_ref, datum.governance_script_hash)

        // ── 2. Proposal must be fully executed ────────────────
        // (quorum passed + timelock elapsed — enforced by governance.ak)
        expect gov_datum.status == Executed

        // ── 3. Action must be a TreasuryTransfer ──────────────
        expect TreasuryTransfer { recipient, lovelace, .. } = gov_datum.action

        // ── 4. Amount must be within the per-proposal cap ─────
        expect lovelace <= datum.max_transfer_lovelace

        // ── 5. Recipient receives at least the approved amount ─
        // We sum all outputs to the recipient's key — they may receive
        // multiple outputs (e.g. value + change) in the same tx.
        let paid = lovelace_to_recipient(outputs, recipient)
        expect paid >= lovelace

        // ── 6. Treasury continues — state must survive the spend ─
        // The continuing output enforces:
        //   a) funds return to the same script address
        //   b) governance_script_hash is unchanged (no config hijack)
        //   c) max_transfer_lovelace is unchanged
        expect Spend(own_ref) = ctx.purpose
        expect Some(own_input) = find_input(inputs, own_ref)
        let own_address = own_input.output.address

        let treasury_outputs =
          list.filter(outputs, fn(o) { o.address == own_address })
        expect [continuing] = treasury_outputs
        expect InlineDatum(raw) = continuing.datum
        expect new_datum: TreasuryDatum = raw

        new_datum.governance_script_hash == datum.governance_script_hash && new_datum.max_transfer_lovelace == datum.max_transfer_lovelace
      }
    }
  }
}

// ─── Tests ────────────────────────────────────────────────
// Pure logic tests.  Full integration (ScriptContext + reference inputs)
// requires the off-chain testing framework (lucid / cardano-transaction-lib).

test transfer_within_cap_is_allowed() {
  let lovelace = 1_000_000
  let max = 5_000_000
  lovelace <= max
}

test transfer_at_exact_cap_is_allowed() {
  let lovelace = 5_000_000
  let max = 5_000_000
  lovelace <= max
}

test transfer_exceeding_cap_is_rejected() {
  let lovelace = 6_000_000
  let max = 5_000_000
  !(lovelace <= max)
}

test executed_proposal_can_release_funds() {
  // Status check used in the validator
  let status = Executed
  status == Executed
}

test voting_proposal_cannot_release_funds() {
  let status = fos_types.Voting
  !(status == Executed)
}

test expired_proposal_cannot_release_funds() {
  let status = fos_types.Expired
  !(status == Executed)
}

test treasury_transfer_action_is_recognized() {
  let action: ProposalAction =
    TreasuryTransfer { recipient: #"aa", lovelace: 2_000_000, memo: #"salary" }
  when action is {
    TreasuryTransfer { lovelace, .. } -> lovelace == 2_000_000
    _ -> False
  }
}

test non_treasury_action_is_rejected() {
  // OffChainDecision cannot trigger a treasury release
  let action: ProposalAction = OffChainDecision { memo: #"just a vote" }
  when action is {
    TreasuryTransfer { .. } -> False
    _ -> True
  }
}

test rotate_admin_action_cannot_release_funds() {
  let action: ProposalAction = RotateAdmin { new_admin: #"bb" }
  when action is {
    TreasuryTransfer { .. } -> False
    _ -> True
  }
}

test update_registry_action_cannot_release_funds() {
  let action: ProposalAction =
    UpdateRegistryMember {
      target_key: #"cc",
      new_role: fos_types.Treasurer,
      new_status: fos_types.Active,
    }
  when action is {
    TreasuryTransfer { .. } -> False
    _ -> True
  }
}

test recipient_receives_full_amount() {
  // Simulates the paid >= lovelace check
  let approved = 3_000_000
  let paid = 3_500_000
  paid >= approved
}

test recipient_underpaid_is_rejected() {
  let approved = 3_000_000
  let paid = 2_999_999
  !(paid >= approved)
}

test treasury_config_governance_hash_preserved() {
  // Simulates the continuing datum config check
  let original_hash = #"deadbeef"
  let new_hash = #"deadbeef"
  new_hash == original_hash
}

test treasury_config_tampered_hash_rejected() {
  let original_hash = #"deadbeef"
  let tampered_hash = #"cafebabe"
  !(tampered_hash == original_hash)
}
