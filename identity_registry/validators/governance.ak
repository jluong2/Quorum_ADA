use aiken/crypto.{VerificationKeyHash}
use aiken/interval.{Finite}
use aiken/list
use aiken/transaction.{
  InlineDatum, OutputReference, ScriptContext, Spend, Transaction, find_input,
}
use fos_types.{
  Active, Admin, Executed, Expired, GovernanceDatum, Member, Observer, ProposalAction,
  RegistryDatum, RegistryMember, Role, RotateAdmin, Suspended, Treasurer, TreasuryTransfer,
  VoteRecord, Voting,
}

// ─── Redeemer ─────────────────────────────────────────────

pub type GovernanceRedeemer {
  // voter is explicit so any wallet can submit on behalf of the member.
  CastVote { voter: VerificationKeyHash, approve: Bool }
  Execute
  Expire
}

// ─── Vote Weights ─────────────────────────────────────────
//
// Role-weighted voting ensures the founding team retains meaningful
// influence during early governance while the Member class grows.
//
// Quorum example with 5-person org (1 Admin + 1 Treasurer + 3 Members):
//   max possible yes score = 3 + 2 + 3 = 8
//   quorum of 5 requires: Admin+Treasurer, or Admin+2 Members, etc.

fn vote_weight(role: Role) -> Int {
  when role is {
    Admin -> 3
    Treasurer -> 2
    Member -> 1
    Observer -> 0
  }
}

// ─── Helpers ─────────────────────────────────────────────

fn already_voted(votes: List<VoteRecord>, key: VerificationKeyHash) -> Bool {
  list.any(votes, fn(v) { v.voter == key })
}

fn count_weighted_yes(
  votes: List<VoteRecord>,
  members: List<RegistryMember>,
) -> Int {
  list.foldl(
    votes,
    0,
    fn(v, acc) {
      if v.approve {
        // Suspended members lose voting power retroactively.
        when list.find(members, fn(m) { m.key_hash == v.voter }) is {
          Some(reg_member) ->
            if reg_member.status == Active {
              acc + vote_weight(reg_member.role)
            } else {
              acc
            }
          None -> acc
        }
      } else {
        acc
      }
    },
  )
}

fn load_registry(
  ctx: ScriptContext,
  registry_ref: OutputReference,
  expected_version: Int,
) -> RegistryDatum {
  let registry_inputs =
    list.filter(
      ctx.transaction.reference_inputs,
      fn(i) { i.output_reference == registry_ref },
    )
  expect [reg_input] = registry_inputs
  expect InlineDatum(raw) = reg_input.output.datum
  expect registry: RegistryDatum = raw
  expect registry.version == expected_version
  registry
}

// ─── Supermajority ───────────────────────────────────────
//
// High-stakes actions require a 2/3 supermajority of the maximum possible
// weighted yes score, regardless of the proposal's own quorum field.
//
// Threshold: TreasuryTransfer > 10 ADA, or any RotateAdmin.

const high_value_lovelace: Int = 10_000_000

fn action_requires_supermajority(action: ProposalAction) -> Bool {
  when action is {
    RotateAdmin { .. } -> True
    TreasuryTransfer { lovelace, .. } -> lovelace > high_value_lovelace
    _ -> False
  }
}

fn max_possible_yes_score(members: List<RegistryMember>) -> Int {
  list.foldl(
    members,
    0,
    fn(m, acc) {
      if m.status == Active {
        acc + vote_weight(m.role)
      } else {
        acc
      }
    },
  )
}

// Two-thirds supermajority: yes_score * 3 >= max_score * 2.
fn supermajority_met(yes_score: Int, max_score: Int) -> Bool {
  yes_score * 3 >= max_score * 2
}

fn tx_lower_bound(ctx: ScriptContext) -> Int {
  expect Finite(lower) =
    ctx.transaction.validity_range.lower_bound.bound_type
  lower
}

fn continuing_datum(ctx: ScriptContext) -> GovernanceDatum {
  let Transaction { outputs, inputs, .. } = ctx.transaction
  expect Spend(own_ref) = ctx.purpose
  expect Some(own_input) = find_input(inputs, own_ref)
  let own_address = own_input.output.address
  let script_outputs =
    list.filter(outputs, fn(o) { o.address == own_address })
  expect [continuing] = script_outputs
  expect InlineDatum(raw) = continuing.datum
  expect new_datum: GovernanceDatum = raw
  new_datum
}

// ─── Validator ────────────────────────────────────────────

validator {
  fn spend(
    datum: GovernanceDatum,
    redeemer: GovernanceRedeemer,
    ctx: ScriptContext,
  ) -> Bool {
    let Transaction { extra_signatories, .. } = ctx.transaction

    when redeemer is {
      CastVote { voter, approve } -> {
        expect datum.status == Voting

        let now = tx_lower_bound(ctx)
        expect now <= datum.vote_deadline

        expect list.has(extra_signatories, voter)

        let registry =
          load_registry(ctx, datum.registry_ref, datum.registry_version)
        expect Some(reg_member) =
          list.find(registry.members, fn(m) { m.key_hash == voter })
        expect reg_member.status == Active
        expect vote_weight(reg_member.role) > 0

        expect !already_voted(datum.votes, voter)

        let new_datum = continuing_datum(ctx)
        let new_vote = VoteRecord { voter: voter, approve: approve }

        // Votes accumulate; everything else is immutable.
        new_datum.votes == [new_vote, ..datum.votes] && new_datum.status == Voting
          && new_datum.proposer == datum.proposer
          && new_datum.action == datum.action
          && new_datum.quorum == datum.quorum
          && new_datum.execute_after == datum.execute_after
          && new_datum.vote_deadline == datum.vote_deadline
          && new_datum.registry_ref == datum.registry_ref
          && new_datum.registry_version == datum.registry_version
      }

      Execute -> {
        expect datum.status == Voting

        let now = tx_lower_bound(ctx)
        expect now >= datum.execute_after

        let registry =
          load_registry(ctx, datum.registry_ref, datum.registry_version)
        let yes_score = count_weighted_yes(datum.votes, registry.members)
        expect yes_score >= datum.quorum

        // High-stakes actions (RotateAdmin, large transfers) require 2/3 supermajority
        // regardless of the proposal's own quorum field.
        expect
          !action_requires_supermajority(datum.action) || supermajority_met(
            yes_score,
            max_possible_yes_score(registry.members),
          )

        let new_datum = continuing_datum(ctx)

        // Status flips to Executed; nothing else may change.
        new_datum.status == Executed && new_datum.votes == datum.votes
          && new_datum.action == datum.action
          && new_datum.proposer == datum.proposer
          && new_datum.description == datum.description
          && new_datum.quorum == datum.quorum
          && new_datum.execute_after == datum.execute_after
          && new_datum.vote_deadline == datum.vote_deadline
          && new_datum.registry_ref == datum.registry_ref
          && new_datum.registry_version == datum.registry_version
      }

      Expire -> {
        expect datum.status == Voting

        let now = tx_lower_bound(ctx)
        expect now > datum.vote_deadline

        let registry =
          load_registry(ctx, datum.registry_ref, datum.registry_version)
        let yes_score = count_weighted_yes(datum.votes, registry.members)
        expect yes_score < datum.quorum

        let new_datum = continuing_datum(ctx)

        new_datum.status == Expired && new_datum.votes == datum.votes
          && new_datum.action == datum.action
          && new_datum.proposer == datum.proposer
          && new_datum.description == datum.description
          && new_datum.quorum == datum.quorum
          && new_datum.execute_after == datum.execute_after
          && new_datum.vote_deadline == datum.vote_deadline
          && new_datum.registry_ref == datum.registry_ref
          && new_datum.registry_version == datum.registry_version
      }
    }
  }
}

// ─── Tests ────────────────────────────────────────────────

test vote_weight_admin_is_three() {
  vote_weight(Admin) == 3
}

test vote_weight_treasurer_is_two() {
  vote_weight(Treasurer) == 2
}

test vote_weight_member_is_one() {
  vote_weight(Member) == 1
}

test vote_weight_observer_is_zero() {
  vote_weight(Observer) == 0
}

test already_voted_detects_duplicate() {
  let votes = [VoteRecord { voter: #"aa", approve: True }]
  already_voted(votes, #"aa")
}

test already_voted_misses_non_voter() {
  let votes = [VoteRecord { voter: #"aa", approve: True }]
  !already_voted(votes, #"bb")
}

test empty_votes_no_duplicate() {
  !already_voted([], #"aa")
}

test count_weighted_yes_sums_correctly() {
  // Admin yes (3) + Member yes (1) + Treasurer nay (0) = 4
  let members =
    [
      RegistryMember { key_hash: #"aa", role: Admin, joined_at: 100, status: Active },
      RegistryMember {
        key_hash: #"bb",
        role: Member,
        joined_at: 200,
        status: Active,
      },
      RegistryMember {
        key_hash: #"cc",
        role: Treasurer,
        joined_at: 300,
        status: Active,
      },
    ]
  let votes =
    [
      VoteRecord { voter: #"aa", approve: True },
      VoteRecord { voter: #"bb", approve: True },
      VoteRecord { voter: #"cc", approve: False },
    ]
  count_weighted_yes(votes, members) == 4
}

test count_weighted_yes_excludes_suspended_members() {
  let members =
    [
      RegistryMember {
        key_hash: #"aa",
        role: Admin,
        joined_at: 100,
        status: Suspended,
      },
    ]
  count_weighted_yes([VoteRecord { voter: #"aa", approve: True }], members) == 0
}

test count_weighted_yes_excludes_observer_votes() {
  let members =
    [
      RegistryMember {
        key_hash: #"aa",
        role: Observer,
        joined_at: 100,
        status: Active,
      },
    ]
  count_weighted_yes([VoteRecord { voter: #"aa", approve: True }], members) == 0
}

test quorum_passes_when_score_met() {
  // Admin (3) + Treasurer (2) = 5 ≥ quorum of 5
  let members =
    [
      RegistryMember { key_hash: #"aa", role: Admin, joined_at: 100, status: Active },
      RegistryMember {
        key_hash: #"bb",
        role: Treasurer,
        joined_at: 200,
        status: Active,
      },
    ]
  let votes =
    [
      VoteRecord { voter: #"aa", approve: True },
      VoteRecord { voter: #"bb", approve: True },
    ]
  count_weighted_yes(votes, members) >= 5
}

test quorum_rejects_when_score_not_met() {
  // Two Members (1+1=2) cannot reach quorum of 5
  let members =
    [
      RegistryMember {
        key_hash: #"aa",
        role: Member,
        joined_at: 100,
        status: Active,
      },
      RegistryMember {
        key_hash: #"bb",
        role: Member,
        joined_at: 200,
        status: Active,
      },
    ]
  let votes =
    [
      VoteRecord { voter: #"aa", approve: True },
      VoteRecord { voter: #"bb", approve: True },
    ]
  count_weighted_yes(votes, members) < 5
}

test vote_does_not_count_unknown_voters() {
  let members =
    [
      RegistryMember {
        key_hash: #"aa",
        role: Admin,
        joined_at: 100,
        status: Active,
      },
    ]
  // #"bb" is not in the registry — their vote scores 0
  count_weighted_yes([VoteRecord { voter: #"bb", approve: True }], members) == 0
}

test all_nay_votes_score_zero() {
  let members =
    [
      RegistryMember { key_hash: #"aa", role: Admin, joined_at: 100, status: Active },
      RegistryMember {
        key_hash: #"bb",
        role: Treasurer,
        joined_at: 200,
        status: Active,
      },
    ]
  let votes =
    [
      VoteRecord { voter: #"aa", approve: False },
      VoteRecord { voter: #"bb", approve: False },
    ]
  count_weighted_yes(votes, members) == 0
}

// ─── Supermajority tests ─────────────────────────────────

test rotate_admin_requires_supermajority() {
  action_requires_supermajority(RotateAdmin { new_admin: #"aa" })
}

test large_treasury_transfer_requires_supermajority() {
  // 11 ADA > 10 ADA threshold
  action_requires_supermajority(
    TreasuryTransfer { recipient: #"aa", lovelace: 11_000_000, memo: #"" },
  )
}

test small_treasury_transfer_does_not_require_supermajority() {
  // 5 ADA ≤ 10 ADA threshold
  !action_requires_supermajority(
    TreasuryTransfer { recipient: #"aa", lovelace: 5_000_000, memo: #"" },
  )
}

test offchain_decision_does_not_require_supermajority() {
  !action_requires_supermajority(fos_types.OffChainDecision { memo: #"adopt policy" })
}

test max_possible_yes_score_sums_active_members() {
  // Admin(3) + Treasurer(2) + suspended_member(0) = 5
  let members =
    [
      RegistryMember { key_hash: #"aa", role: Admin, joined_at: 100, status: Active },
      RegistryMember {
        key_hash: #"bb",
        role: Treasurer,
        joined_at: 200,
        status: Active,
      },
      RegistryMember {
        key_hash: #"cc",
        role: Member,
        joined_at: 300,
        status: Suspended,
      },
    ]
  max_possible_yes_score(members) == 5
}

test supermajority_met_two_thirds() {
  // 4 out of 6 max = 66.7% ≥ 2/3 → passes (4*3=12 >= 6*2=12)
  supermajority_met(4, 6)
}

test supermajority_not_met_below_two_thirds() {
  // 3 out of 6 max = 50% < 2/3 → fails (3*3=9 < 6*2=12)
  !supermajority_met(3, 6)
}

test supermajority_met_unanimous() {
  supermajority_met(6, 6)
}

test supermajority_at_exact_threshold() {
  // 2 out of 3 max: 2*3=6 >= 3*2=6 → exactly at boundary, passes
  supermajority_met(2, 3)
}
