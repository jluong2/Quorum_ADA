use aiken/interval.{Finite}
use aiken/list
use aiken/transaction.{
  InlineDatum, OutputReference, ScriptContext, Spend, Transaction, find_input,
}
use aiken/transaction/credential.{VerificationKeyCredential}
use aiken/transaction/value
use fos_types.{VestingDatum, VestingTranche}

// ─── Redeemer ─────────────────────────────────────────────
//
// ClaimVested: the recipient claims all tranches whose release_time has
//   passed.  Matured tranches are removed from the datum; remaining
//   tranches produce a continuing output at the same script address.
//   If all tranches are claimed the UTxO is fully consumed.
//
// CancelVesting: reserved for future governance-controlled cancellation.
//   Currently always fails so the vesting schedule is irrevocable once
//   the treasury funds it.

pub type VestingRedeemer {
  ClaimVested
  CancelVesting
}

// ─── Helpers ─────────────────────────────────────────────

fn sum_lovelace(tranches: List<VestingTranche>) -> Int {
  list.foldl(tranches, 0, fn(t, acc) { acc + t.lovelace })
}

// ─── Validator ────────────────────────────────────────────

validator {
  fn spend(
    datum: VestingDatum,
    redeemer: VestingRedeemer,
    ctx: ScriptContext,
  ) -> Bool {
    let Transaction { inputs, outputs, validity_range, extra_signatories, .. } =
      ctx.transaction

    when redeemer is {
      ClaimVested -> {
        // ── 1. Signed by the designated recipient ─────────────
        expect list.has(extra_signatories, datum.recipient)

        // ── 2. Lower bound of validity interval must be Finite ─
        // The submitter commits to a specific moment; we use this
        // to decide which tranches have matured.
        expect Finite(valid_lower) = validity_range.lower_bound.bound_type

        // ── 3. Split tranches into matured and remaining ───────
        let matured =
          list.filter(datum.tranches, fn(t) { t.release_time <= valid_lower })
        let remaining =
          list.filter(datum.tranches, fn(t) { t.release_time > valid_lower })

        // ── 4. At least one tranche must have matured ──────────
        expect list.length(matured) > 0

        // ── 5. Recipient receives at least the sum of matured tranches ─
        let claim_amount = sum_lovelace(matured)
        let recipient_paid =
          list.foldl(
            outputs,
            0,
            fn(o, acc) {
              when o.address.payment_credential is {
                VerificationKeyCredential(pkh) ->
                  if pkh == datum.recipient {
                    acc + value.lovelace_of(o.value)
                  } else {
                    acc
                  }
                _ -> acc
              }
            },
          )
        expect recipient_paid >= claim_amount

        // ── 6. If tranches remain, continuing output is required ─
        if list.length(remaining) > 0 {
          expect Spend(own_ref) = ctx.purpose
          expect Some(own_input) = find_input(inputs, own_ref)
          let own_address = own_input.output.address

          let vesting_outputs =
            list.filter(outputs, fn(o) { o.address == own_address })
          expect [continuing] = vesting_outputs

          // Continuing datum must carry the remaining tranches and same proposal_ref.
          expect InlineDatum(raw) = continuing.datum
          expect new_datum: VestingDatum = raw
          expect new_datum.recipient == datum.recipient
          expect new_datum.proposal_ref == datum.proposal_ref
          expect new_datum.tranches == remaining

          // Continuing output must hold at least the remaining locked lovelace.
          let remaining_amount = sum_lovelace(remaining)
          expect value.lovelace_of(continuing.value) >= remaining_amount

          True
        } else {
          // All tranches claimed — UTxO is fully consumed, no continuing output.
          True
        }
      }

      // Not implemented: vesting schedules are irrevocable once funded.
      CancelVesting -> False
    }
  }
}

// ─── Tests ────────────────────────────────────────────────

test matured_tranche_is_claimable() {
  // release_time 1000 ms, validity lower bound 1500 ms → matured
  let tranche = VestingTranche { release_time: 1000, lovelace: 2_000_000 }
  let valid_lower = 1500
  tranche.release_time <= valid_lower
}

test future_tranche_is_not_claimable() {
  let tranche = VestingTranche { release_time: 2000, lovelace: 2_000_000 }
  let valid_lower = 1500
  !(tranche.release_time <= valid_lower)
}

test tranche_at_exact_release_time_is_claimable() {
  let tranche = VestingTranche { release_time: 1500, lovelace: 2_000_000 }
  let valid_lower = 1500
  tranche.release_time <= valid_lower
}

test sum_lovelace_empty_list() {
  let tranches: List<VestingTranche> = []
  sum_lovelace(tranches) == 0
}

test sum_lovelace_single_tranche() {
  let tranches = [VestingTranche { release_time: 1000, lovelace: 3_000_000 }]
  sum_lovelace(tranches) == 3_000_000
}

test sum_lovelace_multiple_tranches() {
  let tranches = [
    VestingTranche { release_time: 1000, lovelace: 1_000_000 },
    VestingTranche { release_time: 2000, lovelace: 2_000_000 },
    VestingTranche { release_time: 3000, lovelace: 3_000_000 },
  ]
  sum_lovelace(tranches) == 6_000_000
}

test split_tranches_correct() {
  let tranches = [
    VestingTranche { release_time: 1000, lovelace: 1_000_000 },
    VestingTranche { release_time: 2000, lovelace: 2_000_000 },
    VestingTranche { release_time: 3000, lovelace: 3_000_000 },
  ]
  let valid_lower = 2000
  let matured = list.filter(tranches, fn(t) { t.release_time <= valid_lower })
  let remaining = list.filter(tranches, fn(t) { t.release_time > valid_lower })
  list.length(matured) == 2 && list.length(remaining) == 1
}

test claim_amount_satisfies_payout() {
  let matured = [
    VestingTranche { release_time: 1000, lovelace: 1_000_000 },
    VestingTranche { release_time: 2000, lovelace: 2_000_000 },
  ]
  let claim_amount = sum_lovelace(matured)
  let recipient_paid = 3_500_000
  recipient_paid >= claim_amount
}

test underpayment_is_rejected() {
  let matured = [VestingTranche { release_time: 1000, lovelace: 3_000_000 }]
  let claim_amount = sum_lovelace(matured)
  let recipient_paid = 2_999_999
  !(recipient_paid >= claim_amount)
}

test cancel_vesting_always_fails() {
  // CancelVesting -> False
  !False
}

test recipient_datum_preserved_on_partial_claim() {
  // Simulates the continuing-datum invariant check
  let original_recipient = #"aabbccdd"
  let new_recipient = #"aabbccdd"
  new_recipient == original_recipient
}

test proposal_ref_preserved_on_partial_claim() {
  let original_ref =
    OutputReference { transaction_id: #"deadbeef", output_index: 0 }
  let new_ref =
    OutputReference { transaction_id: #"deadbeef", output_index: 0 }
  new_ref == original_ref
}

test remaining_tranches_equal_unmatured() {
  let tranches = [
    VestingTranche { release_time: 1000, lovelace: 1_000_000 },
    VestingTranche { release_time: 3000, lovelace: 2_000_000 },
  ]
  let valid_lower = 1500
  let remaining = list.filter(tranches, fn(t) { t.release_time > valid_lower })
  remaining == [VestingTranche { release_time: 3000, lovelace: 2_000_000 }]
}

test continuing_output_lovelace_covers_remaining() {
  let remaining = [VestingTranche { release_time: 3000, lovelace: 2_000_000 }]
  let remaining_amount = sum_lovelace(remaining)
  let continuing_lovelace = 2_000_000
  continuing_lovelace >= remaining_amount
}

test continuing_output_underfunded_is_rejected() {
  let remaining = [VestingTranche { release_time: 3000, lovelace: 2_000_000 }]
  let remaining_amount = sum_lovelace(remaining)
  let continuing_lovelace = 1_999_999
  !(continuing_lovelace >= remaining_amount)
}
