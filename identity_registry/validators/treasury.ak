use aiken/crypto.{VerificationKeyHash}
use aiken/collection/list
use cardano/address.{Script, VerificationKey}
use cardano/assets
use cardano/transaction.{
  InlineDatum, Input, Output, OutputReference, Transaction, find_input,
}
use fos_types.{
  CreateVesting, Executed, GovernanceDatum, NativeToken, OffChainDecision,
  ProposalAction, RotateAdmin, TreasuryTransfer, UpdateRegistryMember,
  VestingDatum,
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
  // Fund a vesting schedule approved by a CreateVesting governance proposal.
  // The vesting UTxO is created at vesting_script_hash locked by a VestingDatum.
  CreateVestingSchedule {
    governance_ref: OutputReference,
    vesting_script_hash: ByteArray,
  }
}

// ─── Helpers ─────────────────────────────────────────────

// Locate, authenticate, and parse the governance reference input.
// Fails the validator if:
//   - the UTxO is absent from reference_inputs
//   - it is not at the expected governance script address
//   - its datum is not a GovernanceDatum
fn load_governance(
  reference_inputs: List<Input>,
  governance_ref: OutputReference,
  governance_script_hash: ByteArray,
) -> GovernanceDatum {
  let governance_inputs =
    list.filter(
      reference_inputs,
      fn(i) { i.output_reference == governance_ref },
    )
  expect [gov_input] = governance_inputs

  // Authenticate: verify the UTxO is locked by the real governance script.
  // Without this check, anyone could create a fake Executed UTxO.
  expect Script(script_hash) =
    gov_input.output.address.payment_credential
  expect script_hash == governance_script_hash

  expect InlineDatum(raw) = gov_input.output.datum
  expect gov_datum: GovernanceDatum = raw
  gov_datum
}

// Sum all lovelace in outputs sent to a specific verification key.
fn lovelace_to_recipient(
  outputs: List<Output>,
  recipient: VerificationKeyHash,
) -> Int {
  list.foldl(
    outputs,
    0,
    fn(o, acc) {
      when o.address.payment_credential is {
        VerificationKey(pkh) ->
          if pkh == recipient {
            acc + assets.lovelace_of(o.value)
          } else {
            acc
          }
        _ -> acc
      }
    },
  )
}

// Verify that all approved native tokens reach the recipient.
// Sums the quantity of each token across all outputs to the recipient
// and asserts each approved quantity is met.
fn tokens_to_recipient(
  outputs: List<Output>,
  recipient: VerificationKeyHash,
  tokens: List<NativeToken>,
) -> Bool {
  let recipient_value =
    list.foldl(
      outputs,
      assets.zero,
      fn(o, acc) {
        when o.address.payment_credential is {
          VerificationKey(pkh) ->
            if pkh == recipient {
              assets.merge(acc, o.value)
            } else {
              acc
            }
          _ -> acc
        }
      },
    )
  list.all(
    tokens,
    fn(t) {
      assets.quantity_of(recipient_value, t.policy_id, t.asset_name) >= t.quantity
    },
  )
}

// Verify the treasury's continuing output still holds all tokens it held
// minus any tokens approved for transfer.
fn treasury_conserves_tokens(
  own_value,
  continuing_value,
  tokens: List<NativeToken>,
) -> Bool {
  list.all(
    tokens,
    fn(t) {
      let held_before = assets.quantity_of(own_value, t.policy_id, t.asset_name)
      let held_after  = assets.quantity_of(continuing_value, t.policy_id, t.asset_name)
      held_after >= held_before - t.quantity
    },
  )
}

// ─── Validator ────────────────────────────────────────────

validator treasury {
  spend(
    datum: Option<TreasuryDatum>,
    redeemer: TreasuryRedeemer,
    own_ref: OutputReference,
    self: Transaction,
  ) -> Bool {
    expect Some(datum) = datum
    let Transaction { outputs, inputs, reference_inputs, .. } = self

    when redeemer is {
      ExecuteTransfer { governance_ref } -> {
        // ── 1. Load and authenticate the governance proposal ──
        let gov_datum =
          load_governance(reference_inputs, governance_ref, datum.governance_script_hash)

        // ── 2. Proposal must be fully executed ────────────────
        // (quorum passed + timelock elapsed — enforced by governance.ak)
        expect gov_datum.status == Executed

        // ── 3. Action must be a TreasuryTransfer ──────────────
        expect TreasuryTransfer { recipient, lovelace, tokens, .. } = gov_datum.action

        // ── 4. ADA amount must be within the per-proposal cap ─
        expect lovelace <= datum.max_transfer_lovelace

        // ── 5. Recipient receives at least the approved ADA ───
        // We sum all outputs to the recipient's key — they may receive
        // multiple outputs (e.g. value + change) in the same tx.
        let paid = lovelace_to_recipient(outputs, recipient)
        expect paid >= lovelace

        // ── 5a. Recipient receives all approved native tokens ──
        expect tokens_to_recipient(outputs, recipient, tokens)

        // ── 6. Treasury continues — state must survive the spend ─
        // The continuing output enforces:
        //   a) funds return to the same script address
        //   b) governance_script_hash is unchanged (no config hijack)
        //   c) max_transfer_lovelace is unchanged
        expect Some(own_input) = find_input(inputs, own_ref)
        let own_address = own_input.output.address

        let treasury_outputs =
          list.filter(outputs, fn(o) { o.address == own_address })
        expect [continuing] = treasury_outputs
        expect InlineDatum(raw) = continuing.datum
        expect new_datum: TreasuryDatum = raw

        // ── 7. Treasury conserves remaining ADA ──────────────────
        // Continuing output must hold at least treasury_in - approved_transfer.
        // Without this, the submitter could drain ADA to their change address.
        let treasury_in_lovelace = assets.lovelace_of(own_input.output.value)
        expect assets.lovelace_of(continuing.value) >= treasury_in_lovelace - lovelace

        // ── 7a. Treasury conserves remaining native tokens ────────
        expect
          treasury_conserves_tokens(
            own_input.output.value,
            continuing.value,
            tokens,
          )

        new_datum.governance_script_hash == datum.governance_script_hash && new_datum.max_transfer_lovelace == datum.max_transfer_lovelace
      }

      CreateVestingSchedule { governance_ref, vesting_script_hash } -> {
        // ── 1. Load and authenticate the governance proposal ──
        let gov_datum =
          load_governance(reference_inputs, governance_ref, datum.governance_script_hash)

        // ── 2. Proposal must be fully executed ────────────────
        expect gov_datum.status == Executed

        // ── 3. Action must be a CreateVesting ─────────────────
        expect CreateVesting { recipient, tranches, .. } = gov_datum.action

        // ── 4. Total lovelace to vest ─────────────────────────
        let total_lovelace =
          list.foldl(tranches, 0, fn(t, acc) { acc + t.lovelace })

        // ── 5. Vesting UTxO is created at the vesting script ──
        // Exactly one output must go to the vesting script with a correct VestingDatum.
        let vesting_outputs =
          list.filter(
            outputs,
            fn(o) {
              when o.address.payment_credential is {
                Script(sh) -> sh == vesting_script_hash
                _ -> False
              }
            },
          )
        expect [vesting_out] = vesting_outputs

        // ── 6. Vesting UTxO holds the full vesting amount ─────
        expect assets.lovelace_of(vesting_out.value) >= total_lovelace

        // ── 7. Vesting datum matches the governance action ────
        expect InlineDatum(raw) = vesting_out.datum
        expect vesting_datum: VestingDatum = raw
        expect vesting_datum.recipient == recipient
        expect vesting_datum.tranches == tranches
        expect vesting_datum.proposal_ref == governance_ref

        // ── 8. Treasury continues with remaining ADA ──────────
        expect Some(own_input) = find_input(inputs, own_ref)
        let own_address = own_input.output.address
        let treasury_outputs =
          list.filter(outputs, fn(o) { o.address == own_address })
        expect [continuing] = treasury_outputs
        expect InlineDatum(raw2) = continuing.datum
        expect new_datum: TreasuryDatum = raw2

        let treasury_in_lovelace = assets.lovelace_of(own_input.output.value)
        expect assets.lovelace_of(continuing.value) >= treasury_in_lovelace - total_lovelace

        new_datum.governance_script_hash == datum.governance_script_hash && new_datum.max_transfer_lovelace == datum.max_transfer_lovelace
      }
    }
  }

  else(_) {
    fail
  }
}

// ─── Tests ────────────────────────────────────────────────
// Pure logic tests.  Full integration (Transaction + reference inputs)
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
    TreasuryTransfer { recipient: #"aa", lovelace: 2_000_000, memo: #"", tokens: [] }
  when action is {
    TreasuryTransfer { lovelace, .. } -> lovelace == 2_000_000
    _ -> False
  }
}

test non_treasury_action_is_rejected() {
  // OffChainDecision cannot trigger a treasury release
  let action: ProposalAction = OffChainDecision { memo: #"" }
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

test treasury_lovelace_conservation_satisfied() {
  // treasury_in=10 ADA, transfer=2 ADA → continuing must have ≥ 8 ADA
  let treasury_in = 10_000_000
  let transfer = 2_000_000
  let continuing_out = 8_000_000
  continuing_out >= treasury_in - transfer
}

test treasury_lovelace_drained_to_change_is_rejected() {
  // treasury_in=10 ADA, transfer=2 ADA but only 1 ADA left → drain to change address
  let treasury_in = 10_000_000
  let transfer = 2_000_000
  let continuing_out = 1_000_000
  !(continuing_out >= treasury_in - transfer)
}

test treasury_lovelace_exact_minimum_is_allowed() {
  let treasury_in = 10_000_000
  let transfer = 2_000_000
  let continuing_out = treasury_in - transfer
  continuing_out >= treasury_in - transfer
}

// ─── Native token tests ───────────────────────────────────
// (Pure logic — Transaction tests require off-chain framework)

test empty_token_list_always_passes() {
  // all([], _) = True regardless of predicate (vacuous truth).
  let tokens: List<NativeToken> = []
  list.all(tokens, fn(_) { False }) && list.all(tokens, fn(_) { True })
}

test token_quantity_check_passes_when_sufficient() {
  let held = 500
  let required = 500
  held >= required
}

test token_quantity_check_fails_when_insufficient() {
  let held = 499
  let required = 500
  !(held >= required)
}

test token_conservation_passes_when_enough_remain() {
  // Treasury held 1000 tokens, transferred 100, should hold ≥ 900
  let before = 1000
  let after = 900
  let transferred = 100
  after >= before - transferred
}

test token_conservation_fails_when_drained() {
  // Treasury held 1000 tokens, transferred 100, but only 0 remain
  let before = 1000
  let after = 0
  let transferred = 100
  !(after >= before - transferred)
}

test treasury_transfer_with_empty_tokens_matches_old_behaviour() {
  // A TreasuryTransfer with tokens=[] should pattern-match identically.
  let action: ProposalAction =
    TreasuryTransfer { recipient: #"aa", lovelace: 2_000_000, memo: #"", tokens: [] }
  when action is {
    TreasuryTransfer { lovelace, tokens, .. } -> lovelace == 2_000_000 && tokens == []
    _ -> False
  }
}

test treasury_transfer_with_native_tokens() {
  let action: ProposalAction =
    TreasuryTransfer {
      recipient: #"aa",
      lovelace: 0,
      memo: #"",
      tokens: [NativeToken { policy_id: #"deadbeef", asset_name: #"51524d4c", quantity: 100 }],
    }
  when action is {
    TreasuryTransfer { tokens, .. } -> list.length(tokens) == 1
    _ -> False
  }
}

// ─── Vesting schedule tests ───────────────────────────────

test create_vesting_action_is_recognized() {
  let action: ProposalAction =
    CreateVesting {
      recipient: #"aa",
      tranches: [
        fos_types.VestingTranche { release_time: 1000, lovelace: 2_000_000 },
        fos_types.VestingTranche { release_time: 2000, lovelace: 3_000_000 },
      ],
      memo: #"",
    }
  when action is {
    CreateVesting { recipient, tranches, .. } ->
      recipient == #"aa" && list.length(tranches) == 2
    _ -> False
  }
}

test vesting_total_lovelace_is_sum_of_tranches() {
  let tranches = [
    fos_types.VestingTranche { release_time: 1000, lovelace: 2_000_000 },
    fos_types.VestingTranche { release_time: 2000, lovelace: 3_000_000 },
  ]
  let total = list.foldl(tranches, 0, fn(t, acc) { acc + t.lovelace })
  total == 5_000_000
}

test vesting_treasury_conservation_passes() {
  let treasury_in = 20_000_000
  let vesting_total = 5_000_000
  let continuing_out = 15_000_000
  continuing_out >= treasury_in - vesting_total
}

test vesting_treasury_conservation_fails_when_drained() {
  let treasury_in = 20_000_000
  let vesting_total = 5_000_000
  let continuing_out = 5_000_000
  !(continuing_out >= treasury_in - vesting_total)
}
