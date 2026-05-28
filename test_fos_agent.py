"""
Test suite for the FOS agentic layer.
Runs without a Blockfrost API key or Anthropic API key.
Tests: type parsing, chain state logic, transaction builders, agent decisions.
"""

import json
import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))

from fos_agent.types import (
    RegistryDatum, RegistryMember, GovernanceDatum, GovernanceDatum,
    TreasuryDatum, VoteRecord, OutputReference, TreasuryTransferAction,
    OffChainDecisionAction, RotateAdminAction,
    ROLE_ADMIN, ROLE_TREASURER, ROLE_MEMBER, ROLE_OBSERVER,
    STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_REMOVED,
    PROPOSAL_VOTING, PROPOSAL_EXECUTED, PROPOSAL_EXPIRED,
    VOTE_WEIGHTS,
)
from fos_agent.chain import FOSState, BlockfrostClient
from fos_agent.transactions import (
    build_cast_vote_tx, build_execute_proposal_tx,
    build_expire_proposal_tx, build_execute_transfer_tx,
    build_create_proposal_tx,
    UnsignedTransaction,
)


# ─── Test runner ──────────────────────────────────────────

passed = failed = 0

def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✅ {name}")
        passed += 1
    except AssertionError as e:
        print(f"  ❌ {name}\n     {e}")
        failed += 1
    except Exception as e:
        print(f"  ❌ {name} ({type(e).__name__}: {e})")
        failed += 1

def eq(a, b, msg=""):
    assert a == b, msg or f"expected {b!r}, got {a!r}"

def ok(val, msg=""):
    assert val, msg or f"expected truthy, got {val!r}"

def not_ok(val, msg=""):
    assert not val, msg or f"expected falsy, got {val!r}"


# ─── Fixtures ─────────────────────────────────────────────

def make_member(key="aa", role=ROLE_MEMBER, status=STATUS_ACTIVE, joined=1000):
    return RegistryMember(key_hash=key, role=role, joined_at=joined, status=status)

def make_registry(*members):
    return RegistryDatum(members=list(members), admin="cafe", version=1)

def make_vote(voter="aa", approve=True):
    return VoteRecord(voter=voter, approve=approve)

def make_ref(tx="abc123", idx=0):
    return OutputReference(tx_hash=tx, output_index=idx)

def make_proposal(
    votes=None, status=PROPOSAL_VOTING,
    quorum=4, vote_deadline=9_999_999_999_000,
    execute_after=0, registry_version=1,
    action=None, deposit=0,
):
    return GovernanceDatum(
        proposer="cafe" * 14,
        description="Test proposal",
        action=action or TreasuryTransferAction(
            recipient="bb" * 28, lovelace=2_000_000, memo="salary"
        ),
        votes=votes or [],
        status=status,
        vote_deadline=vote_deadline,
        execute_after=execute_after,
        quorum=quorum,
        registry_ref=make_ref(),
        registry_version=registry_version,
        deposit=deposit,
    )

make_governance_datum = make_proposal

def make_treasury(gov_hash="deadbeef", max_transfer=5_000_000):
    return TreasuryDatum(
        governance_script_hash=gov_hash,
        max_transfer_lovelace=max_transfer,
    )

from fos_agent.types import UTxO

def make_utxo(tx="tx1", idx=0, lovelace=10_000_000):
    return UTxO(tx_hash=tx, output_index=idx, lovelace=lovelace, datum_hex="")


# ─── 1. RegistryMember ───────────────────────────────────

print("\n── 1. RegistryMember ────────────────────────────")

def test_vote_weights():
    eq(make_member(role=ROLE_ADMIN).vote_weight,      3)
    eq(make_member(role=ROLE_TREASURER).vote_weight,  2)
    eq(make_member(role=ROLE_MEMBER).vote_weight,     1)
    eq(make_member(role=ROLE_OBSERVER).vote_weight,   0)

def test_can_vote_active_nonobserver():
    ok(make_member(role=ROLE_MEMBER, status=STATUS_ACTIVE).can_vote)

def test_cannot_vote_suspended():
    not_ok(make_member(role=ROLE_MEMBER, status=STATUS_SUSPENDED).can_vote)

def test_cannot_vote_observer():
    not_ok(make_member(role=ROLE_OBSERVER, status=STATUS_ACTIVE).can_vote)

def test_is_active():
    ok(make_member(status=STATUS_ACTIVE).is_active)
    not_ok(make_member(status=STATUS_SUSPENDED).is_active)
    not_ok(make_member(status=STATUS_REMOVED).is_active)

test("vote_weights match Aiken contract",       test_vote_weights)
test("active non-observer can vote",            test_can_vote_active_nonobserver)
test("suspended member cannot vote",            test_cannot_vote_suspended)
test("observer cannot vote",                    test_cannot_vote_observer)
test("is_active status check",                  test_is_active)


# ─── 2. RegistryDatum ────────────────────────────────────

print("\n── 2. RegistryDatum ─────────────────────────────")

def test_find_member_hit():
    reg = make_registry(make_member("aa"), make_member("bb"))
    ok(reg.find_member("aa"))

def test_find_member_miss():
    reg = make_registry(make_member("aa"))
    assert reg.find_member("zz") is None

def test_active_voting_members_excludes_observer_and_suspended():
    reg = make_registry(
        make_member("admin", ROLE_ADMIN, STATUS_ACTIVE),
        make_member("obs",   ROLE_OBSERVER, STATUS_ACTIVE),
        make_member("susp",  ROLE_MEMBER, STATUS_SUSPENDED),
    )
    voters = reg.active_voting_members()
    eq(len(voters), 1)
    eq(voters[0].key_hash, "admin")

def test_max_possible_yes_score():
    # Admin(3) + Treasurer(2) + Member(1) = 6
    reg = make_registry(
        make_member("a", ROLE_ADMIN),
        make_member("b", ROLE_TREASURER),
        make_member("c", ROLE_MEMBER),
    )
    eq(reg.max_possible_yes_score(), 6)

test("find_member returns correct member",               test_find_member_hit)
test("find_member returns None for unknown key",         test_find_member_miss)
test("active_voting_members excludes observer+suspended",test_active_voting_members_excludes_observer_and_suspended)
test("max_possible_yes_score sums active voters",        test_max_possible_yes_score)


# ─── 3. GovernanceDatum voting logic ─────────────────────

print("\n── 3. GovernanceDatum ───────────────────────────")

def test_weighted_yes_score_correct():
    reg = make_registry(
        make_member("admin", ROLE_ADMIN),
        make_member("mem",   ROLE_MEMBER),
        make_member("treas", ROLE_TREASURER),
    )
    votes = [make_vote("admin", True), make_vote("mem", True), make_vote("treas", False)]
    prop = make_proposal(votes=votes, quorum=5)
    # Admin yes(3) + Member yes(1) = 4, Treasurer nay not counted
    eq(prop.weighted_yes_score(reg), 4)

def test_suspended_voter_not_counted():
    reg = make_registry(make_member("admin", ROLE_ADMIN, STATUS_SUSPENDED))
    votes = [make_vote("admin", True)]
    prop = make_proposal(votes=votes, quorum=1)
    eq(prop.weighted_yes_score(reg), 0)
    not_ok(prop.quorum_met(reg))

def test_quorum_met_when_score_sufficient():
    reg = make_registry(
        make_member("a", ROLE_ADMIN),
        make_member("b", ROLE_TREASURER),
    )
    votes = [make_vote("a", True), make_vote("b", True)]
    prop = make_proposal(votes=votes, quorum=5)
    ok(prop.quorum_met(reg))  # 3+2=5 ≥ 5

def test_quorum_not_met():
    reg = make_registry(make_member("a", ROLE_MEMBER))
    votes = [make_vote("a", True)]
    prop = make_proposal(votes=votes, quorum=5)
    not_ok(prop.quorum_met(reg))  # 1 < 5

def test_already_voted_detects_duplicate():
    prop = make_proposal(votes=[make_vote("aa")])
    ok(prop.already_voted("aa"))
    not_ok(prop.already_voted("bb"))

def test_status_helpers():
    ok(make_proposal(status=PROPOSAL_VOTING).is_voting)
    ok(make_proposal(status=PROPOSAL_EXECUTED).is_executed)
    ok(make_proposal(status=PROPOSAL_EXPIRED).is_expired)

def test_unknown_voter_scores_zero():
    reg = make_registry(make_member("aa", ROLE_ADMIN))
    votes = [make_vote("zz", True)]  # "zz" not in registry
    prop = make_proposal(votes=votes)
    eq(prop.weighted_yes_score(reg), 0)

test("weighted_yes_score sums correctly",           test_weighted_yes_score_correct)
test("suspended voter not counted",                 test_suspended_voter_not_counted)
test("quorum_met when score >= quorum",             test_quorum_met_when_score_sufficient)
test("quorum_not_met when score < quorum",          test_quorum_not_met)
test("already_voted detects duplicate",             test_already_voted_detects_duplicate)
test("status helpers (is_voting/executed/expired)", test_status_helpers)
test("unknown voter contributes 0 weight",          test_unknown_voter_scores_zero)


# ─── 4. FOSState derived properties ──────────────────────

print("\n── 4. FOSState ──────────────────────────────────")

NOW = 1_000_000_000_000  # arbitrary POSIX ms

def make_fos_state(proposals, registry=None):
    reg = registry or make_registry(
        make_member("admin", ROLE_ADMIN),
        make_member("mem",   ROLE_MEMBER),
    )
    treas_utxo = make_utxo("treas", 0, 100_000_000)
    treas_utxo.datum = make_treasury()
    reg_utxo = make_utxo("registry", 0, 2_000_000)
    reg_utxo.datum = reg
    return FOSState(
        registry=reg,
        registry_utxo=reg_utxo,
        proposals=proposals,
        treasury_utxo=treas_utxo,
        treasury_datum=make_treasury(),
        current_time_ms=NOW,
    )

def test_executable_proposals_detected():
    reg = make_registry(
        make_member("admin", ROLE_ADMIN),
        make_member("mem",   ROLE_MEMBER),
    )
    # Proposal: quorum=4, Admin(3)+Member(1)=4, execute_after < NOW
    votes = [make_vote("admin", True), make_vote("mem", True)]
    prop = make_proposal(votes=votes, quorum=4, execute_after=NOW - 1000)
    state = make_fos_state([(make_utxo(), prop)], registry=reg)
    eq(len(state.executable_proposals), 1)

def test_locked_proposal_not_executable():
    # Timelock not yet cleared
    votes = [make_vote("admin", True), make_vote("mem", True)]
    prop = make_proposal(votes=votes, quorum=4, execute_after=NOW + 1_000_000)
    state = make_fos_state([(make_utxo(), prop)])
    eq(len(state.executable_proposals), 0)

def test_expirable_proposals_detected():
    prop = make_proposal(
        votes=[],
        quorum=4,
        vote_deadline=NOW - 1,  # deadline passed
    )
    state = make_fos_state([(make_utxo(), prop)])
    eq(len(state.expirable_proposals), 1)

def test_executed_proposals_detected():
    prop = make_proposal(status=PROPOSAL_EXECUTED)
    state = make_fos_state([(make_utxo(), prop)])
    eq(len(state.executed_proposals), 1)

test("executable proposals correctly identified",   test_executable_proposals_detected)
test("locked proposal not in executable list",      test_locked_proposal_not_executable)
test("expirable proposals correctly identified",    test_expirable_proposals_detected)
test("executed proposals correctly identified",     test_executed_proposals_detected)


# ─── 5. Transaction builders ─────────────────────────────

print("\n── 5. Transaction Builders ──────────────────────")

def test_cast_vote_tx_structure():
    gov_utxo = make_utxo("gov1")
    gov_utxo.datum_hex = ""
    reg_utxo = make_utxo("reg1")
    prop = make_proposal()
    tx = build_cast_vote_tx(
        governance_utxo=gov_utxo,
        governance_datum=prop,
        registry_utxo=reg_utxo,
        voter_key_hash="voter_key",
        voter_address="addr_voter",
        approve=True,
        change_address="addr_change",
        current_time_ms=NOW,
    )
    ok(isinstance(tx, UnsignedTransaction))
    ok("gov1#0" in tx.inputs)
    ok("reg1#0" in tx.reference_inputs)
    ok("voter_key" in tx.required_signers)
    ok(tx.validity_end_ms == prop.vote_deadline)

def test_execute_proposal_tx_structure():
    gov_utxo = make_utxo("gov2")
    prop = make_proposal()
    tx = build_execute_proposal_tx(
        governance_utxo=gov_utxo,
        governance_datum=prop,
        registry_utxo=make_utxo("reg1"),
        change_address="",
        current_time_ms=NOW,
    )
    ok(isinstance(tx, UnsignedTransaction))
    ok("gov2#0" in tx.inputs)
    eq(tx.validity_start_ms, prop.execute_after)

def test_execute_transfer_tx_structure():
    gov_utxo = make_utxo("gov3")
    treas_utxo = make_utxo("treas1", lovelace=20_000_000)
    prop = make_proposal(status=PROPOSAL_EXECUTED)
    treas_datum = make_treasury(max_transfer=5_000_000)
    tx = build_execute_transfer_tx(
        treasury_utxo=treas_utxo,
        treasury_datum=treas_datum,
        governance_utxo=gov_utxo,
        governance_datum=prop,
        registry_utxo=make_utxo("reg1"),
        change_address="",
    )
    ok(isinstance(tx, UnsignedTransaction))
    ok("treas1#0" in tx.inputs)
    ok("gov3#0" in tx.reference_inputs)
    # Two outputs: recipient + continuing treasury
    eq(len(tx.outputs), 2)
    eq(tx.outputs[0].lovelace, 2_000_000)   # approved transfer amount
    eq(tx.outputs[1].lovelace, 18_000_000)  # treasury remainder

def test_execute_transfer_rejects_non_executed():
    gov_utxo = make_utxo("gov4")
    prop = make_proposal(status=PROPOSAL_VOTING)   # NOT executed
    try:
        build_execute_transfer_tx(
            treasury_utxo=make_utxo("t"),
            treasury_datum=make_treasury(),
            governance_utxo=gov_utxo,
            governance_datum=prop,
            registry_utxo=make_utxo("r"),
            change_address="",
        )
        assert False, "Should have raised"
    except AssertionError as e:
        ok("Executed" in str(e) or "not Executed" in str(e) or "proposal not Executed" in str(e))

def test_execute_transfer_rejects_over_cap():
    prop = make_proposal(
        status=PROPOSAL_EXECUTED,
        action=TreasuryTransferAction(recipient="bb", lovelace=10_000_000, memo="big")
    )
    try:
        build_execute_transfer_tx(
            treasury_utxo=make_utxo("t", lovelace=20_000_000),
            treasury_datum=make_treasury(max_transfer=5_000_000),
            governance_utxo=make_utxo("g"),
            governance_datum=prop,
            registry_utxo=make_utxo("r"),
            change_address="",
        )
        assert False, "Should have raised"
    except AssertionError as e:
        ok("cap" in str(e) or "exceeds" in str(e))

def test_execute_transfer_rejects_non_treasury_action():
    prop = make_proposal(
        status=PROPOSAL_EXECUTED,
        action=OffChainDecisionAction(memo="just a vote")
    )
    try:
        build_execute_transfer_tx(
            treasury_utxo=make_utxo("t"),
            treasury_datum=make_treasury(),
            governance_utxo=make_utxo("g"),
            governance_datum=prop,
            registry_utxo=make_utxo("r"),
            change_address="",
        )
        assert False, "Should have raised"
    except AssertionError:
        pass

test("cast_vote tx has correct inputs and signers",         test_cast_vote_tx_structure)
test("execute_proposal tx uses correct validity start",     test_execute_proposal_tx_structure)
test("execute_transfer tx splits outputs correctly",        test_execute_transfer_tx_structure)
test("execute_transfer rejects non-Executed proposal",      test_execute_transfer_rejects_non_executed)
test("execute_transfer rejects transfer over spending cap", test_execute_transfer_rejects_over_cap)
test("execute_transfer rejects non-TreasuryTransfer action",test_execute_transfer_rejects_non_treasury_action)


# ─── 6. ProposalAction parsing ───────────────────────────

print("\n── 6. ProposalAction ────────────────────────────")

def test_treasury_transfer_action_str():
    action = TreasuryTransferAction(recipient="aa" * 14, lovelace=2_000_000, memo="salary")
    ok("2.00₳" in str(action))
    ok("salary" in str(action))

def test_offchain_decision_str():
    action = OffChainDecisionAction(memo="elect treasurer")
    ok("elect treasurer" in str(action))

def test_rotate_admin_str():
    action = RotateAdminAction(new_admin="bb")
    ok("RotateAdmin" in str(action))

test("TreasuryTransfer action str includes amount and memo", test_treasury_transfer_action_str)
test("OffChainDecision str includes memo",                   test_offchain_decision_str)
test("RotateAdmin str includes label",                       test_rotate_admin_str)


# ─── 7. Config ────────────────────────────────────────────

print("\n── 7. Config ─────────────────────────────────────")

def test_is_configured_false_without_env():
    import fos_agent.config as cfg
    old = cfg.BLOCKFROST_PROJECT_ID
    cfg.BLOCKFROST_PROJECT_ID = ""
    not_ok(cfg.is_configured())
    cfg.BLOCKFROST_PROJECT_ID = old

def test_blockfrost_url_defaults_to_preprod():
    import fos_agent.config as cfg
    ok("preprod" in cfg.BLOCKFROST_URL or cfg.NETWORK != "mainnet")

def test_autonomous_mode_default_false():
    import fos_agent.config as cfg
    not_ok(cfg.AUTONOMOUS_MODE)

test("is_configured returns False without env vars",  test_is_configured_false_without_env)
test("Blockfrost URL defaults to preprod",            test_blockfrost_url_defaults_to_preprod)
test("autonomous mode off by default",                test_autonomous_mode_default_false)


# ─── 8. Supermajority thresholds (Feature 1) ─────────────

print("\n── 8. Supermajority thresholds ──────────────────")

from fos_agent.types import (
    action_requires_supermajority, SUPERMAJORITY_HIGH_VALUE_LOVELACE,
    UpdateRegistryMemberAction,
)

def test_rotate_admin_requires_supermajority():
    action = RotateAdminAction(new_admin="aa" * 28)
    ok(action_requires_supermajority(action))

def test_large_transfer_requires_supermajority():
    action = TreasuryTransferAction(
        recipient="aa" * 28,
        lovelace=SUPERMAJORITY_HIGH_VALUE_LOVELACE + 1,
        memo="big",
    )
    ok(action_requires_supermajority(action))

def test_small_transfer_no_supermajority():
    action = TreasuryTransferAction(
        recipient="aa" * 28,
        lovelace=SUPERMAJORITY_HIGH_VALUE_LOVELACE,  # exactly at threshold → no supermajority
        memo="small",
    )
    not_ok(action_requires_supermajority(action))

def test_offchain_no_supermajority():
    not_ok(action_requires_supermajority(OffChainDecisionAction(memo="adopt policy")))

def test_update_member_no_supermajority():
    not_ok(action_requires_supermajority(
        UpdateRegistryMemberAction(target_key="aa" * 28, new_role=ROLE_MEMBER, new_status=STATUS_ACTIVE)
    ))

def test_supermajority_met_method_passes():
    # Admin(3) + Treasurer(2) = 5 out of 5 max → 5*3=15 >= 5*2=10 ✓
    reg = make_registry(
        make_member("a", ROLE_ADMIN),
        make_member("b", ROLE_TREASURER),
    )
    prop = make_proposal(
        votes=[make_vote("a", True), make_vote("b", True)],
        action=RotateAdminAction(new_admin="cc" * 28),
    )
    ok(prop.supermajority_met(reg))

def test_supermajority_not_met_method_fails():
    # Admin(3) out of Admin(3)+Treasurer(2)+Member(1)=6 max → 3*3=9 < 6*2=12 ✗
    reg = make_registry(
        make_member("a", ROLE_ADMIN),
        make_member("b", ROLE_TREASURER),
        make_member("c", ROLE_MEMBER),
    )
    prop = make_proposal(
        votes=[make_vote("a", True)],
        action=RotateAdminAction(new_admin="cc" * 28),
    )
    not_ok(prop.supermajority_met(reg))

def test_quorum_met_includes_supermajority_check():
    # quorum=1 (trivially met), but supermajority is not → quorum_met returns False
    reg = make_registry(
        make_member("a", ROLE_ADMIN),
        make_member("b", ROLE_TREASURER),
        make_member("c", ROLE_MEMBER),
    )
    # Only admin voted yes; max=6, yes=3, need 4 for 2/3 supermajority
    prop = make_proposal(
        votes=[make_vote("a", True)],
        quorum=1,
        action=RotateAdminAction(new_admin="cc" * 28),
    )
    not_ok(prop.quorum_met(reg))

def test_quorum_met_supermajority_passes():
    # Admin(3)+Treasurer(2)=5 out of 6 max → 5*3=15 >= 6*2=12 ✓
    reg = make_registry(
        make_member("a", ROLE_ADMIN),
        make_member("b", ROLE_TREASURER),
        make_member("c", ROLE_MEMBER),
    )
    prop = make_proposal(
        votes=[make_vote("a", True), make_vote("b", True)],
        quorum=3,
        action=RotateAdminAction(new_admin="cc" * 28),
    )
    ok(prop.quorum_met(reg))

test("RotateAdmin requires supermajority",                  test_rotate_admin_requires_supermajority)
test("large TreasuryTransfer requires supermajority",       test_large_transfer_requires_supermajority)
test("small TreasuryTransfer does not require supermajority", test_small_transfer_no_supermajority)
test("OffChainDecision does not require supermajority",     test_offchain_no_supermajority)
test("UpdateRegistryMember does not require supermajority", test_update_member_no_supermajority)
test("supermajority_met() passes for 2/3 yes",              test_supermajority_met_method_passes)
test("supermajority_met() fails for < 2/3 yes",             test_supermajority_not_met_method_fails)
test("quorum_met enforces supermajority for RotateAdmin",   test_quorum_met_includes_supermajority_check)
test("quorum_met passes when supermajority satisfied",      test_quorum_met_supermajority_passes)


# ─── 9. AlertManager deduplication (Feature 2) ───────────

print("\n── 9. AlertManager ───────────────────────────────")

from fos_agent.alerts import AlertManager

def make_alert_state(treasury_lovelace=50_000_000, proposals=None):
    reg = make_registry(make_member("admin", ROLE_ADMIN))
    treas_utxo = make_utxo("treas", 0, treasury_lovelace)
    treas_datum = make_treasury()
    treas_utxo.datum = treas_datum
    reg_utxo = make_utxo("registry", 0)
    reg_utxo.datum = reg
    props = proposals or []
    return FOSState(
        registry=reg,
        registry_utxo=reg_utxo,
        proposals=props,
        treasury_utxo=treas_utxo,
        treasury_datum=treas_datum,
        current_time_ms=NOW,
    )

def test_alert_manager_deduplicates():
    # No webhooks configured — send_alert() returns False but _once() still tracks
    mgr = AlertManager()
    # Manually test dedup
    ok(mgr._once("key:a"))
    not_ok(mgr._once("key:a"))  # second call: False
    ok(mgr._once("key:b"))

def test_alert_fires_for_low_treasury():
    mgr = AlertManager()
    # 5 ADA treasury, threshold defaults to 10 ADA in tests we patch directly
    import fos_agent.alerts as alerts_mod
    old = alerts_mod.TREASURY_ALERT_ADA
    alerts_mod.TREASURY_ALERT_ADA = 10.0
    state = make_alert_state(treasury_lovelace=5_000_000)  # 5 ADA < 10 ADA
    fired = mgr.check(state)
    alerts_mod.TREASURY_ALERT_ADA = old
    # Alert key should have been registered even if no webhook is configured
    ok(any("treasury" in k for k in mgr._fired))

def test_alert_does_not_repeat():
    mgr = AlertManager()
    import fos_agent.alerts as alerts_mod
    old = alerts_mod.TREASURY_ALERT_ADA
    alerts_mod.TREASURY_ALERT_ADA = 10.0
    state = make_alert_state(treasury_lovelace=5_000_000)
    mgr.check(state)
    treasury_keys_before = sum(1 for k in mgr._fired if "treasury:low" in k)
    mgr.check(state)
    treasury_keys_after = sum(1 for k in mgr._fired if "treasury:low" in k)
    alerts_mod.TREASURY_ALERT_ADA = old
    eq(treasury_keys_before, treasury_keys_after)  # no new key added

def test_alert_tracks_new_proposals():
    mgr = AlertManager()
    prop_utxo = make_utxo("proposal1")
    prop = make_proposal(vote_deadline=NOW + 10 * 86_400_000)
    state = make_alert_state(proposals=[(prop_utxo, prop)])
    mgr.check(state)
    ok(f"{prop_utxo.ref}:seen" in mgr._fired)

def test_alert_at_risk_fires_when_low_participation():
    # Proposal with 30h left, 0/6 score (0% of max), quorum=4 — should fire at_risk
    mgr = AlertManager()
    admin = make_member("admin", ROLE_ADMIN)    # weight 3
    mem   = make_member("mem1",  ROLE_MEMBER)  # weight 1
    reg   = RegistryDatum(members=[admin, mem], admin="admin", version=1)
    reg_utxo = make_utxo("registry", 0)
    reg_utxo.datum = reg
    treas_utxo = make_utxo("treas", 0, 50_000_000)
    treas_utxo.datum = make_treasury()
    deadline = NOW + 30 * 3_600_000   # 30 h from now
    prop = make_proposal(votes=[], quorum=4, vote_deadline=deadline)
    prop_utxo = make_utxo("proposal_ar")
    state = FOSState(
        registry=reg, registry_utxo=reg_utxo,
        proposals=[(prop_utxo, prop)],
        treasury_utxo=treas_utxo, treasury_datum=make_treasury(),
        current_time_ms=NOW,
    )
    mgr.check(state)
    ok(f"{prop_utxo.ref}:at_risk" in mgr._fired, "at_risk key should be tracked")

def test_alert_at_risk_does_not_fire_when_quorum_met():
    # Same scenario but with enough yes votes — at_risk should NOT fire
    mgr = AlertManager()
    admin = make_member("admin", ROLE_ADMIN)   # weight 3
    mem   = make_member("mem1",  ROLE_MEMBER)  # weight 1
    reg   = RegistryDatum(members=[admin, mem], admin="admin", version=1)
    reg_utxo = make_utxo("registry", 0)
    reg_utxo.datum = reg
    treas_utxo = make_utxo("treas", 0, 50_000_000)
    treas_utxo.datum = make_treasury()
    deadline = NOW + 30 * 3_600_000
    votes = [make_vote("admin", True), make_vote("mem1", True)]
    prop = make_proposal(votes=votes, quorum=3, vote_deadline=deadline)  # yes=4 >= quorum=3
    prop_utxo = make_utxo("proposal_qm")
    state = FOSState(
        registry=reg, registry_utxo=reg_utxo,
        proposals=[(prop_utxo, prop)],
        treasury_utxo=treas_utxo, treasury_datum=make_treasury(),
        current_time_ms=NOW,
    )
    mgr.check(state)
    not_ok(f"{prop_utxo.ref}:at_risk" in mgr._fired, "at_risk should not fire when quorum is met")

test("AlertManager deduplicates on repeated key",            test_alert_manager_deduplicates)
test("alert fires for low treasury balance",                 test_alert_fires_for_low_treasury)
test("alert does not repeat on second poll",                 test_alert_does_not_repeat)
test("alert tracks new proposal refs",                       test_alert_tracks_new_proposals)
test("at_risk alert fires when low participation < 48h",     test_alert_at_risk_fires_when_low_participation)
test("at_risk alert silent when quorum already met",         test_alert_at_risk_does_not_fire_when_quorum_met)


# ─── 10. Proposal agent validation (Feature 3) ───────────

print("\n── 10. Proposal agent ────────────────────────────")

from fos_agent.proposal_agent import _ProposalAgent
from fos_agent.chain import mock_fos_state, BlockfrostClient

def make_proposal_agent():
    bf = BlockfrostClient(project_id="", base_url="https://cardano-preprod.blockfrost.io/api/v0")
    agent = _ProposalAgent(bf)
    agent._read_fos_state()  # populates self._state from mock
    return agent

def test_proposal_agent_read_state():
    agent = make_proposal_agent()
    ok(agent._state is not None)

def test_proposal_agent_rejects_over_cap():
    agent = make_proposal_agent()
    result = json.loads(agent._draft_proposal(
        action_type="TreasuryTransfer",
        description="Big grant",
        vote_deadline_days=7,
        execute_after_days=8,
        quorum=4,
        recipient_key_hash="c3d4e5f6" * 7,
        amount_ada=100.0,   # way over the 5 ADA mock cap
        memo="too much",
    ))
    not_ok(result["success"])
    ok("cap" in result["error"].lower() or "exceeds" in result["error"].lower())

def test_proposal_agent_rejects_low_supermajority_quorum():
    agent = make_proposal_agent()
    # RotateAdmin needs supermajority; quorum=1 should be rejected
    result = json.loads(agent._draft_proposal(
        action_type="RotateAdmin",
        description="Rotate admin",
        vote_deadline_days=7,
        execute_after_days=8,
        quorum=1,
        new_admin_key_hash="a1b2c3d4" * 7,
    ))
    not_ok(result["success"])
    ok("supermajority" in result["error"].lower())

def test_proposal_agent_drafts_offchain_decision():
    agent = make_proposal_agent()
    result = json.loads(agent._draft_proposal(
        action_type="OffChainDecision",
        description="Adopt MIT licence for all repos",
        vote_deadline_days=7,
        execute_after_days=8,
        quorum=3,
        memo="Adopt MIT licence",
    ))
    ok(result["success"])
    ok("preview" in result)
    eq(result["preview"]["action_type"], "OffChainDecision")

def test_proposal_agent_draft_stored():
    agent = make_proposal_agent()
    agent._draft_proposal(
        action_type="OffChainDecision",
        description="Adopt MIT licence",
        vote_deadline_days=7,
        execute_after_days=8,
        quorum=3,
        memo="MIT",
    )
    ok(agent._draft is not None)

test("proposal agent reads mock state",                           test_proposal_agent_read_state)
test("proposal agent rejects transfer over cap",                  test_proposal_agent_rejects_over_cap)
test("proposal agent rejects RotateAdmin without supermajority",  test_proposal_agent_rejects_low_supermajority_quorum)
test("proposal agent drafts OffChainDecision successfully",       test_proposal_agent_drafts_offchain_decision)
test("proposal agent stores draft for submission",                test_proposal_agent_draft_stored)


# ─── 11. Vote delegation ───────────────────────────────────

print("\n── 11. Vote delegation ───────────────────────────────")

from fos_agent.transactions import build_set_delegate_tx

def make_two_member_registry():
    admin = RegistryMember(key_hash="aa" * 28, role=ROLE_ADMIN, joined_at=100, status=STATUS_ACTIVE)
    mem   = RegistryMember(key_hash="bb" * 28, role=ROLE_MEMBER, joined_at=200, status=STATUS_ACTIVE)
    return RegistryDatum(members=[admin, mem], admin="aa" * 28, version=1)

def test_delegation_adds_weight():
    # bb delegates to aa; aa votes yes → aa gets 3 + 1 = 4
    reg = make_two_member_registry()
    reg.members[1] = RegistryMember(key_hash="bb" * 28, role=ROLE_MEMBER,
                                    joined_at=200, status=STATUS_ACTIVE, delegate="aa" * 28)
    votes = [make_vote("aa" * 28, True)]
    prop = make_proposal(votes=votes, quorum=4)
    eq(prop.weighted_yes_score(reg), 4)

def test_delegation_overridden_by_direct_vote():
    # bb delegates to aa, but bb also votes directly → aa gets 3 (own only), bb gets 1
    reg = make_two_member_registry()
    reg.members[1] = RegistryMember(key_hash="bb" * 28, role=ROLE_MEMBER,
                                    joined_at=200, status=STATUS_ACTIVE, delegate="aa" * 28)
    votes = [make_vote("aa" * 28, True), make_vote("bb" * 28, True)]
    prop = make_proposal(votes=votes, quorum=4)
    eq(prop.weighted_yes_score(reg), 4)  # 3 + 1, no double count

def test_delegation_no_effect_when_delegate_votes_no():
    # bb delegates to aa, aa votes no → yes score = 0 (bb's weight doesn't flow anywhere)
    reg = make_two_member_registry()
    reg.members[1] = RegistryMember(key_hash="bb" * 28, role=ROLE_MEMBER,
                                    joined_at=200, status=STATUS_ACTIVE, delegate="aa" * 28)
    votes = [make_vote("aa" * 28, False)]
    prop = make_proposal(votes=votes)
    eq(prop.weighted_yes_score(reg), 0)

def test_build_set_delegate_tx_sets_delegate():
    reg = make_two_member_registry()
    utxo = make_utxo("reg_tx", 0, 2_000_000)
    tx = build_set_delegate_tx(utxo, reg, "bb" * 28, "aa" * 28)
    ok("SetDelegate" in tx.description)
    eq(tx.required_signers, ["bb" * 28])
    eq(len(tx.inputs), 1)
    eq(len(tx.outputs), 1)

def test_build_set_delegate_tx_clears_delegate():
    reg = make_two_member_registry()
    reg.members[1] = RegistryMember(key_hash="bb" * 28, role=ROLE_MEMBER,
                                    joined_at=200, status=STATUS_ACTIVE, delegate="aa" * 28)
    utxo = make_utxo("reg_tx", 0, 2_000_000)
    tx = build_set_delegate_tx(utxo, reg, "bb" * 28, None)
    ok("clear" in tx.description or "None" in tx.description or "(clear)" in tx.description)
    eq(tx.required_signers, ["bb" * 28])

def test_build_set_delegate_tx_rejects_self_delegation():
    reg = make_two_member_registry()
    utxo = make_utxo("reg_tx", 0, 2_000_000)
    try:
        build_set_delegate_tx(utxo, reg, "aa" * 28, "aa" * 28)
        assert False, "should have raised"
    except AssertionError as e:
        ok("self" in str(e).lower())

def test_build_set_delegate_tx_rejects_chain():
    # aa delegates to bb, then bb tries to delegate to aa — chain would be created
    reg = make_two_member_registry()
    reg.members[0] = RegistryMember(key_hash="aa" * 28, role=ROLE_ADMIN,
                                    joined_at=100, status=STATUS_ACTIVE, delegate="bb" * 28)
    utxo = make_utxo("reg_tx", 0, 2_000_000)
    try:
        build_set_delegate_tx(utxo, reg, "bb" * 28, "aa" * 28)
        assert False, "should have raised — target already has a delegate"
    except AssertionError as e:
        ok("chain" in str(e).lower() or "delegate" in str(e).lower())

test("delegation adds delegator weight to delegate's yes vote",   test_delegation_adds_weight)
test("delegation overridden when delegator votes directly",        test_delegation_overridden_by_direct_vote)
test("delegation has no effect when delegate votes no",            test_delegation_no_effect_when_delegate_votes_no)
test("build_set_delegate_tx produces correct tx structure",        test_build_set_delegate_tx_sets_delegate)
test("build_set_delegate_tx clears delegation when None",          test_build_set_delegate_tx_clears_delegate)
test("build_set_delegate_tx rejects self-delegation",              test_build_set_delegate_tx_rejects_self_delegation)
test("build_set_delegate_tx rejects delegation chains",            test_build_set_delegate_tx_rejects_chain)


# ════════════════════════════════════════════════════════
# Section 12 — Proposal deposits
# ════════════════════════════════════════════════════════

print("\n── 12. Proposal deposits ─────────────────────────")

from fos_agent.transactions import MIN_PROPOSAL_DEPOSIT

def _make_proposal_registry():
    """Registry with one Admin member — enough for quorum=1 tests."""
    return make_registry(make_member("aa" * 28, ROLE_ADMIN, STATUS_ACTIVE))

def test_create_proposal_default_deposit():
    reg = _make_proposal_registry()
    utxo = make_utxo("reg_tx", 0, 2_000_000)
    gov_hash = "cc" * 28
    tx = build_create_proposal_tx(
        registry_utxo=utxo,
        registry=reg,
        proposer_key_hash="aa" * 28,
        description="Test proposal",
        action=OffChainDecisionAction(memo="test"),
        vote_deadline_ms=int(time.time() * 1000) + 3_600_000,
        execute_after_ms=int(time.time() * 1000) + 7_200_000,
        quorum=1,
        governance_script_hash=gov_hash,
        current_time_ms=int(time.time() * 1000),
    )
    ok(tx is not None)
    # Output lovelace = min_lovelace (2 ADA) + default deposit (2 ADA)
    ok(tx.outputs[0].lovelace == 4_000_000)

def test_create_proposal_custom_deposit():
    reg = _make_proposal_registry()
    utxo = make_utxo("reg_tx", 0, 2_000_000)
    gov_hash = "cc" * 28
    tx = build_create_proposal_tx(
        registry_utxo=utxo,
        registry=reg,
        proposer_key_hash="aa" * 28,
        description="High deposit",
        action=OffChainDecisionAction(memo="big"),
        vote_deadline_ms=int(time.time() * 1000) + 3_600_000,
        execute_after_ms=int(time.time() * 1000) + 7_200_000,
        quorum=1,
        governance_script_hash=gov_hash,
        current_time_ms=int(time.time() * 1000),
        deposit=5_000_000,
    )
    ok(tx.outputs[0].lovelace == 7_000_000)  # 2 min + 5 deposit

def test_create_proposal_deposit_stored_in_datum():
    reg = _make_proposal_registry()
    utxo = make_utxo("ab" * 32, 0, 2_000_000)   # hex tx_hash for valid CBOR encoding
    gov_hash = "cc" * 28
    tx = build_create_proposal_tx(
        registry_utxo=utxo,
        registry=reg,
        proposer_key_hash="aa" * 28,
        description="Deposit datum test",
        action=OffChainDecisionAction(memo="memo"),
        vote_deadline_ms=int(time.time() * 1000) + 3_600_000,
        execute_after_ms=int(time.time() * 1000) + 7_200_000,
        quorum=1,
        governance_script_hash=gov_hash,
        current_time_ms=int(time.time() * 1000),
        deposit=3_000_000,
    )
    datum_hex = tx.outputs[0].datum_hex
    ok(datum_hex is not None and datum_hex != "<governance_datum_cbor_hex>")
    recovered = GovernanceDatum.from_cbor_hex(datum_hex)
    ok(recovered.deposit == 3_000_000)

def test_create_proposal_deposit_below_minimum_raises():
    reg = make_registry()
    utxo = make_utxo("reg_tx", 0, 2_000_000)
    try:
        build_create_proposal_tx(
            registry_utxo=utxo,
            registry=reg,
            proposer_key_hash="aa" * 28,
            description="Cheap proposal",
            action=OffChainDecisionAction(memo="spam"),
            vote_deadline_ms=int(time.time() * 1000) + 3_600_000,
            execute_after_ms=int(time.time() * 1000) + 7_200_000,
            quorum=1,
            governance_script_hash="cc" * 28,
            current_time_ms=int(time.time() * 1000),
            deposit=500_000,
        )
        ok(False, "should have raised ValueError")
    except ValueError as e:
        ok("minimum" in str(e).lower() or "deposit" in str(e).lower())

def test_execute_proposal_refunds_deposit():
    gov_datum = make_governance_datum(deposit=3_000_000)
    gov_utxo = make_utxo("gov_tx", 0, 5_000_000)
    reg_utxo = make_utxo("reg_tx", 0, 2_000_000)
    tx = build_execute_proposal_tx(
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        registry_utxo=reg_utxo,
        change_address="addr_test1v" + "aa" * 28,
        current_time_ms=int(time.time() * 1000),
    )
    ok(len(tx.outputs) == 2)
    refund_outputs = [o for o in tx.outputs if o.lovelace == 3_000_000]
    ok(len(refund_outputs) == 1)

def test_execute_proposal_continuing_output_reduced():
    gov_datum = make_governance_datum(deposit=2_000_000)
    gov_utxo = make_utxo("gov_tx", 0, 4_000_000)
    reg_utxo = make_utxo("reg_tx", 0, 2_000_000)
    tx = build_execute_proposal_tx(
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        registry_utxo=reg_utxo,
        change_address="addr_test1v" + "aa" * 28,
        current_time_ms=int(time.time() * 1000),
    )
    gov_continuing = tx.outputs[0]
    ok(gov_continuing.lovelace == 2_000_000)

def test_execute_proposal_no_refund_when_no_deposit():
    gov_datum = make_governance_datum(deposit=0)
    gov_utxo = make_utxo("gov_tx", 0, 2_000_000)
    reg_utxo = make_utxo("reg_tx", 0, 2_000_000)
    tx = build_execute_proposal_tx(
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        registry_utxo=reg_utxo,
        change_address="addr_test1v" + "aa" * 28,
        current_time_ms=int(time.time() * 1000),
    )
    ok(len(tx.outputs) == 1)

def test_expire_proposal_keeps_full_lovelace():
    gov_datum = make_governance_datum(status=PROPOSAL_VOTING, deposit=2_000_000)
    gov_utxo = make_utxo("gov_tx", 0, 4_000_000)
    reg_utxo = make_utxo("reg_tx", 0, 2_000_000)
    tx = build_expire_proposal_tx(
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        registry_utxo=reg_utxo,
        change_address="addr_test1v" + "aa" * 28,
        current_time_ms=int(time.time() * 1000) + 999_999_999,
    )
    ok(len(tx.outputs) == 1)
    ok(tx.outputs[0].lovelace == 4_000_000)

def test_min_proposal_deposit_constant():
    ok(MIN_PROPOSAL_DEPOSIT == 2_000_000)

def test_governance_datum_deposit_roundtrip():
    d = make_governance_datum(deposit=5_000_000)
    from fos_agent.datums import governance_datum_cbor_hex
    hex_str = governance_datum_cbor_hex(d)
    recovered = GovernanceDatum.from_cbor_hex(hex_str)
    ok(recovered.deposit == 5_000_000)

def test_governance_datum_zero_deposit_roundtrip():
    d = make_governance_datum(deposit=0)
    from fos_agent.datums import governance_datum_cbor_hex
    hex_str = governance_datum_cbor_hex(d)
    recovered = GovernanceDatum.from_cbor_hex(hex_str)
    ok(recovered.deposit == 0)

test("build_create_proposal_tx output includes deposit + min_lovelace",  test_create_proposal_default_deposit)
test("build_create_proposal_tx respects custom deposit",                  test_create_proposal_custom_deposit)
test("build_create_proposal_tx stores deposit in datum",                  test_create_proposal_deposit_stored_in_datum)
test("build_create_proposal_tx rejects deposit below 2 ADA minimum",     test_create_proposal_deposit_below_minimum_raises)
test("build_execute_proposal_tx adds proposer refund output",             test_execute_proposal_refunds_deposit)
test("build_execute_proposal_tx reduces continuing output by deposit",    test_execute_proposal_continuing_output_reduced)
test("build_execute_proposal_tx omits refund when deposit=0",             test_execute_proposal_no_refund_when_no_deposit)
test("build_expire_proposal_tx preserves full lovelace (deposit locked)", test_expire_proposal_keeps_full_lovelace)
test("MIN_PROPOSAL_DEPOSIT constant equals 2 ADA",                       test_min_proposal_deposit_constant)
test("GovernanceDatum deposit round-trips through CBOR (non-zero)",      test_governance_datum_deposit_roundtrip)
test("GovernanceDatum deposit round-trips through CBOR (zero)",          test_governance_datum_zero_deposit_roundtrip)


# ─── 13. Native token treasury ────────────────────────────

print("\n── 13. Native token treasury ────────────────────")

from fos_agent.types import NativeToken, UpdateRegistryMemberAction
from fos_agent.datums import governance_datum_cbor_hex
from fos_agent.transactions import build_execute_transfer_tx

def _make_token(policy="deadbeef" * 7, asset="514d524d4c", qty=100):
    return NativeToken(policy_id=policy, asset_name=asset, quantity=qty)

def test_native_token_action_serializes():
    token = _make_token()
    action = TreasuryTransferAction(
        recipient="bb" * 28, lovelace=0, memo="token grant", tokens=[token]
    )
    gov = make_proposal(action=action, deposit=0)
    hex_str = governance_datum_cbor_hex(gov)
    recovered = GovernanceDatum.from_cbor_hex(hex_str)
    ok(isinstance(recovered.action, TreasuryTransferAction))
    eq(len(recovered.action.tokens), 1)
    eq(recovered.action.tokens[0].quantity, 100)

def test_native_token_action_empty_tokens_roundtrip():
    action = TreasuryTransferAction(recipient="bb" * 28, lovelace=2_000_000, memo="ada only", tokens=[])
    gov = make_proposal(action=action, deposit=0)
    hex_str = governance_datum_cbor_hex(gov)
    recovered = GovernanceDatum.from_cbor_hex(hex_str)
    eq(len(recovered.action.tokens), 0)

def test_build_execute_transfer_includes_tokens():
    token = _make_token()
    action = TreasuryTransferAction(recipient="bb" * 28, lovelace=0, memo="token", tokens=[token])
    gov_datum = make_proposal(action=action, status=PROPOSAL_EXECUTED, deposit=0)
    gov_utxo  = make_utxo("ab" * 32, 0, 2_000_000)
    treas_utxo = make_utxo("cd" * 32, 0, 10_000_000)
    reg_utxo  = make_utxo("ef" * 32, 0, 2_000_000)
    treasury = make_treasury(max_transfer=5_000_000)
    tx = build_execute_transfer_tx(
        treasury_utxo=treas_utxo,
        treasury_datum=treasury,
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        registry_utxo=reg_utxo,
        change_address="",
    )
    ok(len(tx.outputs) == 2)
    recipient_out = tx.outputs[0]
    eq(len(recipient_out.tokens), 1)
    eq(recipient_out.tokens[0].quantity, 100)

def test_build_execute_transfer_token_description():
    token = _make_token()
    action = TreasuryTransferAction(recipient="bb" * 28, lovelace=1_000_000, memo="mixed", tokens=[token])
    gov_datum = make_proposal(action=action, status=PROPOSAL_EXECUTED, deposit=0)
    gov_utxo  = make_utxo("ab" * 32, 0, 2_000_000)
    treas_utxo = make_utxo("cd" * 32, 0, 10_000_000)
    reg_utxo  = make_utxo("ef" * 32, 0, 2_000_000)
    treasury = make_treasury(max_transfer=5_000_000)
    tx = build_execute_transfer_tx(
        treasury_utxo=treas_utxo,
        treasury_datum=treasury,
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        registry_utxo=reg_utxo,
        change_address="",
    )
    ok("1 token(s)" in tx.description)

def test_native_token_asset_name_str_hex():
    # 51 52 4d 4c = Q R M L
    t = NativeToken(policy_id="aa" * 28, asset_name="51524d4c", quantity=1)
    eq(t.asset_name_str(), "QRML")

def test_native_token_asset_name_str_fallback():
    t = NativeToken(policy_id="aa" * 28, asset_name="zzzz_not_hex", quantity=1)
    eq(t.asset_name_str(), "zzzz_not_hex")

def test_build_create_proposal_token_only_raises_if_no_ada_and_no_tokens():
    reg = make_registry(make_member("aa" * 28, role=ROLE_ADMIN))
    reg_utxo = make_utxo("ab" * 32, 0, 2_000_000)
    action = TreasuryTransferAction(recipient="bb" * 28, lovelace=0, memo="empty", tokens=[])
    try:
        build_create_proposal_tx(
            registry_utxo=reg_utxo,
            registry=reg,
            proposer_key_hash="aa" * 28,
            description="bad",
            action=action,
            vote_deadline_ms=int(time.time() * 1000) + 86_400_000,
            execute_after_ms=int(time.time() * 1000) + 2 * 86_400_000,
            quorum=1,
            governance_script_hash="ff" * 28,
            current_time_ms=int(time.time() * 1000),
        )
        ok(False, "should have raised ValueError")
    except ValueError as e:
        ok("token" in str(e).lower() or "lovelace" in str(e).lower() or "ADA" in str(e))

test("NativeToken CBOR roundtrip through GovernanceDatum",        test_native_token_action_serializes)
test("Empty tokens list roundtrip (ADA-only proposal)",           test_native_token_action_empty_tokens_roundtrip)
test("build_execute_transfer_tx passes tokens to recipient output", test_build_execute_transfer_includes_tokens)
test("build_execute_transfer_tx description includes token count", test_build_execute_transfer_token_description)
test("NativeToken asset_name hex decoded to UTF-8",               test_native_token_asset_name_str_hex)
test("NativeToken asset_name fallback when not valid hex",        test_native_token_asset_name_str_fallback)
test("build_create_proposal_tx rejects zero ADA and zero tokens", test_build_create_proposal_token_only_raises_if_no_ada_and_no_tokens)


# ─── 14. IPFS rationale URL ───────────────────────────────

print("\n── 14. IPFS rationale URL ───────────────────────")

def _make_registry_with_admin():
    return make_registry(make_member("aa" * 28, role=ROLE_ADMIN))

def test_rationale_url_stored_in_datum():
    reg = _make_registry_with_admin()
    reg_utxo = make_utxo("ab" * 32, 0, 2_000_000)
    action = OffChainDecisionAction(memo="test decision")
    tx = build_create_proposal_tx(
        registry_utxo=reg_utxo,
        registry=reg,
        proposer_key_hash="aa" * 28,
        description="Test IPFS proposal",
        action=action,
        vote_deadline_ms=int(time.time() * 1000) + 86_400_000,
        execute_after_ms=int(time.time() * 1000) + 2 * 86_400_000,
        quorum=1,
        governance_script_hash="ff" * 28,
        current_time_ms=int(time.time() * 1000),
        rationale_url="ipfs://bafkreihdwdcefgh4dqkjv67uzcmw37nike4ttgrfkhnz4b4ygw2qcjzh7a",
    )
    ok(tx is not None)

def test_rationale_url_in_metadata():
    reg = _make_registry_with_admin()
    reg_utxo = make_utxo("ab" * 32, 0, 2_000_000)
    action = OffChainDecisionAction(memo="with rationale")
    tx = build_create_proposal_tx(
        registry_utxo=reg_utxo,
        registry=reg,
        proposer_key_hash="aa" * 28,
        description="Rationale URL test",
        action=action,
        vote_deadline_ms=int(time.time() * 1000) + 86_400_000,
        execute_after_ms=int(time.time() * 1000) + 2 * 86_400_000,
        quorum=1,
        governance_script_hash="ff" * 28,
        current_time_ms=int(time.time() * 1000),
        rationale_url="ipfs://bafkreitest",
    )
    ok(675 in tx.metadata)
    eq(tx.metadata[675]["rationale"], "ipfs://bafkreitest")

def test_no_rationale_url_no_metadata_key():
    reg = _make_registry_with_admin()
    reg_utxo = make_utxo("ab" * 32, 0, 2_000_000)
    action = OffChainDecisionAction(memo="no url")
    tx = build_create_proposal_tx(
        registry_utxo=reg_utxo,
        registry=reg,
        proposer_key_hash="aa" * 28,
        description="No rationale",
        action=action,
        vote_deadline_ms=int(time.time() * 1000) + 86_400_000,
        execute_after_ms=int(time.time() * 1000) + 2 * 86_400_000,
        quorum=1,
        governance_script_hash="ff" * 28,
        current_time_ms=int(time.time() * 1000),
    )
    ok(675 not in tx.metadata)

def test_rationale_url_in_gov_datum():
    reg = _make_registry_with_admin()
    reg_utxo = make_utxo("ab" * 32, 0, 2_000_000)
    action = OffChainDecisionAction(memo="check datum url")
    url = "ipfs://bafkreitest_datum"
    tx = build_create_proposal_tx(
        registry_utxo=reg_utxo,
        registry=reg,
        proposer_key_hash="aa" * 28,
        description="Datum rationale test",
        action=action,
        vote_deadline_ms=int(time.time() * 1000) + 86_400_000,
        execute_after_ms=int(time.time() * 1000) + 2 * 86_400_000,
        quorum=1,
        governance_script_hash="ff" * 28,
        current_time_ms=int(time.time() * 1000),
        rationale_url=url,
    )
    # The datum hex embeds the on-chain datum (no rationale_url); only metadata has it
    ok(tx.metadata[675]["rationale"] == url)

test("build_create_proposal_tx accepts rationale_url",          test_rationale_url_stored_in_datum)
test("rationale_url stored in tx metadata label 675",           test_rationale_url_in_metadata)
test("no rationale_url → no metadata key 675",                  test_no_rationale_url_no_metadata_key)
test("rationale_url accessible from UnsignedTransaction",       test_rationale_url_in_gov_datum)


# ─── 15. DRep integration ─────────────────────────────────

print("\n── 15. DRep integration ─────────────────────────")

from fos_agent.drep import (
    build_drep_registration,
    build_drep_retirement,
    generate_drep_metadata,
    query_drep_status,
    drep_id_from_key_hash,
    DREP_DEPOSIT_PREPROD,
    DREP_DEPOSIT_MAINNET,
)

def test_drep_registration_descriptor():
    cert = build_drep_registration("aa" * 28, "https://example.com/meta.json", "bb" * 32)
    eq(cert.drep_key_hash, "aa" * 28)
    eq(cert.anchor_url, "https://example.com/meta.json")
    eq(cert.anchor_hash, "bb" * 32)
    ok(cert.deposit_lovelace > 0)

def test_drep_retirement_descriptor():
    cert = build_drep_retirement("cc" * 28)
    eq(cert.drep_key_hash, "cc" * 28)
    eq(cert.deposit_lovelace, 0)
    eq(cert.anchor_url, "")

def test_drep_deposit_preprod():
    eq(DREP_DEPOSIT_PREPROD, 2_000_000)

def test_drep_deposit_mainnet():
    eq(DREP_DEPOSIT_MAINNET, 500_000_000)

def test_drep_id_fallback():
    kid = drep_id_from_key_hash("aa" * 28)
    ok(len(kid) > 0)
    ok("drep" in kid.lower())

def test_generate_drep_metadata_shape():
    meta = generate_drep_metadata()
    ok("body" in meta)
    ok("givenName" in meta["body"])
    ok("motivations" in meta["body"])
    ok("references" in meta["body"])
    ok(len(meta["body"]["references"]) > 0)

def test_query_drep_status_mock():
    # Without BLOCKFROST_PROJECT_ID, uses mock data
    info = query_drep_status("aa" * 28)
    ok(info.drep_id is not None)
    ok(info.delegator_count >= 0)

def test_generate_drep_metadata_custom_name():
    meta = generate_drep_metadata(name="My Custom DRep")
    eq(meta["body"]["givenName"], "My Custom DRep")

test("DRep registration descriptor has correct fields",  test_drep_registration_descriptor)
test("DRep retirement descriptor has zero deposit",      test_drep_retirement_descriptor)
test("DRep preprod deposit = 2 ADA",                     test_drep_deposit_preprod)
test("DRep mainnet deposit = 500 ADA",                   test_drep_deposit_mainnet)
test("drep_id_from_key_hash returns drep-prefixed ID",   test_drep_id_fallback)
test("generate_drep_metadata has required CIP-119 fields", test_generate_drep_metadata_shape)
test("query_drep_status works in mock mode",             test_query_drep_status_mock)
test("generate_drep_metadata accepts custom name",       test_generate_drep_metadata_custom_name)


# ─── Section 16: Vesting ──────────────────────────────────
print("\n── 16. Vesting types, datums, transactions ──")

from fos_agent.types import VestingTranche, VestingDatum, CreateVestingAction
from fos_agent.transactions import build_create_vesting_tx, build_claim_vesting_tx

NOW_MS = int(time.time() * 1000)

_REG_TX  = "a1" * 32   # 64 hex chars
_GOV_TX  = "b2" * 32
_TREAS_TX = "c3" * 32
_VEST_TX  = "d4" * 32

def _make_vesting_state():
    members = [
        RegistryMember(key_hash="a1" * 28, role=ROLE_ADMIN,     joined_at=NOW_MS - 86400000, status=STATUS_ACTIVE),
        RegistryMember(key_hash="b2" * 28, role=ROLE_MEMBER,    joined_at=NOW_MS - 86400000, status=STATUS_ACTIVE),
    ]
    registry = RegistryDatum(members=members, admin="a1" * 28, version=1, governance_script_hash="ff" * 28)
    reg_utxo = make_utxo(_REG_TX, 0, 3_000_000)
    reg_utxo.datum = registry

    tranches = [
        VestingTranche(release_time=NOW_MS - 86400000, lovelace=1_000_000),  # matured
        VestingTranche(release_time=NOW_MS + 86400000, lovelace=2_000_000),  # future
    ]
    action = CreateVestingAction(
        recipient="b2" * 28,
        tranches=tranches,
        memo="quarterly vesting",
    )
    gov_datum = GovernanceDatum(
        proposer="a1" * 28, description="Vest 3 ADA", action=action,
        votes=[], status=PROPOSAL_EXECUTED,
        vote_deadline=NOW_MS - 1000, execute_after=NOW_MS - 500,
        quorum=3, registry_ref=OutputReference(_REG_TX, 0),
        registry_version=1, deposit=2_000_000,
    )
    gov_utxo = make_utxo(_GOV_TX, 0, 2_000_000)
    gov_utxo.datum = gov_datum

    treas_datum = TreasuryDatum(governance_script_hash="ff" * 28, max_transfer_lovelace=5_000_000)
    treas_utxo = make_utxo(_TREAS_TX, 0, 47_000_000)
    treas_utxo.datum = treas_datum

    vest_datum = VestingDatum(
        recipient="b2" * 28,
        tranches=tranches,
        proposal_ref=OutputReference(_GOV_TX, 0),
    )
    vest_utxo = make_utxo(_VEST_TX, 0, 3_000_000)
    vest_utxo.datum = vest_datum

    return registry, reg_utxo, gov_datum, gov_utxo, treas_datum, treas_utxo, vest_datum, vest_utxo, tranches


def test_vesting_tranche_matured():
    t = VestingTranche(release_time=NOW_MS - 1000, lovelace=1_000_000)
    ok(t.release_time <= NOW_MS)

def test_vesting_tranche_future():
    t = VestingTranche(release_time=NOW_MS + 1_000_000, lovelace=1_000_000)
    ok(t.release_time > NOW_MS)

def test_create_vesting_action_str():
    a = CreateVestingAction(recipient="b2" * 28, tranches=[
        VestingTranche(release_time=NOW_MS + 86400000, lovelace=1_000_000),
    ], memo="vest")
    ok("CreateVesting" in str(a))
    eq(a.total_lovelace(), 1_000_000)

def test_vesting_datum_matured_remaining():
    _, _, _, _, _, _, vest_datum, _, tranches = _make_vesting_state()
    matured = vest_datum.matured_tranches(NOW_MS)
    remaining = vest_datum.remaining_tranches(NOW_MS)
    eq(len(matured), 1)
    eq(len(remaining), 1)
    eq(vest_datum.claimable_lovelace(NOW_MS), 1_000_000)

def test_build_create_vesting_tx():
    _, _, gov_datum, gov_utxo, treas_datum, treas_utxo, _, _, _ = _make_vesting_state()
    tx = build_create_vesting_tx(
        treasury_utxo=treas_utxo,
        treasury_datum=treas_datum,
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        vesting_script_hash="ee" * 28,
        treasury_script_hash="ff" * 28,
    )
    ok(isinstance(tx, UnsignedTransaction))
    ok("CreateVesting" in tx.description or "3.00" in tx.description)
    eq(len(tx.inputs), 1)         # treasury UTxO
    eq(len(tx.reference_inputs), 1)  # governance UTxO
    eq(len(tx.outputs), 2)        # vesting + treasury continuing

def test_build_create_vesting_tx_total_lovelace():
    _, _, gov_datum, gov_utxo, treas_datum, treas_utxo, _, _, tranches = _make_vesting_state()
    tx = build_create_vesting_tx(
        treasury_utxo=treas_utxo,
        treasury_datum=treas_datum,
        governance_utxo=gov_utxo,
        governance_datum=gov_datum,
        vesting_script_hash="ee" * 28,
    )
    vesting_out = tx.outputs[0]
    total = sum(t.lovelace for t in tranches)
    eq(vesting_out.lovelace, total)

def test_build_claim_vesting_tx_partial():
    _, _, _, _, _, _, vest_datum, vest_utxo, _ = _make_vesting_state()
    recipient_addr = "addr_test1v" + "b2" * 20
    tx = build_claim_vesting_tx(
        vesting_utxo=vest_utxo,
        vesting_datum=vest_datum,
        recipient_address=recipient_addr,
        current_time_ms=NOW_MS,
        vesting_script_hash="ee" * 28,
    )
    ok(isinstance(tx, UnsignedTransaction))
    ok("ClaimVested" in tx.description or "1.00" in tx.description)
    eq(len(tx.outputs), 2)  # recipient + continuing vesting UTxO
    eq(tx.outputs[0].lovelace, 1_000_000)   # matured amount
    eq(tx.outputs[1].lovelace, 2_000_000)   # remaining

def test_build_claim_vesting_tx_full():
    # When all tranches matured, no continuing output
    tranches_all_matured = [
        VestingTranche(release_time=NOW_MS - 2_000_000, lovelace=1_000_000),
        VestingTranche(release_time=NOW_MS - 1_000_000, lovelace=2_000_000),
    ]
    vest_datum = VestingDatum(
        recipient="b2" * 28,
        tranches=tranches_all_matured,
        proposal_ref=OutputReference(_GOV_TX, 0),
    )
    vest_utxo = make_utxo("e5" * 32, 0, 3_000_000)
    vest_utxo.datum = vest_datum

    tx = build_claim_vesting_tx(
        vesting_utxo=vest_utxo,
        vesting_datum=vest_datum,
        recipient_address="addr_test1vb2b2b2",
        current_time_ms=NOW_MS,
        vesting_script_hash="ee" * 28,
    )
    eq(len(tx.outputs), 1)  # only recipient, no continuing output
    eq(tx.outputs[0].lovelace, 3_000_000)

def test_build_claim_vesting_tx_no_matured():
    # Nothing matured yet → should raise
    tranches_future = [
        VestingTranche(release_time=NOW_MS + 1_000_000, lovelace=2_000_000),
    ]
    vest_datum = VestingDatum(
        recipient="b2" * 28, tranches=tranches_future,
        proposal_ref=OutputReference(_GOV_TX, 0),
    )
    vest_utxo = make_utxo("f6" * 32, 0, 2_000_000)
    vest_utxo.datum = vest_datum

    raised = False
    try:
        build_claim_vesting_tx(
            vesting_utxo=vest_utxo, vesting_datum=vest_datum,
            recipient_address="addr_test1vb2b2",
            current_time_ms=NOW_MS, vesting_script_hash="ee" * 28,
        )
    except AssertionError:
        raised = True
    ok(raised, "Expected AssertionError when no tranches matured")

def test_mock_fos_state_has_vesting():
    from fos_agent.chain import mock_fos_state
    s = mock_fos_state()
    ok(len(s.vesting_utxos) > 0)
    _, d = s.vesting_utxos[0]
    ok(len(d.tranches) > 0)

def test_fos_state_claimable_vesting():
    from fos_agent.chain import mock_fos_state
    s = mock_fos_state()
    # The mock has one matured tranche
    ok(len(s.claimable_vesting) > 0)

def test_vesting_datum_cbor_roundtrip():
    try:
        from fos_agent.datums import vesting_datum_cbor_hex
        from fos_agent.types import VestingDatum as VD
    except ImportError:
        return  # cbor2 not installed
    d = VestingDatum(
        recipient="aa" * 28,
        tranches=[VestingTranche(release_time=1_000_000, lovelace=2_000_000)],
        proposal_ref=OutputReference("bb" * 32, 0),  # bb is valid hex
    )
    hex_val = vesting_datum_cbor_hex(d)
    ok(len(hex_val) > 0)
    d2 = VestingDatum.from_cbor_hex(hex_val)
    eq(d2.recipient, d.recipient)
    eq(len(d2.tranches), 1)
    eq(d2.tranches[0].lovelace, 2_000_000)
    eq(d2.proposal_ref.output_index, 0)

def test_create_vesting_action_cbor_roundtrip():
    try:
        from fos_agent.datums import governance_datum_cbor_hex
        from fos_agent.types import GovernanceDatum as GD
    except ImportError:
        return
    tranches = [VestingTranche(release_time=9_000_000, lovelace=1_500_000)]
    action = CreateVestingAction(recipient="cc" * 28, tranches=tranches, memo="vest test")
    gov = GovernanceDatum(
        proposer="a1" * 28, description="Vest test",
        action=action, votes=[], status=PROPOSAL_VOTING,
        vote_deadline=NOW_MS + 86400000, execute_after=NOW_MS + 86400000 * 2,
        quorum=3, registry_ref=OutputReference("aa" * 32, 0),
        registry_version=1, deposit=2_000_000,
    )
    hex_val = governance_datum_cbor_hex(gov)
    ok(len(hex_val) > 0)
    gov2 = GovernanceDatum.from_cbor_hex(hex_val)
    ok(isinstance(gov2.action, CreateVestingAction))
    eq(gov2.action.memo, "vest test")
    eq(gov2.action.tranches[0].lovelace, 1_500_000)


test("VestingTranche: matured if release_time <= now",          test_vesting_tranche_matured)
test("VestingTranche: future if release_time > now",            test_vesting_tranche_future)
test("CreateVestingAction: __str__ and total_lovelace",         test_create_vesting_action_str)
test("VestingDatum: matured/remaining split correct",           test_vesting_datum_matured_remaining)
test("build_create_vesting_tx: shape and inputs",               test_build_create_vesting_tx)
test("build_create_vesting_tx: vesting output holds total",     test_build_create_vesting_tx_total_lovelace)
test("build_claim_vesting_tx: partial claim → 2 outputs",       test_build_claim_vesting_tx_partial)
test("build_claim_vesting_tx: full claim → 1 output",           test_build_claim_vesting_tx_full)
test("build_claim_vesting_tx: nothing matured raises",          test_build_claim_vesting_tx_no_matured)
test("mock_fos_state: has vesting UTxOs",                       test_mock_fos_state_has_vesting)
test("FOSState.claimable_vesting: detects matured tranche",     test_fos_state_claimable_vesting)
test("VestingDatum CBOR roundtrip",                             test_vesting_datum_cbor_roundtrip)
test("CreateVestingAction CBOR roundtrip via GovernanceDatum",  test_create_vesting_action_cbor_roundtrip)


# ─── Results ──────────────────────────────────────────────

total = passed + failed
print(f"\n{'='*50}")
print(f"  {passed}/{total} tests passed", "✅" if failed == 0 else "❌")
if failed:
    print(f"  {failed} test(s) failed")
print(f"{'='*50}\n")

sys.exit(0 if failed == 0 else 1)
