use aiken/crypto.{VerificationKeyHash}
use aiken/interval.{Finite}
use aiken/list
use aiken/transaction.{
  InlineDatum, OutputReference, ScriptContext, Spend, Transaction, find_input,
}
use fos_types.{
  Active, Admin, Executed, Expired, GovernanceDatum, Member, Observer, RegistryDatum,
  RegistryMember, Role, Suspended, Treasurer, VoteRecord, Voting,
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

        let new_datum = continuing_datum(ctx)

        // Status flips to Executed; nothing else may change.
        new_datum.status == Executed && new_datum.votes == datum.votes
          && new_datum.action == datum.action
          && new_datum.proposer == datum.proposer
          && new_datum.quorum == datum.quorum
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
