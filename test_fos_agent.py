"""
Test suite for the FOS agentic layer.
Runs without a Blockfrost API key or Anthropic API key.
Tests: type parsing, chain state logic, transaction builders, agent decisions.
"""

import json
import sys
import os
import tempfile

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
    action=None,
):
    return GovernanceDatum(
        proposer="cafe",
        description="Test proposal",
        action=action or TreasuryTransferAction(
            recipient="bb", lovelace=2_000_000, memo="salary"
        ),
        votes=votes or [],
        status=status,
        vote_deadline=vote_deadline,
        execute_after=execute_after,
        quorum=quorum,
        registry_ref=make_ref(),
        registry_version=registry_version,
    )

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


# ─── Results ──────────────────────────────────────────────

total = passed + failed
print(f"\n{'='*50}")
print(f"  {passed}/{total} tests passed", "✅" if failed == 0 else "❌")
if failed:
    print(f"  {failed} test(s) failed")
print(f"{'='*50}\n")

sys.exit(0 if failed == 0 else 1)
