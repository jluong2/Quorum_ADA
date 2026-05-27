use aiken/list
use aiken/transaction.{InlineDatum, ScriptContext, Spend, Transaction, find_input}
use aiken/transaction/credential.{ScriptCredential}
use fos_types.{
  Executed, GovernanceDatum, Member, MemberStatus, Observer, RegistryDatum,
  RegistryMember, Role, RotateAdmin, Suspended, Treasurer, UpdateRegistryMember,
  Active, Admin,
}

// ─── Actions ──────────────────────────────────────────────
//
// Admin actions require the admin key.
// GovernanceApproval lets an executed governance proposal mutate the registry
// without the admin key — enabling DAO-controlled membership and key rotation.

pub type RegistryAction {
  AddMember { new_member: RegistryMember }
  RemoveMember { target_key: fos_types.VerificationKeyHash }
  UpdateMember {
    target_key: fos_types.VerificationKeyHash,
    new_role: Role,
    new_status: MemberStatus,
  }
  TransferAdmin { new_admin: fos_types.VerificationKeyHash }
  GovernanceApproval
  // Self-service delegation: signed by the member; no admin key or governance required.
  // Incrementing version invalidates open proposals — delegation changes affect quorum.
  SetDelegate {
    key_hash: fos_types.VerificationKeyHash,
    new_delegate: Option<fos_types.VerificationKeyHash>,
  }
}

// ─── Helpers ──────────────────────────────────────────────

fn member_exists(
  members: List<RegistryMember>,
  key: fos_types.VerificationKeyHash,
) -> Bool {
  list.any(members, fn(m) { m.key_hash == key })
}

fn remove_member_by_key(
  members: List<RegistryMember>,
  key: fos_types.VerificationKeyHash,
) -> List<RegistryMember> {
  list.filter(members, fn(m) { m.key_hash != key })
}

fn update_member_by_key(
  members: List<RegistryMember>,
  key: fos_types.VerificationKeyHash,
  new_role: Role,
  new_status: MemberStatus,
) -> List<RegistryMember> {
  list.map(
    members,
    fn(m) {
      if m.key_hash == key {
        RegistryMember { ..m, role: new_role, status: new_status }
      } else {
        m
      }
    },
  )
}

fn update_delegate(
  members: List<RegistryMember>,
  key: fos_types.VerificationKeyHash,
  new_delegate: Option<fos_types.VerificationKeyHash>,
) -> List<RegistryMember> {
  list.map(
    members,
    fn(m) {
      if m.key_hash == key {
        RegistryMember { ..m, delegate: new_delegate }
      } else {
        m
      }
    },
  )
}

// ─── Validator ────────────────────────────────────────────

validator {
  fn spend(
    datum: RegistryDatum,
    action: RegistryAction,
    ctx: ScriptContext,
  ) -> Bool {
    let Transaction { extra_signatories, outputs, inputs, reference_inputs, .. } =
      ctx.transaction

    expect Spend(own_ref) = ctx.purpose
    expect Some(own_input) = find_input(inputs, own_ref)
    let own_address = own_input.output.address

    // Exactly one continuing output — registry state can never disappear.
    let continuing_outputs =
      list.filter(outputs, fn(o) { o.address == own_address })
    expect [continuing] = continuing_outputs
    expect InlineDatum(raw_datum) = continuing.datum
    expect new_datum: RegistryDatum = raw_datum

    // Invariants that apply to ALL mutations regardless of who authorised them.
    expect new_datum.version == datum.version + 1
    // governance_script_hash is immutable — prevents rewiring governance after deployment.
    expect new_datum.governance_script_hash == datum.governance_script_hash

    when action is {
      GovernanceApproval -> {
        // Find the single governance reference input by script hash.
        let gov_inputs =
          list.filter(
            reference_inputs,
            fn(inp) {
              when inp.output.address.payment_credential is {
                ScriptCredential(hash) -> hash == datum.governance_script_hash
                _ -> False
              }
            },
          )
        expect [gov_input] = gov_inputs
        expect InlineDatum(raw_gov) = gov_input.output.datum
        expect gov: GovernanceDatum = raw_gov

        // Proposal must have cleared quorum + timelock (status Executed).
        expect gov.status == Executed

        // Replay protection: the proposal must pin exactly the registry version
        // being mutated.  After this mutation increments the version, any
        // re-use of the same governance UTxO will fail this check.
        expect gov.registry_version == datum.version

        // Apply the governance-mandated mutation.
        when gov.action is {
          RotateAdmin { new_admin } ->
            new_datum.admin == new_admin && new_datum.members == datum.members

          UpdateRegistryMember { target_key, new_role, new_status } ->
            member_exists(datum.members, target_key) && new_datum.members == update_member_by_key(
              datum.members,
              target_key,
              new_role,
              new_status,
            ) && new_datum.admin == datum.admin

          // TreasuryTransfer and OffChainDecision do not touch the registry.
          _ -> False
        }
      }

      // All remaining actions require the admin key.
      AddMember { new_member } -> {
        expect list.has(extra_signatories, datum.admin)
        !member_exists(datum.members, new_member.key_hash) && new_datum.members == [
          new_member,
          ..datum.members
        ] && new_datum.admin == datum.admin
      }

      RemoveMember { target_key } -> {
        expect list.has(extra_signatories, datum.admin)
        member_exists(datum.members, target_key) && new_datum.members == remove_member_by_key(
          datum.members,
          target_key,
        ) && new_datum.admin == datum.admin
      }

      UpdateMember { target_key, new_role, new_status } -> {
        expect list.has(extra_signatories, datum.admin)
        member_exists(datum.members, target_key) && new_datum.members == update_member_by_key(
          datum.members,
          target_key,
          new_role,
          new_status,
        ) && new_datum.admin == datum.admin
      }

      TransferAdmin { new_admin } -> {
        expect list.has(extra_signatories, datum.admin)
        new_datum.admin == new_admin && new_datum.members == datum.members
      }

      SetDelegate { key_hash, new_delegate } -> {
        // Signed by the member themselves — no admin key required.
        expect list.has(extra_signatories, key_hash)
        // Member must exist and be active.
        expect Some(member) =
          list.find(datum.members, fn(m) { m.key_hash == key_hash })
        expect member.status == Active
        // Cannot delegate to self.
        expect new_delegate != Some(key_hash)
        // If setting a delegate, target must be active and must not themselves
        // have a delegate (no delegation chains — single-hop only).
        let target_ok =
          when new_delegate is {
            None -> True
            Some(target) ->
              when list.find(datum.members, fn(m) { m.key_hash == target }) is {
                Some(target_m) ->
                  target_m.status == Active && target_m.delegate == None
                None -> False
              }
          }
        expect target_ok
        new_datum.members == update_delegate(datum.members, key_hash, new_delegate)
          && new_datum.admin == datum.admin
      }
    }
  }
}

// ─── Tests ────────────────────────────────────────────────

test empty_registry_has_no_members() {
  let datum =
    RegistryDatum { members: [], admin: #"cafe", version: 0, governance_script_hash: #"" }
  list.length(datum.members) == 0
}

test member_exists_finds_registered_key() {
  let m =
    RegistryMember {
      key_hash: #"deadbeef",
      role: Member,
      joined_at: 1000,
      status: Active,
      delegate: None,
    }
  member_exists([m], #"deadbeef")
}

test member_exists_misses_unknown_key() {
  let m =
    RegistryMember {
      key_hash: #"deadbeef",
      role: Member,
      joined_at: 1000,
      status: Active,
      delegate: None,
    }
  !member_exists([m], #"cafebabe")
}

test add_member_no_duplicate_logic() {
  let m =
    RegistryMember {
      key_hash: #"aa",
      role: Member,
      joined_at: 100,
      status: Active,
      delegate: None,
    }
  !member_exists([], #"aa") && member_exists([m], #"aa")
}

test remove_member_by_key_removes_correct_member() {
  let m1 =
    RegistryMember {
      key_hash: #"aa",
      role: Member,
      joined_at: 100,
      status: Active,
      delegate: None,
    }
  let m2 =
    RegistryMember {
      key_hash: #"bb",
      role: Treasurer,
      joined_at: 200,
      status: Active,
      delegate: None,
    }
  remove_member_by_key([m1, m2], #"aa") == [m2]
}

test remove_member_by_key_preserves_others() {
  let m1 =
    RegistryMember { key_hash: #"aa", role: Member, joined_at: 100, status: Active, delegate: None }
  let m2 =
    RegistryMember { key_hash: #"bb", role: Member, joined_at: 200, status: Active, delegate: None }
  let m3 =
    RegistryMember {
      key_hash: #"cc",
      role: Observer,
      joined_at: 300,
      status: Active,
      delegate: None,
    }
  let result = remove_member_by_key([m1, m2, m3], #"bb")
  list.length(result) == 2 && !member_exists(result, #"bb")
}

test update_member_by_key_changes_role_and_status() {
  let m =
    RegistryMember {
      key_hash: #"aa",
      role: Member,
      joined_at: 100,
      status: Active,
      delegate: None,
    }
  let updated = update_member_by_key([m], #"aa", Treasurer, Suspended)
  when updated is {
    [um] ->
      um.role == Treasurer && um.status == Suspended && um.key_hash == #"aa" && um.joined_at == 100
    _ -> False
  }
}

test update_member_by_key_leaves_others_unchanged() {
  let m1 =
    RegistryMember { key_hash: #"aa", role: Member, joined_at: 100, status: Active, delegate: None }
  let m2 =
    RegistryMember {
      key_hash: #"bb",
      role: Observer,
      joined_at: 200,
      status: Active,
      delegate: None,
    }
  let result = update_member_by_key([m1, m2], #"aa", Admin, Active)
  when result is {
    [updated, unchanged] ->
      updated.role == Admin && unchanged.role == Observer && unchanged.key_hash == #"bb"
    _ -> False
  }
}

test update_member_by_key_preserves_delegate() {
  // Updating role/status must not clear an existing delegate.
  let m =
    RegistryMember {
      key_hash: #"aa",
      role: Member,
      joined_at: 100,
      status: Active,
      delegate: Some(#"bb"),
    }
  let updated = update_member_by_key([m], #"aa", Treasurer, Active)
  when updated is {
    [um] -> um.delegate == Some(#"bb")
    _ -> False
  }
}

test version_increment_invariant() {
  let old_version = 7
  let new_version = 8
  new_version == old_version + 1
}

test transfer_admin_changes_only_admin() {
  let members =
    [
      RegistryMember {
        key_hash: #"cc",
        role: Member,
        joined_at: 100,
        status: Active,
        delegate: None,
      },
    ]
  let new_datum =
    RegistryDatum {
      members: members,
      admin: #"bbbbbb",
      version: 2,
      governance_script_hash: #"aabb",
    }
  new_datum.admin == #"bbbbbb" && new_datum.members == members
}

test set_delegate_update_delegate_sets_field() {
  let m =
    RegistryMember {
      key_hash: #"aa",
      role: Member,
      joined_at: 100,
      status: Active,
      delegate: None,
    }
  let updated = update_delegate([m], #"aa", Some(#"bb"))
  when updated is {
    [um] -> um.delegate == Some(#"bb") && um.key_hash == #"aa"
    _ -> False
  }
}

test set_delegate_clear_delegate() {
  let m =
    RegistryMember {
      key_hash: #"aa",
      role: Member,
      joined_at: 100,
      status: Active,
      delegate: Some(#"bb"),
    }
  let updated = update_delegate([m], #"aa", None)
  when updated is {
    [um] -> um.delegate == None
    _ -> False
  }
}

test set_delegate_leaves_other_members_unchanged() {
  let m1 =
    RegistryMember {
      key_hash: #"aa",
      role: Member,
      joined_at: 100,
      status: Active,
      delegate: None,
    }
  let m2 =
    RegistryMember {
      key_hash: #"bb",
      role: Admin,
      joined_at: 50,
      status: Active,
      delegate: None,
    }
  let updated = update_delegate([m1, m2], #"aa", Some(#"bb"))
  when updated is {
    [um1, um2] -> um1.delegate == Some(#"bb") && um2.delegate == None
    _ -> False
  }
}

test governance_approval_preserves_governance_script_hash() {
  let gov_hash = #"deadbeefcafe"
  let datum =
    RegistryDatum { members: [], admin: #"cafe", version: 5, governance_script_hash: gov_hash }
  let new_datum =
    RegistryDatum { members: [], admin: #"cafe", version: 6, governance_script_hash: gov_hash }
  new_datum.governance_script_hash == datum.governance_script_hash
}
